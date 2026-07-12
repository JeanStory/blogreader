"""数据库初始化与会话管理。SQLite，零配置，个人自用。

将来迁移到 PostgreSQL 只需改 DATABASE_URL。
"""
import os
from sqlmodel import SQLModel, create_engine, Session

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


def init_db() -> None:
    """建表（若不存在）。需在 import 完 models 后调用。"""
    import backend.models  # noqa: F401  确保模型注册到 metadata
    SQLModel.metadata.create_all(engine)
    _migrate()


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
