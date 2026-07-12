"""数据模型：Feed(订阅源) 与 Article(文章)。

设计要点：
- Feed 存博客订阅源信息，site_url 是用户输入的博客主页，feed_url 是自动发现的 RSS/Atom 地址。
- Article 存单篇文章，用 guid 做去重键（RSS 标准里 guid/id 唯一标识一篇内容）。
- 预留 is_read/is_starred 支持"已读/星标"，fetched_at 支持归档与增量抓取。
"""
from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field


class Feed(SQLModel, table=True):
    """一个博客订阅源。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    # 用户输入的博客主页 URL（可能不是 feed 本身）
    site_url: str = Field(index=True)
    # 自动发现或用户直接给的 feed 地址，唯一
    feed_url: str = Field(index=True, unique=True)
    title: str = ""
    description: str = ""
    site_link: str = ""  # feed 里声明的站点主页
    # 抓取状态
    last_fetched_at: Optional[datetime] = None
    last_status: str = "pending"  # pending | ok | error
    last_error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # 历史文章回溯（backfill）：探测站点能力后分批补全 RSS 之外的旧文章。
    # method: "" 未探测 | wp（WordPress REST API）| sitemap | none（两者都不支持，仅 RSS）
    # cursor: 续抓断点——wp 为下一页页码；sitemap 为下一个 URL 下标。上限是单轮速率而非总量，
    #         分多轮抓完，靠 Article.guid 去重保证幂等。
    backfill_method: str = ""
    backfill_cursor: str = ""
    backfill_done: bool = Field(default=False)
    backfill_total: int = 0  # 已知历史文章总数（wp 的 X-WP-Total；0=未知）


class Article(SQLModel, table=True):
    """一篇文章。按 guid 去重。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    feed_id: int = Field(index=True, foreign_key="feed.id")
    # 唯一标识：优先 entry.id/guid，缺失时用 link
    guid: str = Field(index=True, unique=True)
    title: str = ""
    link: str = ""
    author: str = ""
    summary: str = ""  # 摘要（纯文本或短 HTML）
    content: str = ""  # 正文 HTML（若 feed 提供 content:encoded）
    # 全文抓取标记：RSS 常只给摘要，点开文章时按需抓原文页提取全文并缓存，避免重复抓
    full_fetched: bool = Field(default=False)
    published_at: Optional[datetime] = Field(default=None, index=True)
    # 用户状态
    is_read: bool = Field(default=False, index=True)
    is_starred: bool = Field(default=False, index=True)
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class Setting(SQLModel, table=True):
    """通用键值配置表。用于存 LLM 翻译等功能的配置（api_type/base_url/api_key/model）。

    个人自用、单条配置即可，用键值表避免为每个新功能改表结构。
    api_key 明文存本地 SQLite（个人自用场景），但 API 只返回是否已配置、不回传密钥。
    """
    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Conversation(SQLModel, table=True):
    """一篇文章下的一次 AI 对话会话。

    一篇文章可有多个会话（多对话需求）。删除会话时需一并删除其消息（应用层处理）。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    article_id: int = Field(index=True, foreign_key="article.id")
    title: str = ""  # 会话标题，默认取首条用户消息前若干字
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class ChatMessage(SQLModel, table=True):
    """一条对话消息。role=user|assistant。

    quote 存划词引用片段（可选），与 content 分开存便于前端独立渲染引用块。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True, foreign_key="conversation.id")
    role: str = ""      # user | assistant
    content: str = ""
    quote: str = ""     # 划词引用片段（可选）
    created_at: datetime = Field(default_factory=datetime.utcnow)
