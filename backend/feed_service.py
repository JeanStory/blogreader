"""核心服务：RSS/Atom 自动发现 + 抓取解析入库。

对用户来说，只需给一个博客主页 URL，系统自动找到它的 feed。
实现同步版本（httpx.Client + feedparser），FastAPI 的同步端点会自动跑在线程池，
APScheduler 调度线程也可直接调用，避免 async 复杂度。
"""
from __future__ import annotations

import re
import time
import calendar
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlmodel import Session, select

from backend.db import engine
from backend.models import Article, Feed

USER_AGENT = "BlogReader/0.1 (+https://github.com/; personal feed reader)"
_HEADERS = {"User-Agent": USER_AGENT}
_TIMEOUT = 15.0
# 按需抓全文是用户主动触发的操作，容忍慢站较长等待，与调度器的 XML 抓取超时分开
_FULL_FETCH_TIMEOUT = 40.0

# feed 的 content-type 特征
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
# 常见 feed 相对路径（自动发现兜底）
_COMMON_PATHS = ("/index.xml", "/feed", "/feed/", "/rss.xml", "/atom.xml",
                 "/feed.xml", "/rss", "/feed/atom", "/blog/feed", "/atom")


def _client() -> httpx.Client:
    return httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)


def _looks_like_feed(content: bytes) -> bool:
    """用 feedparser 快速判断一段内容是否为有效 feed（有条目或有 feed 标题）。"""
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        return False
    return bool(parsed.entries) or bool(parsed.feed.get("title"))


def discover_feed(url: str) -> Optional[str]:
    """给定博客主页 URL，返回其 feed 地址；找不到返回 None。

    策略：1) 若 URL 本身就是 feed 直接返回；
         2) 解析 HTML <link rel=alternate> 声明；
         3) 尝试常见 feed 路径。
    """
    if not urlparse(url).scheme:
        url = "https://" + url

    with _client() as client:
        # 1) URL 本身可能就是 feed
        try:
            r = client.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").lower()
            if any(t in ctype for t in ("xml", "rss", "atom")) and _looks_like_feed(r.content):
                return str(r.url)
        except Exception:
            r = None

        # 2) 从 HTML 里找 <link rel="alternate" type="application/rss+xml">
        if r is not None and "html" in r.headers.get("content-type", "").lower():
            soup = BeautifulSoup(r.text, "html.parser")
            for link in soup.find_all("link", rel=True):
                rel = " ".join(link.get("rel", [])).lower()
                ltype = (link.get("type") or "").lower()
                href = link.get("href")
                if href and "alternate" in rel and any(t in ltype for t in _FEED_TYPES):
                    return urljoin(str(r.url), href)

        # 3) 兜底：尝试常见路径
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        for path in _COMMON_PATHS:
            candidate = urljoin(base + "/", path.lstrip("/"))
            try:
                cr = client.get(candidate)
                if cr.status_code == 200 and _looks_like_feed(cr.content):
                    return str(cr.url)
            except Exception:
                continue
    return None


def _parse_time(entry) -> Optional[datetime]:
    """把 feed 的时间字段转为 naive UTC datetime。

    用 calendar.timegm（按 UTC 解释 struct_time，不受本地时区/平台范围限制），
    避免 time.mktime 对未来日期或极端时区抛 OverflowError。
    """
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc).replace(tzinfo=None)
            except (OverflowError, ValueError, OSError):
                continue
    return None


def _entry_guid(entry, link: str) -> str:
    return entry.get("id") or entry.get("guid") or link or entry.get("title", "")


def _extract_content(entry) -> str:
    """取正文 HTML：优先 content:encoded，其次 summary。"""
    if entry.get("content"):
        # feedparser 把 content 存成 list
        return entry["content"][0].get("value", "")
    return entry.get("summary", "")


def fetch_feed(feed: Feed, session: Session) -> dict:
    """抓取单个 feed，把新文章入库（按 guid 去重）。返回统计 dict。"""
    result = {"feed_id": feed.id, "new": 0, "status": "ok", "error": ""}
    try:
        with _client() as client:
            r = client.get(feed.feed_url)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)

        # 更新 feed 元信息（首次抓取时补全标题）
        if parsed.feed.get("title") and not feed.title:
            feed.title = parsed.feed.get("title", "")
        if parsed.feed.get("subtitle") and not feed.description:
            feed.description = parsed.feed.get("subtitle", "")
        if parsed.feed.get("link") and not feed.site_link:
            feed.site_link = parsed.feed.get("link", "")

        for entry in parsed.entries:
            link = entry.get("link", "")
            guid = _entry_guid(entry, link)
            if not guid:
                continue
            exists = session.exec(select(Article).where(Article.guid == guid)).first()
            if exists:
                continue
            article = Article(
                feed_id=feed.id,
                guid=guid,
                title=entry.get("title", "(无标题)"),
                link=link,
                author=entry.get("author", ""),
                summary=entry.get("summary", "")[:2000],
                content=_extract_content(entry),
                published_at=_parse_time(entry),
            )
            session.add(article)
            result["new"] += 1

        feed.last_status = "ok"
        feed.last_error = ""
        feed.last_fetched_at = datetime.utcnow()
        session.add(feed)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        result["new"] = 0  # 抓取失败，之前累加的计数作废（数据已回滚）
        feed.last_status = "error"
        feed.last_error = str(e)[:500]
        feed.last_fetched_at = datetime.utcnow()
        session.add(feed)
        session.commit()
        result["status"] = "error"
        result["error"] = str(e)
    return result


def add_feed(site_url: str) -> dict:
    """用户新增订阅：自动发现 feed → 建记录 → 首次抓取。

    返回 {ok, feed_id?, message}。
    """
    site_url = site_url.strip()
    if not urlparse(site_url).scheme:
        site_url = "https://" + site_url

    feed_url = discover_feed(site_url)
    if not feed_url:
        return {"ok": False, "message": f"未能在 {site_url} 找到 RSS/Atom 订阅源"}

    with Session(engine) as session:
        existing = session.exec(select(Feed).where(Feed.feed_url == feed_url)).first()
        if existing:
            return {"ok": False, "message": "该订阅源已存在", "feed_id": existing.id}
        feed = Feed(site_url=site_url, feed_url=feed_url, last_status="pending")
        session.add(feed)
        session.commit()
        session.refresh(feed)
        stat = fetch_feed(feed, session)
        session.refresh(feed)
        return {
            "ok": True,
            "feed_id": feed.id,
            "feed_url": feed_url,
            "title": feed.title,
            "new_articles": stat["new"],
            "message": f"订阅成功：{feed.title or feed_url}，抓到 {stat['new']} 篇文章",
        }


def refresh_all(max_workers: int = 8) -> dict:
    """并发抓取所有 feed（定时任务/手动刷新用）。

    每个 feed 在独立线程 + 独立 Session 里抓取，靠 SQLite WAL + busy_timeout
    保证并发写安全。max_workers 限制同时打开的网络连接数，避免打爆对端或本机。
    """
    from concurrent.futures import ThreadPoolExecutor

    with Session(engine) as session:
        feeds = session.exec(select(Feed)).all()
        # 先取出所有 feed id，避免 ORM 对象跨线程/session 使用
        feed_ids = [f.id for f in feeds]

    def _one(fid: int) -> dict:
        # 每个线程用自己的 session，互不共享连接
        with Session(engine) as s:
            feed = s.get(Feed, fid)
            if feed is None:
                return {"feed_id": fid, "new": 0, "status": "error", "error": "feed not found"}
            return fetch_feed(feed, s)

    details = []
    if feed_ids:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(feed_ids))) as ex:
            details = list(ex.map(_one, feed_ids))
    total_new = sum(d["new"] for d in details)
    return {"feeds": len(details), "total_new": total_new, "details": details}


# ---------- 全文按需抓取 ----------
# RSS/Atom 常常只输出摘要（<description>），没有 content:encoded 全文。
# 用户点开文章时，若尚未抓过全文，就实时抓原文页用 trafilatura 提取正文并缓存入库，
# 之后再点开直接走缓存，避免重复网络请求。

def _normalize_extracted_html(html: str, base_url: str, source_html: Optional[str] = None) -> str:
    """把 trafilatura 的自定义 XML 标签转成标准 HTML，并把相对 URL 转绝对。

    trafilatura 的 output_format="html" 用的是自定义 schema，浏览器不认：
      - 图片输出 <graphic src="相对路径"/> → 需转 <img>，src 转绝对 URL
      - 表格输出 <table><row><cell role="head"> → 需转 <tr>/<th>/<td>
    base_url 用文章原文链接，用于把相对路径（如 shortcut-features.png）补全成绝对地址。
    source_html 传入时，用原始 HTML 的 <figcaption> 全文补全被 trafilatura 丢弃的图注文本。
    """
    if not html:
        return html
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")

    # include_links=False 的回退路径会用 <html><body> 包裹正文；文章正文会被
    # 注入到前端的内容容器里，嵌套的 <html>/<body> 属非法结构，浏览器会剥离/错乱。
    # 这里统一拆掉外层 html/body，保证输出是干净的 HTML 片段。
    for wrapper in soup.find_all(["html", "body"]):
        wrapper.unwrap()

    # <graphic> → <img>
    for g in soup.find_all("graphic"):
        g.name = "img"

    # 表格伪标签 → 标准标签
    for r in soup.find_all("row"):
        r.name = "tr"
        if r.has_attr("span"):
            del r["span"]
    for c in soup.find_all("cell"):
        is_head = c.get("role") == "head"
        c.name = "th" if is_head else "td"
        if c.has_attr("role"):
            del c["role"]
        if c.has_attr("span"):  # trafilatura 用 span 表示跨列
            c["colspan"] = c["span"]
            del c["span"]

    # 相对 URL 转绝对（图片、链接）
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            img["src"] = urljoin(base_url, src)
    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            a["href"] = urljoin(base_url, href)

    # 单行 <pre> → 内联 <code>
    # trafilatura 常把正文里的单个术语/命令名（如 bash、grep、"think step by step"）
    # 也输出成 <pre> 代码块，视觉上像整块代码，很突兀。凡是不含换行的短 <pre>
    # （真正的多行代码块才含换行）一律降级成内联 <code>，只保留等宽字体的行内样式。
    for pre in soup.find_all("pre"):
        text = pre.get_text()
        if "\n" not in text:
            pre.name = "code"

    _restore_figcaptions(soup, source_html, base_url)

    _merge_split_paragraphs(soup)

    return str(soup)


def _restore_figcaptions(soup, source_html: Optional[str], base_url: str) -> None:
    """用原始 HTML 的 <figcaption> 全文补全被 trafilatura 丢弃的图注。

    trafilatura 提取 <figcaption> 时会丢掉内联 <a> 之前的裸文本节点，
    例如原文 "The framework of ACE. (Image source: <a>Zhang et al. 2025</a> )"
    只剩下 "<a>Zhang et al. 2025</a> )"。这里从原始 HTML 按图片文件名建立
    {basename: figcaption_html} 映射，再把提取结果里紧跟每张 <img> 的图注段落
    整体替换成完整版本（链接 href 转绝对）。
    """
    if not source_html:
        return
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    import os

    src_soup = BeautifulSoup(source_html, "html.parser")
    cap_map = {}  # basename -> figcaption 的内部 HTML（已转绝对 URL，含 "Figure N: " 前缀）
    # 源站用 CSS 计数器给图注自动编号：
    #   .post-content figure { counter-increment: figure-counter }
    #   figcaption::before   { content: "Figure " counter(figure-counter) ": " }
    # 计数器对每个 <figure> 递增（含无 figcaption 的图），编号需按全部 figure 顺序算。
    fig_counter = 0
    # CSS 选择器是 `.post-content figure`，限定容器内计数以匹配原文编号
    fig_scope = src_soup.find(class_="post-content") or src_soup
    for fig in fig_scope.find_all("figure"):
        fig_counter += 1
        img = fig.find("img")
        cap = fig.find("figcaption")
        if not img or not cap:
            continue
        src = img.get("src")
        if not src:
            continue
        # 图注里的相对链接转绝对
        for a in cap.find_all("a"):
            href = a.get("href")
            if href:
                a["href"] = urljoin(base_url, href)
        for im in cap.find_all("img"):
            isrc = im.get("src")
            if isrc:
                im["src"] = urljoin(base_url, isrc)
        inner = cap.decode_contents().strip()
        # 折叠图注内多余空白，避免换行导致视觉断行
        inner = re.sub(r"\s+", " ", inner).strip()
        if inner:
            # 补上 CSS 生成的 "Figure N: " 编号前缀
            cap_map[os.path.basename(src)] = f"Figure {fig_counter}: {inner}"

    if not cap_map:
        return

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        full = cap_map.get(os.path.basename(src))
        if not full:
            continue
        # trafilatura 可能把一条 figcaption 拆成紧跟 <img> 的多个 <p>
        # （尤其含 <br/> 时），例如 <p>(Image source:</p><p><a>x</a>)</p>。
        # 收集这些「文本是完整图注子串」的连续 <p> 残片，首个替换为完整图注，其余删除。
        full_text = re.sub(
            r"\s+", " ",
            BeautifulSoup(full, "html.parser").get_text()
        ).strip()
        fragments = []
        sib = img.find_next_sibling()
        while sib is not None and sib.name == "p":
            frag_text = re.sub(r"\s+", " ", sib.get_text()).strip()
            # 空段或文本属于完整图注的一部分 → 判定为图注残片
            if frag_text and frag_text not in full_text:
                break
            fragments.append(sib)
            sib = sib.find_next_sibling()
        if fragments:
            new_p = BeautifulSoup(f"<p>{full}</p>", "html.parser").p
            fragments[0].replace_with(new_p)
            for extra in fragments[1:]:
                extra.decompose()


def _merge_split_paragraphs(soup) -> None:
    """合并被 trafilatura 在内联链接处拆断的段落。

    trafilatura 遇到图注里的内联 <a>（如 "(Image source: <a>OpenAI post</a>)"）
    时会把一句话拆成两个 <p>：<p>(Image source:</p><p><a>...</a>)</p>。
    这样图注被断成两行，翻译时还会被当成两个独立段落。

    判据：某 <p> 文本里出现未闭合的 '('（'(' 多于 ')'），说明句子没写完，
    把紧随其后的 <p> 兄弟合并进来，直到括号闭合。只在括号不平衡时触发，
    对正常段落无副作用。
    """
    def unbalanced(txt: str) -> int:
        return txt.count("(") - txt.count(")")

    for p in soup.find_all("p"):
        if p.parent is None:  # 已在前面的合并里被吃掉
            continue
        guard = 0
        while unbalanced(p.get_text()) > 0 and guard < 5:
            guard += 1
            nxt = p.find_next_sibling()
            if nxt is None or nxt.name != "p":
                break
            p.append(" ")
            for child in list(nxt.children):
                p.append(child.extract())
            nxt.decompose()


def _extract_main_html(html: str) -> Optional[str]:
    """用 trafilatura 提取正文 HTML。

    trafilatura 1.12.2 在 include_links=True 且页面含无 target 的锚点时会抛
    TypeError（convert_to_html 的已知缺陷）。故先尝试带链接提取，失败则回退
    到不带链接的提取，保证正文仍能抓到。
    """
    import trafilatura

    common = dict(
        output_format="html",
        include_images=True,
        include_formatting=True,
        favor_recall=True,
    )
    try:
        return trafilatura.extract(html, include_links=True, **common)
    except Exception:
        # 回退：不带链接，避开 include_links 触发的 lxml 崩溃
        try:
            return trafilatura.extract(html, include_links=False, **common)
        except Exception:
            return None


def fetch_full_content(article_id: int) -> dict:
    """按需抓取单篇文章全文。返回 {ok, updated, content_len, reason?}。

    幂等：已 full_fetched 的直接返回缓存，不重复抓取。
    失败降级：trafilatura 提取不到时，回退到已存的 content/summary，
    并仍标记 full_fetched=True 避免每次点开都重试拖慢体验。
    """
    with Session(engine) as session:
        art = session.get(Article, article_id)
        if art is None:
            return {"ok": False, "reason": "article not found"}
        if art.full_fetched:
            return {"ok": True, "updated": False, "content_len": len(art.content or ""), "cached": True}
        if not art.link:
            art.full_fetched = True
            session.add(art); session.commit()
            return {"ok": True, "updated": False, "reason": "no link", "content_len": len(art.content or "")}

        try:
            resp = httpx.get(art.link, headers=_HEADERS, timeout=_FULL_FETCH_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:  # 网络失败：不标记，允许下次重试
            return {"ok": False, "reason": f"fetch error: {e}", "content_len": len(art.content or "")}

        extracted = None
        try:
            import trafilatura
            extracted = _extract_main_html(html)
        except Exception as e:
            extracted = None

        if extracted and len(extracted) > len(art.content or ""):
            extracted = _normalize_extracted_html(extracted, art.link, source_html=html)
            art.content = extracted
            updated = True
        else:
            updated = False
        art.full_fetched = True
        session.add(art)
        session.commit()
        return {"ok": True, "updated": updated, "content_len": len(art.content or "")}
