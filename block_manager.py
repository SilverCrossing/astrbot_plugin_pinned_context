"""
钉选块的 CRUD、状态管理、闲置清理。

注意：AstrBot 使用 SQLAlchemy >= 2.0，所有原生 SQL 必须用 text() 包裹，
参数使用 :paramname 命名占位符（非 ? 位置占位符）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger("astrbot.pinned_context")

# 合法的块状态
_VALID_STATUS = ("active", "idle", "pinned")


class BlockManager:
    """管理钉选上下文块的生命周期。"""

    MAX_LABEL_LEN = 20
    MAX_CONTENT_LEN = 500
    IDLE_RETENTION_DAYS = 7
    IDLE_RETENTION_CALLS = 10

    def __init__(self, db_session_factory, max_blocks: int = 20):
        self._db = db_session_factory
        self.max_blocks = max_blocks

    # ── 核心 CRUD ──────────────────────────────────────

    async def add(
        self, session_id: str, label: str, content: str,
        status: str = "active",
    ) -> dict:
        """新增一个钉选块。"""
        # 校验状态参数
        if status not in _VALID_STATUS:
            return {"ok": False, "error": f"无效状态: {status}"}

        label = label.strip()[:self.MAX_LABEL_LEN]
        content = content.strip()[:self.MAX_CONTENT_LEN]

        if not label or not content:
            return {"ok": False, "error": "标签和内容不能为空"}

        async with self._db() as session:
            # 检查数量上限
            result = await session.execute(
                text("SELECT COUNT(*) FROM pinned_blocks WHERE session_id = :sid"),
                {"sid": session_id},
            )
            count = result.scalar()
            if count >= self.max_blocks:
                return {"ok": False, "error": f"已达上限（{self.max_blocks} 个块），请先清理"}

            # 检查标签是否已存在
            result = await session.execute(
                text("SELECT id FROM pinned_blocks WHERE session_id = :sid AND label = :label"),
                {"sid": session_id, "label": label},
            )
            if result.fetchone():
                return {"ok": False, "error": f"标签'{label}'已存在，使用 /pin edit 修改或 /pin remove 删除"}

            now = datetime.now(timezone.utc).isoformat()
            await session.execute(
                text("""INSERT INTO pinned_blocks
                        (session_id, label, content, status, created_at, updated_at)
                        VALUES (:sid, :label, :content, :status, :created, :updated)"""),
                {"sid": session_id, "label": label, "content": content,
                 "status": status, "created": now, "updated": now},
            )
            await session.commit()

        logger.info(f"[钉选上下文] 新增块: session={session_id[:12]} label={label} status={status}")
        return {"ok": True, "label": label, "status": status}

    async def remove(self, session_id: str, label: str) -> dict:
        """删除指定块。"""
        async with self._db() as session:
            result = await session.execute(
                text("DELETE FROM pinned_blocks WHERE session_id = :sid AND label = :label"),
                {"sid": session_id, "label": label},
            )
            await session.commit()
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}
        logger.info(f"[钉选上下文] 删除块: session={session_id[:12]} label={label}")
        return {"ok": True, "label": label}

    async def update(self, session_id: str, label: str, content: str) -> dict:
        """编辑块内容。"""
        content = content.strip()[:self.MAX_CONTENT_LEN]
        if not content:
            return {"ok": False, "error": "内容不能为空"}

        now = datetime.now(timezone.utc).isoformat()
        async with self._db() as session:
            result = await session.execute(
                text("UPDATE pinned_blocks SET content = :content, updated_at = :updated "
                     "WHERE session_id = :sid AND label = :label"),
                {"content": content, "updated": now, "sid": session_id, "label": label},
            )
            await session.commit()
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}
        logger.info(f"[钉选上下文] 更新块: session={session_id[:12]} label={label}")
        return {"ok": True, "label": label}

    # ── 状态管理 ────────────────────────────────────────

    async def set_status(self, session_id: str, label: str, new_status: str) -> dict:
        """切换块状态：active / idle / pinned。

        进入 idle 时会重置 llm_call_count 为 0 并记录 idle_since 时间；
        离开 idle 时会清空 idle_since。
        """
        if new_status not in _VALID_STATUS:
            return {"ok": False, "error": f"无效状态: {new_status}"}

        now = datetime.now(timezone.utc).isoformat()
        async with self._db() as session:
            if new_status == "idle":
                # 进入 idle：重置计数器 + 记录时间
                result = await session.execute(
                    text("UPDATE pinned_blocks SET status = :status, idle_since = :idle_since, "
                         "llm_call_count = 0, updated_at = :updated "
                         "WHERE session_id = :sid AND label = :label"),
                    {"status": new_status, "idle_since": now, "updated": now,
                     "sid": session_id, "label": label},
                )
            else:
                # 离开 idle：清空 idle_since
                result = await session.execute(
                    text("UPDATE pinned_blocks SET status = :status, idle_since = NULL, "
                         "llm_call_count = 0, updated_at = :updated "
                         "WHERE session_id = :sid AND label = :label"),
                    {"status": new_status, "updated": now,
                     "sid": session_id, "label": label},
                )
            await session.commit()

            # 检查块是否存在
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}

        logger.info(
            f"[钉选上下文] 状态切换: session={session_id[:12]} label={label} → {new_status}"
        )
        return {"ok": True, "label": label, "status": new_status}

    # ── 查询 ────────────────────────────────────────────

    async def list_all(self, session_id: str) -> dict:
        """按状态分组列出所有块。"""
        async with self._db() as session:
            result = await session.execute(
                text("SELECT label, content, status, idle_since, priority, created_at, updated_at "
                     "FROM pinned_blocks WHERE session_id = :sid "
                     "ORDER BY priority DESC, updated_at DESC"),
                {"sid": session_id},
            )
            rows = result.fetchall()

        grouped: dict[str, list[dict]] = {"active": [], "idle": [], "pinned": []}
        for row in rows:
            grouped[row[2]].append({
                "label": row[0],
                "content": row[1],
                "status": row[2],
                "idle_since": row[3],
                "priority": row[4],
            })
        return grouped

    async def get_active(self, session_id: str) -> list[dict]:
        """获取 active 和 pinned 的块（用于注入）。"""
        async with self._db() as session:
            result = await session.execute(
                text("SELECT label, content, status, priority FROM pinned_blocks "
                     "WHERE session_id = :sid AND status IN ('active', 'pinned') "
                     "ORDER BY priority DESC, updated_at DESC"),
                {"sid": session_id},
            )
            rows = result.fetchall()
        return [
            {"label": r[0], "content": r[1], "status": r[2], "priority": r[3]}
            for r in rows
        ]

    async def count(self, session_id: str) -> int:
        async with self._db() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM pinned_blocks WHERE session_id = :sid"),
                {"sid": session_id},
            )
            return result.scalar() or 0

    # ── 闲置清理 ────────────────────────────────────────

    async def increment_call_count(self, session_id: str) -> None:
        """递增会话内所有 idle 块的 LLM 调用计数。"""
        async with self._db() as session:
            await session.execute(
                text("UPDATE pinned_blocks SET llm_call_count = llm_call_count + 1 "
                     "WHERE session_id = :sid AND status = 'idle'"),
                {"sid": session_id},
            )
            await session.commit()

    async def purge_stale_idle(self, session_id: str) -> int:
        """清理满足双条件的过期 idle 块。返回清理数量。

        双条件（同时满足才清理）：
        - 闲置天数 >= IDLE_RETENTION_DAYS
        - 进入 idle 后经过的 LLM 调用次数 >= IDLE_RETENTION_CALLS
        """
        cutoff_time = (
            datetime.now(timezone.utc) - timedelta(days=self.IDLE_RETENTION_DAYS)
        ).isoformat()

        async with self._db() as session:
            result = await session.execute(
                text("DELETE FROM pinned_blocks WHERE session_id = :sid AND status = 'idle' "
                     "AND idle_since IS NOT NULL "
                     "AND idle_since <= :cutoff "
                     "AND llm_call_count >= :min_calls"),
                {"sid": session_id, "cutoff": cutoff_time,
                 "min_calls": self.IDLE_RETENTION_CALLS},
            )
            await session.commit()
            purged = result.rowcount

        if purged:
            logger.info(f"[钉选上下文] 清理了 {purged} 个过期 idle 块 (session={session_id[:12]})")
        return purged

    async def clear(self, session_id: str) -> int:
        """清空指定会话的所有块。返回删除数量。"""
        async with self._db() as session:
            result = await session.execute(
                text("DELETE FROM pinned_blocks WHERE session_id = :sid"),
                {"sid": session_id},
            )
            await session.commit()
            deleted = result.rowcount
        if deleted:
            logger.info(f"[钉选上下文] 清空 {deleted} 个块 (session={session_id[:12]})")
        return deleted
