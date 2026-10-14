"""
钉选块的 CRUD、状态管理、闲置清理。

注意：AstrBot 使用 SQLAlchemy >= 2.0，所有原生 SQL 必须用 text() 包裹，
参数使用 :paramname 命名占位符（非 ? 位置占位符）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from astrbot.api import logger
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# 合法的块状态
_VALID_STATUS = ("active", "idle", "pinned")
_VALID_LEVEL = ("strong", "context")

# prepare_injection 的三条语句集中在此，避免同一段语义在多处漂移。
_SQL_BUMP_IDLE = (
    "UPDATE pinned_blocks SET llm_call_count = llm_call_count + 1 "
    "WHERE session_id = :sid AND status = 'idle'"
)
_SQL_PURGE_IDLE = (
    "DELETE FROM pinned_blocks WHERE session_id = :sid AND status = 'idle' "
    "AND idle_since IS NOT NULL "
    "AND ("
    "     (idle_since <= :cutoff AND llm_call_count >= :min_calls)"
    "  OR idle_since <= :hard_cutoff"
    ")"
)
# 顺序即注入顺序：后钉的靠前（冲突时视为更新的需求，优先级更高）。
# 用 created_at 而非 updated_at，保证 /pin edit 改内容不会让块改变裁决位次。
# priority 列已废弃不用（见 db.py 注释）。
_SQL_SELECT_ACTIVE = (
    "SELECT label, content, status, level FROM pinned_blocks "
    "WHERE session_id = :sid AND status IN ('active', 'pinned') "
    "ORDER BY created_at DESC, id DESC"
)


class BlockManager:
    """管理钉选上下文块的生命周期。"""

    MAX_LABEL_LEN = 20
    MAX_CONTENT_LEN = 500
    IDLE_RETENTION_DAYS = 7
    IDLE_RETENTION_CALLS = 10
    # 长阈值：idle 超过此天数后，无视 IDLE_RETENTION_CALLS 直接清理。
    # 用于兜住"手动停用后长期不再使用"的块——它们等不到足够的调用轮次。
    IDLE_HARD_RETENTION_DAYS = 30

    def __init__(self, db_session_factory, max_blocks: int = 20):
        self._db = db_session_factory
        self.max_blocks = max_blocks

    @staticmethod
    def _tag(session_id: str) -> str:
        """会话键的日志标识。

        群聊下键形如 `<umo>::u:<sender_id>`，可变部分在**尾部**，
        前缀截断会把同群的不同成员显示成同一个串，故保留尾部。
        """
        return session_id if len(session_id) <= 48 else "…" + session_id[-48:]

    # ── 核心 CRUD ──────────────────────────────────────

    async def add(
        self,
        session_id: str,
        label: str,
        content: str,
        status: str = "active",
        level: str = "strong",
    ) -> dict:
        """新增一个钉选块。"""
        # 校验状态参数
        if status not in _VALID_STATUS:
            return {"ok": False, "error": f"无效状态: {status}"}
        if level not in _VALID_LEVEL:
            return {"ok": False, "error": f"无效级别: {level}"}

        stripped_label, stripped_content = label.strip(), content.strip()
        # 静默截断会让用户以为存进去的是全文，这里把截断事实回报给调用方
        truncated = []
        if len(stripped_label) > self.MAX_LABEL_LEN:
            truncated.append("标签")
        if len(stripped_content) > self.MAX_CONTENT_LEN:
            truncated.append("内容")
        label = stripped_label[: self.MAX_LABEL_LEN]
        content = stripped_content[: self.MAX_CONTENT_LEN]

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
                return {
                    "ok": False,
                    "error": f"已达上限（{self.max_blocks} 个块），请先清理",
                }

            # 检查标签是否已存在
            result = await session.execute(
                text(
                    "SELECT id FROM pinned_blocks WHERE session_id = :sid AND label = :label"
                ),
                {"sid": session_id, "label": label},
            )
            if result.fetchone():
                return {
                    "ok": False,
                    "error": f"标签'{label}'已存在，使用 /pin edit 修改或 /pin remove 删除",
                }

            now = datetime.now(timezone.utc).isoformat()
            try:
                inserted = await session.execute(
                    text("""INSERT INTO pinned_blocks
                        (session_id, label, content, status, level, created_at, updated_at)
                        SELECT :sid, :label, :content, :status, :level, :created, :updated
                        WHERE (SELECT COUNT(*) FROM pinned_blocks WHERE session_id = :sid)
                              < :max_blocks"""),
                    {
                        "sid": session_id,
                        "label": label,
                        "content": content,
                        "status": status,
                        "level": level,
                        "created": now,
                        "updated": now,
                        "max_blocks": self.max_blocks,
                    },
                )
            except IntegrityError:
                await session.rollback()
                return {"ok": False, "error": f"标签'{label}'已存在"}
            if inserted.rowcount == 0:
                return {
                    "ok": False,
                    "error": f"已达上限（{self.max_blocks} 个块），请先清理",
                }
            await session.commit()

        logger.info(
            f"[钉选上下文] 新增块: session={self._tag(session_id)} label={label} status={status}"
        )
        return {
            "ok": True,
            "label": label,
            "status": status,
            "level": level,
            "truncated": truncated,
        }

    async def remove(self, session_id: str, label: str) -> dict:
        """删除指定块。"""
        async with self._db() as session:
            result = await session.execute(
                text(
                    "DELETE FROM pinned_blocks WHERE session_id = :sid AND label = :label"
                ),
                {"sid": session_id, "label": label},
            )
            await session.commit()
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}
        logger.info(
            f"[钉选上下文] 删除块: session={self._tag(session_id)} label={label}"
        )
        return {"ok": True, "label": label}

    async def update(self, session_id: str, label: str, content: str) -> dict:
        """编辑块内容。"""
        stripped = content.strip()
        truncated = ["内容"] if len(stripped) > self.MAX_CONTENT_LEN else []
        content = stripped[: self.MAX_CONTENT_LEN]
        if not content:
            return {"ok": False, "error": "内容不能为空"}

        now = datetime.now(timezone.utc).isoformat()
        async with self._db() as session:
            result = await session.execute(
                text(
                    "UPDATE pinned_blocks SET content = :content, updated_at = :updated "
                    "WHERE session_id = :sid AND label = :label"
                ),
                {"content": content, "updated": now, "sid": session_id, "label": label},
            )
            await session.commit()
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}
        logger.info(
            f"[钉选上下文] 更新块: session={self._tag(session_id)} label={label}"
        )
        return {"ok": True, "label": label, "truncated": truncated}

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
                    text(
                        "UPDATE pinned_blocks SET status = :status, idle_since = :idle_since, "
                        "llm_call_count = 0, updated_at = :updated "
                        "WHERE session_id = :sid AND label = :label"
                    ),
                    {
                        "status": new_status,
                        "idle_since": now,
                        "updated": now,
                        "sid": session_id,
                        "label": label,
                    },
                )
            else:
                # 离开 idle：清空 idle_since
                result = await session.execute(
                    text(
                        "UPDATE pinned_blocks SET status = :status, idle_since = NULL, "
                        "llm_call_count = 0, updated_at = :updated "
                        "WHERE session_id = :sid AND label = :label"
                    ),
                    {
                        "status": new_status,
                        "updated": now,
                        "sid": session_id,
                        "label": label,
                    },
                )
            await session.commit()

            # 检查块是否存在
            if result.rowcount == 0:
                return {"ok": False, "error": f"块'{label}'不存在"}

        logger.info(
            f"[钉选上下文] 状态切换: session={self._tag(session_id)} label={label} → {new_status}"
        )
        return {"ok": True, "label": label, "status": new_status}

    async def set_level(self, session_id: str, label: str, level: str) -> dict:
        """切换注入级别，不改变生命周期状态。"""
        if level not in _VALID_LEVEL:
            return {"ok": False, "error": f"无效级别: {level}"}
        async with self._db() as session:
            result = await session.execute(
                text(
                    "UPDATE pinned_blocks SET level = :level, updated_at = :updated "
                    "WHERE session_id = :sid AND label = :label"
                ),
                {
                    "level": level,
                    "updated": datetime.now(timezone.utc).isoformat(),
                    "sid": session_id,
                    "label": label,
                },
            )
            await session.commit()
        if result.rowcount == 0:
            return {"ok": False, "error": f"块'{label}'不存在"}
        return {"ok": True, "label": label, "level": level}

    async def get_level(self, session_id: str, label: str) -> str | None:
        async with self._db() as session:
            result = await session.execute(
                text(
                    "SELECT level FROM pinned_blocks WHERE session_id = :sid "
                    "AND label = :label"
                ),
                {"sid": session_id, "label": label},
            )
            row = result.fetchone()
        return row[0] if row else None

    # ── 查询 ────────────────────────────────────────────

    async def list_all(self, session_id: str) -> dict:
        """按状态分组列出所有块。

        组内按 created_at 倒序，与注入顺序一致——列表最上面的就是
        冲突时优先的那一条。
        """
        async with self._db() as session:
            result = await session.execute(
                text(
                    "SELECT label, content, status, idle_since, level FROM pinned_blocks "
                    "WHERE session_id = :sid "
                    "ORDER BY created_at DESC, id DESC"
                ),
                {"sid": session_id},
            )
            rows = result.fetchall()

        grouped: dict[str, list[dict]] = {"active": [], "idle": [], "pinned": []}
        for row in rows:
            # 未知 status（旧版本残留、手工改库）不应让 /pin list 直接抛异常。
            # 落到以原 status 命名的桶里，让用户看得见、也能 /pin remove 掉。
            grouped.setdefault(row[2], []).append(
                {
                    "label": row[0],
                    "content": row[1],
                    "status": row[2],
                    "idle_since": row[3],
                    "level": row[4],
                }
            )
        return grouped

    async def count(self, session_id: str) -> int:
        async with self._db() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM pinned_blocks WHERE session_id = :sid"),
                {"sid": session_id},
            )
            return result.scalar() or 0

    # ── 注入前处理 ──────────────────────────────────────

    def _purge_params(self, session_id: str) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "sid": session_id,
            "cutoff": (now - timedelta(days=self.IDLE_RETENTION_DAYS)).isoformat(),
            "min_calls": self.IDLE_RETENTION_CALLS,
            "hard_cutoff": (
                now - timedelta(days=self.IDLE_HARD_RETENTION_DAYS)
            ).isoformat(),
        }

    async def prepare_injection(self, session_id: str) -> list[dict]:
        """单事务完成：递增 idle 计数 → 清理过期 idle → 取回待注入块。

        三步在一个会话里按序执行：计数必须先于清理累加（否则新一轮
        永远差一次），合并则把每次 LLM 调用的 3 次数据库往返降为 1 次。

        清理条件（满足其一）：
        - 双条件：闲置 >= IDLE_RETENTION_DAYS 天 且 又过了 >= IDLE_RETENTION_CALLS 轮
        - 长阈值：闲置 >= IDLE_HARD_RETENTION_DAYS 天（无视轮次）

        注意：本方法按**当前会话键**调用，所以长阈值只在主人再次发言时
        生效——它是"回来时顺手清掉陈旧块"，不是定时清理。长期不再出现的
        会话，其遗留行不会被这里清掉。
        """
        async with self._db() as session:
            await session.execute(text(_SQL_BUMP_IDLE), {"sid": session_id})
            result = await session.execute(
                text(_SQL_PURGE_IDLE), self._purge_params(session_id)
            )
            purged = result.rowcount
            result = await session.execute(
                text(_SQL_SELECT_ACTIVE), {"sid": session_id}
            )
            rows = result.fetchall()
            await session.commit()

        if purged:
            logger.info(
                f"[钉选上下文] 清理了 {purged} 个过期 idle 块 (session={self._tag(session_id)})"
            )
        return [
            {"label": r[0], "content": r[1], "status": r[2], "level": r[3]}
            for r in rows
        ]

    # ── 清空 ────────────────────────────────────────────

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
            logger.info(
                f"[钉选上下文] 清空 {deleted} 个块 (session={self._tag(session_id)})"
            )
        return deleted
