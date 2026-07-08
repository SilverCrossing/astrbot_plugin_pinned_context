"""
钉选块的 CRUD、状态管理、闲置清理。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("astrbot.pinned_context")


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
        label = label.strip()[:self.MAX_LABEL_LEN]
        content = content.strip()[:self.MAX_CONTENT_LEN]

        if not label or not content:
            return {"ok": False, "error": "标签和内容不能为空"}

        async with self._db() as session:
            # 检查数量上限
            result = await session.execute(
                "SELECT COUNT(*) FROM pinned_blocks WHERE session_id = ?",
                (session_id,),
            )
            count = result.scalar()
            if count >= self.max_blocks:
                return {"ok": False, "error": f"已达上限（{self.max_blocks} 个块），请先清理"}

            # 检查标签是否已存在
            result = await session.execute(
                "SELECT id FROM pinned_blocks WHERE session_id = ? AND label = ?",
                (session_id, label),
            )
            if result.fetchone():
                return {"ok": False, "error": f"标签'{label}'已存在，使用 /pin edit 修改或 /pin remove 删除"}

            now = datetime.now(timezone.utc).isoformat()
            await session.execute(
                """INSERT INTO pinned_blocks (session_id, label, content, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, label, content, status, now, now),
            )
            await session.commit()

        logger.info(f"[钉选上下文] 新增块: session={session_id[:12]} label={label} status={status}")
        return {"ok": True, "label": label, "status": status}

    async def remove(self, session_id: str, label: str) -> dict:
        """删除指定块。"""
        async with self._db() as session:
            result = await session.execute(
                "DELETE FROM pinned_blocks WHERE session_id = ? AND label = ?",
                (session_id, label),
            )
            await session.commit()
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}
        return {"ok": True, "label": label}

    async def update(self, session_id: str, label: str, content: str) -> dict:
        """编辑块内容。"""
        content = content.strip()[:self.MAX_CONTENT_LEN]
        if not content:
            return {"ok": False, "error": "内容不能为空"}

        now = datetime.now(timezone.utc).isoformat()
        async with self._db() as session:
            result = await session.execute(
                "UPDATE pinned_blocks SET content = ?, updated_at = ? WHERE session_id = ? AND label = ?",
                (content, now, session_id, label),
            )
            await session.commit()
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}
        return {"ok": True, "label": label}

    # ── 状态管理 ────────────────────────────────────────

    async def set_status(self, session_id: str, label: str, new_status: str) -> dict:
        """切换块状态：active / idle / pinned。"""
        if new_status not in ("active", "idle", "pinned"):
            return {"ok": False, "error": f"无效状态: {new_status}"}

        now = datetime.now(timezone.utc).isoformat()
        async with self._db() as session:
            if new_status == "idle":
                await session.execute(
                    "UPDATE pinned_blocks SET status = ?, idle_since = ?, updated_at = ? "
                    "WHERE session_id = ? AND label = ?",
                    (new_status, now, now, session_id, label),
                )
            else:
                await session.execute(
                    "UPDATE pinned_blocks SET status = ?, idle_since = NULL, updated_at = ? "
                    "WHERE session_id = ? AND label = ?",
                    (new_status, now, session_id, label),
                )
            await session.commit()

        return {"ok": True, "label": label, "status": new_status}

    # ── 查询 ────────────────────────────────────────────

    async def list_all(self, session_id: str) -> dict:
        """按状态分组列出所有块。"""
        async with self._db() as session:
            result = await session.execute(
                "SELECT label, content, status, idle_since, priority, created_at, updated_at "
                "FROM pinned_blocks WHERE session_id = ? ORDER BY priority DESC, updated_at DESC",
                (session_id,),
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
                "SELECT label, content, status, priority FROM pinned_blocks "
                "WHERE session_id = ? AND status IN ('active', 'pinned') "
                "ORDER BY priority DESC, updated_at DESC",
                (session_id,),
            )
            rows = result.fetchall()
        return [
            {"label": r[0], "content": r[1], "status": r[2], "priority": r[3]}
            for r in rows
        ]

    async def count(self, session_id: str) -> int:
        async with self._db() as session:
            result = await session.execute(
                "SELECT COUNT(*) FROM pinned_blocks WHERE session_id = ?",
                (session_id,),
            )
            return result.scalar() or 0

    # ── 闲置清理 ────────────────────────────────────────

    async def increment_call_count(self, session_id: str) -> None:
        """递增会话的 LLM 调用计数（存储在 idle_since 不为空的块上）。"""
        async with self._db() as session:
            await session.execute(
                "UPDATE pinned_blocks SET llm_call_count = llm_call_count + 1 "
                "WHERE session_id = ? AND status = 'idle'",
                (session_id,),
            )
            await session.commit()

    async def purge_stale_idle(self, session_id: str) -> int:
        """清理满足双条件的过期 idle 块。返回清理数量。"""
        cutoff_time = (
            datetime.now(timezone.utc) - timedelta(days=self.IDLE_RETENTION_DAYS)
        ).isoformat()

        async with self._db() as session:
            result = await session.execute(
                "DELETE FROM pinned_blocks WHERE session_id = ? AND status = 'idle' "
                "AND idle_since IS NOT NULL "
                "AND idle_since <= ? "
                "AND llm_call_count >= ?",
                (session_id, cutoff_time, self.IDLE_RETENTION_CALLS),
            )
            await session.commit()
            purged = result.rowcount

        if purged:
            logger.info(f"[钉选上下文] 清理了 {purged} 个过期 idle 块 (session={session_id[:12]})")
        return purged

    async def clear(self, session_id: str) -> int:
        """清空所有块。"""
        async with self._db() as session:
            result = await session.execute(
                "DELETE FROM pinned_blocks WHERE session_id = ?", (session_id,),
            )
            await session.commit()
            return result.rowcount
