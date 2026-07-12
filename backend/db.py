"""数据库初始化与会话管理。SQLite，零配置，个人自用。

将来迁移到 PostgreSQL 只需改 DATABASE_URL。
"""
import os
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import event

# data/ 目录与本文件同级的上一层（blogreader/data）
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

DATABASE_URL = os.environ.get(
    "BLOGREADER_DB",
    f"sqlite:///{os.path.join(_DATA_DIR, 'blogreader.db')}",
)

# check_same_thread=False 让调度线程也能访问同一 SQLite
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """每个新连接都开 WAL + busy_timeout。

        WAL 让读不阻塞写、写不阻塞读，配合并发抓取（ThreadPoolExecutor）时
        避免 "database is locked"；busy_timeout 让偶发写锁自动重试 5s 而非立刻报错。
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def init_db() -> None:
    """建表（若不存在）。需在 import 完 models 后调用。"""
    import backend.models  # noqa: F401  确保模型注册到 metadata
    SQLModel.metadata.create_all(engine)
    _migrate()
    _setup_fts()


# SQLite 的 create_all 不会给已存在的表补新列，这里做最小手写迁移：
# 检测缺失的列并 ALTER TABLE ADD COLUMN。仅用于个人自用的轻量场景。
_MIGRATIONS = {
    "feed": [
        ("backfill_method", "VARCHAR DEFAULT ''"),
        ("backfill_cursor", "VARCHAR DEFAULT ''"),
        ("backfill_done", "BOOLEAN DEFAULT 0"),
        ("backfill_total", "INTEGER DEFAULT 0"),
    ],
}


def _migrate() -> None:
    from sqlalchemy import text
    if not DATABASE_URL.startswith("sqlite"):
        return  # 仅对 SQLite 做这种轻量迁移
    with engine.begin() as conn:
        for table, cols in _MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, decl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))


def get_session() -> Session:
    """返回一个新的数据库会话。调用方负责关闭（或用 with）。"""
    return Session(engine)


def _setup_fts() -> None:
    """建立文章全文检索（FTS5 + trigram 分词器）。

    - trigram 分词器支持中英文子串匹配（无需空格分词），代价是查询词需 >= 3 字符，
      更短的查询在 API 层用 LIKE 兜底。
    - 采用 external-content 模式：FTS 表只存索引，正文仍在 article 表，避免数据翻倍。
    - 三个触发器让 article 的增/删/改自动同步到 FTS 索引。
    - 首次建表后做一次 rebuild，把已有文章灌进索引。
    """
    from sqlalchemy import text
    if not DATABASE_URL.startswith("sqlite"):
        return  # FTS5 是 SQLite 专属，迁到 PG 时改用其原生全文检索
    with engine.begin() as conn:
        # 探测 FTS5 是否可用，不可用则静默跳过（API 层仍有 LIKE 兜底）
        try:
            conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS article_fts USING fts5("
                "title, summary, content, "
                "content='article', content_rowid='id', "
                "tokenize='trigram')"
            ))
        except Exception:  # noqa: BLE001  FTS5 编译缺失等
            return
        # 判断索引是否为空，空则首次灌数据。
        # 注意：external-content 模式下 count(*) FROM article_fts 会代理到 article 表，
        # 永远等于文章数，不能用来判断索引是否已建。改查内部 %_docsize 影子表：
        # 它每条已索引文档占一行，未 rebuild 时为 0 行。
        index_docs = conn.execute(
            text("SELECT count(*) FROM article_fts_docsize")
        ).scalar()
        exists_rows = (index_docs or 0) > 0
        # 同步触发器（幂等，IF NOT EXISTS）
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS article_ai AFTER INSERT ON article BEGIN "
            "INSERT INTO article_fts(rowid, title, summary, content) "
            "VALUES (new.id, new.title, new.summary, new.content); END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS article_ad AFTER DELETE ON article BEGIN "
            "INSERT INTO article_fts(article_fts, rowid, title, summary, content) "
            "VALUES('delete', old.id, old.title, old.summary, old.content); END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS article_au AFTER UPDATE ON article BEGIN "
            "INSERT INTO article_fts(article_fts, rowid, title, summary, content) "
            "VALUES('delete', old.id, old.title, old.summary, old.content); "
            "INSERT INTO article_fts(rowid, title, summary, content) "
            "VALUES (new.id, new.title, new.summary, new.content); END"
        ))
        if not exists_rows:
            # rebuild 会依据 external content 表重建整个索引
            conn.execute(text("INSERT INTO article_fts(article_fts) VALUES('rebuild')"))
