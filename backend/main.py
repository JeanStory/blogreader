"""FastAPI 应用：博客阅读器后端 API + 前端静态托管。

端点分两类：
- /api/feeds*     订阅源的增删查
- /api/articles*  文章列表、详情、已读/星标、手动刷新
所有 DB 相关端点用同步 def（FastAPI 自动放线程池），与同步的 feed_service 一致。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session, select, func

from backend.db import engine, init_db
from backend.models import Feed, Article
from backend import feed_service
from backend import scheduler as scheduler_mod
from backend import llm

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_BASE, "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler_mod.start_scheduler()
    try:
        yield
    finally:
        scheduler_mod.stop_scheduler()


app = FastAPI(title="BlogReader", version="0.1", lifespan=lifespan)


# ---------- 请求体模型 ----------
class AddFeedReq(BaseModel):
    url: str


class ReadReq(BaseModel):
    is_read: bool = True


class StarReq(BaseModel):
    is_starred: bool = True


class SettingsReq(BaseModel):
    api_type: str = "openai"
    base_url: str = ""
    api_key: str = ""       # 留空表示不修改已存的密钥
    model: str = ""


class TranslateReq(BaseModel):
    segments: list[str]
    target_lang: str = "zh"


# ---------- 订阅源 ----------
@app.post("/api/feeds")
def add_feed(req: AddFeedReq):
    if not req.url.strip():
        raise HTTPException(400, "url 不能为空")
    res = feed_service.add_feed(req.url.strip())
    if not res.get("ok"):
        raise HTTPException(400, res.get("message", "订阅失败"))
    return res


@app.get("/api/feeds")
def list_feeds():
    """返回所有订阅源，附带未读数与总文章数。"""
    with Session(engine) as s:
        feeds = s.exec(select(Feed).order_by(Feed.created_at)).all()
        out = []
        for f in feeds:
            total = s.exec(select(func.count(Article.id)).where(Article.feed_id == f.id)).one()
            unread = s.exec(
                select(func.count(Article.id)).where(Article.feed_id == f.id, Article.is_read == False)  # noqa: E712
            ).one()
            out.append({
                "id": f.id, "title": f.title or f.site_url, "site_url": f.site_url,
                "feed_url": f.feed_url, "site_link": f.site_link,
                "last_fetched_at": f.last_fetched_at.isoformat() if f.last_fetched_at else None,
                "last_status": f.last_status, "last_error": f.last_error,
                "total": total, "unread": unread,
            })
        return out


@app.delete("/api/feeds/{feed_id}")
def delete_feed(feed_id: int):
    with Session(engine) as s:
        f = s.get(Feed, feed_id)
        if not f:
            raise HTTPException(404, "订阅源不存在")
        for a in s.exec(select(Article).where(Article.feed_id == feed_id)).all():
            s.delete(a)
        s.delete(f)
        s.commit()
        return {"ok": True}


@app.post("/api/feeds/{feed_id}/refresh")
def refresh_feed(feed_id: int):
    with Session(engine) as s:
        f = s.get(Feed, feed_id)
        if not f:
            raise HTTPException(404, "订阅源不存在")
        stat = feed_service.fetch_feed(f, s)
        return stat


# ---------- 文章 ----------
@app.get("/api/articles")
def list_articles(feed_id: Optional[int] = None, filter: str = "all", limit: int = 100, offset: int = 0):
    """文章列表。filter: all|unread|starred。按发布时间倒序。"""
    with Session(engine) as s:
        q = select(Article)
        if feed_id is not None:
            q = q.where(Article.feed_id == feed_id)
        if filter == "unread":
            q = q.where(Article.is_read == False)  # noqa: E712
        elif filter == "starred":
            q = q.where(Article.is_starred == True)  # noqa: E712
        q = q.order_by(Article.published_at.desc().nullslast()).limit(limit).offset(offset)
        arts = s.exec(q).all()
        # 附带 feed 标题
        feed_titles = {f.id: (f.title or f.site_url) for f in s.exec(select(Feed)).all()}
        return [{
            "id": a.id, "feed_id": a.feed_id, "feed_title": feed_titles.get(a.feed_id, ""),
            "title": a.title, "link": a.link, "author": a.author,
            "summary": a.summary, "published_at": a.published_at.isoformat() if a.published_at else None,
            "is_read": a.is_read, "is_starred": a.is_starred,
        } for a in arts]


@app.get("/api/articles/{article_id}")
def get_article(article_id: int):
    # 按需抓全文：RSS 常只给摘要，首次点开时抓原文页提取正文并缓存
    with Session(engine) as s:
        exists = s.get(Article, article_id)
        if not exists:
            raise HTTPException(404, "文章不存在")
    feed_service.fetch_full_content(article_id)
    with Session(engine) as s:
        a = s.get(Article, article_id)
        if not a:
            raise HTTPException(404, "文章不存在")
        return {
            "id": a.id, "feed_id": a.feed_id, "title": a.title, "link": a.link,
            "author": a.author, "content": a.content or a.summary,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "is_read": a.is_read, "is_starred": a.is_starred,
        }


@app.post("/api/articles/{article_id}/read")
def set_read(article_id: int, req: ReadReq):
    return _update_flag(article_id, "is_read", req.is_read)


@app.post("/api/articles/{article_id}/star")
def set_star(article_id: int, req: StarReq):
    return _update_flag(article_id, "is_starred", req.is_starred)


def _update_flag(article_id: int, field: str, value: bool):
    with Session(engine) as s:
        a = s.get(Article, article_id)
        if not a:
            raise HTTPException(404, "文章不存在")
        setattr(a, field, value)
        s.add(a)
        s.commit()
        return {"ok": True, field: value}


@app.post("/api/refresh")
def refresh_all():
    """手动刷新所有订阅源。"""
    return feed_service.refresh_all()


# ---------- 翻译设置 & 翻译 ----------
def _masked_settings() -> dict:
    """返回配置给前端，api_key 不回传明文，只给是否已配置。"""
    cfg = llm.get_config()
    return {
        "api_type": cfg.get("api_type") or "openai",
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "has_key": bool(cfg.get("api_key")),
    }


@app.get("/api/settings")
def get_settings():
    return _masked_settings()


@app.post("/api/settings")
def save_settings(req: SettingsReq):
    llm.save_config(req.model_dump())
    return _masked_settings()


@app.post("/api/settings/test")
def test_settings(req: SettingsReq):
    """用当前（或临时传入的）配置发一个最小请求，验证接口可用。"""
    cfg = llm.get_config()
    # 允许用表单里的值临时覆盖测试；api_key 留空则用已存的
    data = req.model_dump()
    for k in ("api_type", "base_url", "model"):
        if data.get(k):
            cfg[k] = data[k]
    if data.get("api_key"):
        cfg["api_key"] = data["api_key"]
    if not llm.is_configured(cfg):
        raise HTTPException(400, "配置不完整，请填写 base_url / api_key / model。")
    try:
        reply = llm.chat([{"role": "user", "content": "ping"}],
                         system="Reply with the single word: pong.",
                         cfg=cfg, max_tokens=16)
        return {"ok": True, "reply": reply}
    except llm.LLMError as e:
        raise HTTPException(400, str(e))


@app.post("/api/translate")
def translate(req: TranslateReq):
    if not req.segments:
        return {"translations": []}
    try:
        out = llm.translate_segments(req.segments, req.target_lang)
        return {"translations": out}
    except llm.LLMError as e:
        raise HTTPException(400, str(e))


# ---------- 前端静态托管 ----------
@app.get("/")
def index():
    return FileResponse(
        os.path.join(_FRONTEND, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if os.path.isdir(_FRONTEND):
    app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")
