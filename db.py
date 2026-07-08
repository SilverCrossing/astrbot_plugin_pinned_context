"""
数据库初始化和迁移。

时间格式统一使用 Python 端 datetime.now(timezone.utc).isoformat() 生成，
格式为 ISO 8601（如 2026-07-08T12:57:14.123456+00:00），
确保字符串比较与时间顺序一致。

注意：AstrBot 使用 SQLAlchemy >= 2.0，原生 SQL 必须用 text() 包裹。
"""
import logging

from sqlalchemy import text

logger = logging.getLogger("astrbot.pinned_context")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pinned_blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    label           TEXT    NOT NULL,
    content         TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'active',
    idle_since      TEXT,
    llm_call_count  INTEGER NOT NULL DEFAULT 0,
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(session_id, label)
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_pinned_session ON pinned_blocks(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_pinned_status  ON pinned_blocks(session_id, status);",
]


async def init_db(db_session) -> None:
    """创建表和索引。"""
    await db_session.execute(text(CREATE_TABLE_SQL))
    for sql in CREATE_INDEXES_SQL:
        await db_session.execute(text(sql))
    await db_session.commit()
    logger.info("[钉选上下文] 数据库初始化完成")
