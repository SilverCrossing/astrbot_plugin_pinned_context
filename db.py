"""
数据库初始化和迁移。
"""
import logging

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
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, label)
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_pinned_session ON pinned_blocks(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_pinned_status  ON pinned_blocks(session_id, status);",
]


async def init_db(db_session) -> None:
    """创建表和索引。"""
    await db_session.execute(CREATE_TABLE_SQL)
    for sql in CREATE_INDEXES_SQL:
        await db_session.execute(sql)
    await db_session.commit()
    logger.info("[钉选上下文] 数据库初始化完成")
