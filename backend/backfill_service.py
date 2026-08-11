"""历史文章回溯（backfill）。

RSS/Atom 通常只输出最近 10~20 篇，站点更早的历史文章拿不到。这里在不依赖 LLM 的前提下，
用两种确定性手段把旧文章补全：

  1. WordPress REST API：GET /wp-json/wp/v2/posts?per_page=100&page=N
     一次拿到标题/正文/日期/链接，响应头 X-WP-Total 给出文章总数。绝大多数 WP 博客可用。
  2. sitemap.xml：解析站点地图里的 URL，按顺序建"占位文章"（用 slug 猜标题 + lastmod 日期），
     正文留待用户点开时按需抓取（沿用 feed_service 的全文抓取逻辑）。
  3. 两者都不支持 → 标记 none，只保留 RSS 增量。

策略：首次订阅时用后台线程补一批（首轮配额 FIRST_RUN_MAX），之后定时任务每轮接力续抓。
单轮有条数上限（是速率而非总量），靠 Article.guid 去重保证幂等，游标 backfill_cursor 记录续抓断点：
  - wp：cursor = 下一页页码
  - sitemap：cursor = URL 列表里的下一个下标
"""
from __future__ import annotations

import re
import html
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree as ET

from sqlmodel import Session, select

from backend.db import get_session
from backend.models import Feed, Article
from backend import feed_service

logger = logging.getLogger("blogreader.backfill")

# 单轮抓取上限（速率而非总量）。sitemap 一轮只是建占位记录，很轻；wp 一轮是一次 API 请求。
PER_ROUND = 100
WP_PER_PAGE = 100          # WP REST 每页最大 100
# 新订阅首轮后台线程最多补这么多篇，余量交给定时任务，避免一次抓爆。
FIRST_RUN_MAX = 300
# sitemap 文章 URL 总量上限，防御超大站点。
SITEMAP_URL_CAP = 3000

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """解析 ISO 时间（可能带时区/只有日期），统一返回 naive datetime。"""
    if not s:
        return None
    s = s.strip().replace("Z", "")
    for cand in (s, s[:19], s[:10]):
        try:
            return datetime.fromisoformat(cand).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _site_root(feed: Feed) -> str:
    base = feed.site_link or feed.site_url or feed.feed_url
    p = urlparse(base or "")
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _title_from_url(url: str) -> str:
    """从 URL 的最后一段 slug 猜一个可读标题（sitemap 无标题时用）。"""
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug.title() if slug else url


# ---------------- 探测 ----------------

def _detect(feed: Feed) -> tuple[str, str, int]:
    """探测该源支持哪种 backfill。返回 (method, cursor, total)。"""
    root = _site_root(feed)
    if not root:
        return "none", "", 0
    # 1) WordPress REST API
    try:
        with feed_service._client() as client:
            r = client.get(f"{root}/wp-json/wp/v2/posts",
                           params={"per_page": 1, "_fields": "id"})
        if r.status_code == 200 and isinstance(r.json(), list):
            total = int(r.headers.get("X-WP-Total", "0") or 0)
            logger.info("backfill 探测 feed=%s → wp (total=%s)", feed.id, total)
            return "wp", "1", total
    except Exception:  # noqa: BLE001
        pass
    # 2) sitemap
    try:
        urls = _sitemap_urls(root)
        if urls:
            logger.info("backfill 探测 feed=%s → sitemap (%s 篇)", feed.id, len(urls))
            return "sitemap", "0", len(urls)
    except Exception:  # noqa: BLE001
        pass
    logger.info("backfill 探测 feed=%s → none", feed.id)
    return "none", "", 0


# ---------------- sitemap 解析 ----------------

_SITEMAP_CANDIDATES = [
    "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
    "/sitemap-index.xml", "/sitemap/sitemap-index.xml",
]

# 非文章 URL（分类/标签/作者/分页/静态资源等）过滤
_NON_ARTICLE = re.compile(
    r"/(tag|tags|category|categories|author|page|search|feed|wp-json|comments)/|"
    r"\.(xml|jpe?g|png|gif|webp|svg|pdf|css|js|ico|txt)$|"
    # 搜索引擎/站点验证占位文件：如 google<hash>.html、BingSiteAuth.xml、
    # 各类 -site-verification 文件，本身无正文，需剔除。
    r"/(google[0-9a-f]{8,}|bingsiteauth|yandex_[0-9a-f]+|pinterest-[0-9a-z]+)\.\w+$|"
    r"[-_]?site-verification",
    re.I,
)

# 导航/列表页 slug：站点地图里除了文章，还会列出首页和这些顶层栏目页（如
# /posts/、/archives/、/faq/），它们不是文章，需剔除。只匹配"单段路径"，
# 避免误伤 /posts/2021-diffusion-models/ 这类真正的文章 URL。
_NAV_SLUGS = {
    "posts", "post", "archive", "archives", "page", "pages",
    "tag", "tags", "category", "categories", "author", "authors",
    "about", "faq", "search", "contact", "index", "home",
    "blog", "articles", "feed", "rss", "atom", "sitemap",
}


def _is_nav_url(loc: str) -> bool:
    """判断是否为首页或顶层列表/导航页（非文章）。"""
    path = urlparse(loc).path.strip("/")
    if not path:
        return True  # 首页
    segs = path.split("/")
    return len(segs) == 1 and segs[0].lower() in _NAV_SLUGS


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fetch_xml(client, url: str) -> Optional[ET.Element]:
    try:
        r = client.get(url)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    try:
        return ET.fromstring(r.content)
    except ET.ParseError:
        return None


def _sitemap_urls(root: str) -> list[tuple[str, Optional[str]]]:
    """发现并解析 sitemap，返回按出现顺序去重的 [(loc, lastmod), ...]。"""
    with feed_service._client() as client:
        queue: list[str] = []
        # robots.txt 里声明的 Sitemap 优先
        try:
            rb = client.get(f"{root}/robots.txt")
            if rb.status_code == 200:
                for line in rb.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        queue.append(line.split(":", 1)[1].strip())
        except Exception:  # noqa: BLE001
            pass
        for path in _SITEMAP_CANDIDATES:
            queue.append(urljoin(root + "/", path.lstrip("/")))

        queue = list(dict.fromkeys(queue))  # 保序去重
        seen_sm: set[str] = set()
        seen_url: set[str] = set()
        urls: list[tuple[str, Optional[str]]] = []
        processed = 0
        while queue and processed < 200:
            sm = queue.pop(0)
            if sm in seen_sm:
                continue
            seen_sm.add(sm)
            processed += 1
            el = _fetch_xml(client, sm)
            if el is None:
                continue
            name = _localname(el.tag)
            if name == "sitemapindex":
                for loc in el.iter():
                    if _localname(loc.tag) == "loc" and loc.text:
                        queue.append(loc.text.strip())
            elif name == "urlset":
                for u in el.iter():
                    if _localname(u.tag) != "url":
                        continue
                    loc = lastmod = None
                    for c in u:
                        ln = _localname(c.tag)
                        if ln == "loc":
                            loc = (c.text or "").strip()
                        elif ln == "lastmod":
                            lastmod = (c.text or "").strip()
                    if not loc or _NON_ARTICLE.search(loc) or _is_nav_url(loc) or loc in seen_url:
                        continue
                    seen_url.add(loc)
                    urls.append((loc, lastmod))
            if len(urls) >= SITEMAP_URL_CAP:
                break
        return urls[:SITEMAP_URL_CAP]


# ---------------- 单轮抓取 ----------------

def _exists(session: Session, guid: str) -> bool:
    return session.exec(select(Article).where(Article.guid == guid)).first() is not None


def _backfill_wp(feed: Feed, session: Session, limit: int) -> int:
    root = _site_root(feed)
    page = int(feed.backfill_cursor or "1")
    per_page = max(1, min(WP_PER_PAGE, limit))
    with feed_service._client() as client:
        r = client.get(f"{root}/wp-json/wp/v2/posts", params={
            "per_page": per_page, "page": page, "orderby": "date", "order": "desc",
            "_fields": "id,date,link,title,guid,content,excerpt",
        })
    # 超出最后一页 → 抓完
    if r.status_code == 400:
        feed.backfill_done = True
        session.add(feed)
        session.commit()
        return 0
    r.raise_for_status()
    posts = r.json()
    if not isinstance(posts, list) or not posts:
        feed.backfill_done = True
        session.add(feed)
        session.commit()
        return 0
    total_pages = int(r.headers.get("X-WP-TotalPages", "0") or 0)
    added = 0
    for p in posts:
        guid = (p.get("guid") or {}).get("rendered") or p.get("link") or str(p.get("id"))
        if not guid or _exists(session, guid):
            continue
        content = (p.get("content") or {}).get("rendered", "")
        title = _strip_html((p.get("title") or {}).get("rendered", "")) or "(无标题)"
        summary = _strip_html((p.get("excerpt") or {}).get("rendered", ""))[:2000]
        session.add(Article(
            feed_id=feed.id, guid=guid, title=title, link=p.get("link", ""),
            author="", summary=summary, content=content,
            full_fetched=bool(content),  # WP 直接给了全文，无需再抓原文页
            published_at=_parse_iso(p.get("date")),
        ))
        added += 1
    feed.backfill_cursor = str(page + 1)
    if total_pages and page >= total_pages:
        feed.backfill_done = True
    session.add(feed)
    session.commit()
    return added


def _backfill_sitemap(feed: Feed, session: Session, limit: int) -> int:
    root = _site_root(feed)
    urls = _sitemap_urls(root)
    total = len(urls)
    offset = int(feed.backfill_cursor or "0")
    batch = urls[offset:offset + limit]
    added = 0
    for loc, lastmod in batch:
        if _exists(session, loc):
            continue
        session.add(Article(
            feed_id=feed.id, guid=loc, title=_title_from_url(loc), link=loc,
            author="", summary="", content="",
            full_fetched=False,  # 正文点开时按需抓取（沿用 feed_service.fetch_full_content）
            published_at=_parse_iso(lastmod),
        ))
        added += 1
    new_offset = offset + len(batch)
    feed.backfill_cursor = str(new_offset)
    feed.backfill_total = total
    if new_offset >= total:
        feed.backfill_done = True
    session.add(feed)
    session.commit()
    return added


def backfill_batch(feed: Feed, session: Session, limit: int = PER_ROUND) -> dict:
    """对单个 feed 抓一轮历史。就地更新游标并 commit。返回统计 dict。

    首次调用会先探测方法；之后按 method 走 wp / sitemap。异常不标记 done，下轮重试。
    """
    res = {"feed_id": feed.id, "added": 0, "method": feed.backfill_method,
           "done": feed.backfill_done}
    if feed.backfill_done:
        return res

    # 首次：探测
    if not feed.backfill_method:
        method, cursor, total = _detect(feed)
        feed.backfill_method = method
        feed.backfill_cursor = cursor
        feed.backfill_total = total
        if method == "none":
            feed.backfill_done = True
        session.add(feed)
        session.commit()
        res["method"] = method
        res["done"] = feed.backfill_done
        if method == "none":
            return res

    try:
        if feed.backfill_method == "wp":
            res["added"] = _backfill_wp(feed, session, limit)
        elif feed.backfill_method == "sitemap":
            res["added"] = _backfill_sitemap(feed, session, limit)
        else:
            feed.backfill_done = True
            session.add(feed)
            session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.warning("backfill 抓取失败 feed=%s method=%s: %s",
                       feed.id, feed.backfill_method, e)
    res["done"] = feed.backfill_done
    return res


# ---------------- 编排 ----------------

def backfill_all(limit: int = PER_ROUND, max_feeds: int = 50) -> dict:
    """定时任务每轮调用：给尚未抓完的 feed 各补一轮。"""
    stats = {"feeds": 0, "added": 0}
    with get_session() as session:
        pending = session.exec(
            select(Feed.id).where(Feed.backfill_done == False)  # noqa: E712
        ).all()
    for fid in pending[:max_feeds]:
        with get_session() as session:
            feed = session.get(Feed, fid)
            if not feed or feed.backfill_done:
                continue
            res = backfill_batch(feed, session, limit)
        stats["feeds"] += 1
        stats["added"] += res["added"]
    if stats["added"]:
        logger.info("定时回溯：%s 个源补全 %s 篇历史", stats["feeds"], stats["added"])
    return stats


def run_first_backfill(feed_id: int, cap: int = FIRST_RUN_MAX) -> None:
    """新订阅后台线程：连抓多轮直到抓完或达到首轮配额 cap，余量留给定时任务。"""
    got = 0
    try:
        while got < cap:
            with get_session() as session:
                feed = session.get(Feed, feed_id)
                if not feed or feed.backfill_done:
                    break
                res = backfill_batch(feed, session, min(PER_ROUND, cap - got))
            got += res["added"]
            # 没新增就停：要么抓完、要么本页全是已存在文章、要么异常，继续也无益（游标已推进，交给定时任务）
            if res["added"] == 0:
                break
        logger.info("首轮回溯完成 feed=%s 新增=%s", feed_id, got)
    except Exception:  # noqa: BLE001
        logger.exception("首轮回溯异常 feed=%s", feed_id)
