"""
Pinned Context Blocks — 钉选上下文块插件

让用户在对话中手动锚定关键信息作为"钉选上下文块"，
这些块会被注入到每次 LLM 调用的 system prompt 末尾，
不会被滑动窗口截断。

命令：
  /pin add <标签> <内容>           新增块（默认 active）
  /pin add <标签> <内容> --pinned  新增为长期锚定块
  /pin list                        列出所有块（按状态分组）
  /pin remove <标签>               删除块
  /pin edit <标签> <新内容>        编辑块
  /pin idle <标签>                 停用块（→ idle）
  /pin activate <标签>             重新激活（idle → active）
  /pin pin <标签>                  锚定（→ pinned，永不过期）
  /pin unpin <标签>                取消锚定（pinned → active）
  /pin clear                       清空全部（需二次确认）
  /pin help                        显示帮助
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .block_manager import BlockManager
from .db import init_db
from .injector import ContextInjector


class PinnedContextPlugin(Star):
    """钉选上下文块插件。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.injector = ContextInjector()

        # 从配置中读取参数
        cfg = context.get_config() or {}
        self.block_mgr = BlockManager(
            db_session_factory=self._get_db,
            max_blocks=cfg.get("max_blocks_per_session", 20),
        )
        self.block_mgr.MAX_LABEL_LEN = cfg.get("max_label_length", 20)
        self.block_mgr.MAX_CONTENT_LEN = cfg.get("max_content_length", 500)
        self.block_mgr.IDLE_RETENTION_DAYS = cfg.get("idle_retention_days", 7)
        self.block_mgr.IDLE_RETENTION_CALLS = cfg.get("idle_retention_calls", 10)

    async def initialize(self):
        """初始化数据库。"""
        async with self._get_db() as session:
            await init_db(session)
        logger.info("[钉选上下文] 插件初始化完成")

    # ── 数据库会话工厂 ──────────────────────────────────

    @asynccontextmanager
    async def _get_db(self):
        """获取数据库会话（AstrBot SQLAlchemy）。"""
        async with self.context._db.get_db() as session:
            yield session

    # ── LLM 请求钩子 ────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每次 LLM 调用前：清理过期 + 注入钉选块。"""
        session_id = req.session_id or ""
        if not session_id:
            return

        # 1. 递增调用计数 + 清理过期 idle 块
        await self.block_mgr.increment_call_count(session_id)
        await self.block_mgr.purge_stale_idle(session_id)

        # 2. 获取 active + pinned 块
        blocks = await self.block_mgr.get_active(session_id)

        # 3. 注入到 system prompt 末尾
        if blocks:
            req.system_prompt = self.injector.inject(
                req.system_prompt or "", blocks
            )

    # ── /pin 命令组 ─────────────────────────────────────

    @filter.command("pin")
    async def pin_command(self, event: AstrMessageEvent):
        """钉选块管理命令入口。"""
        message_str = event.message_str.strip()
        session_id = event.session_id or ""

        # 解析子命令
        parts = message_str.split(maxsplit=1)
        if len(parts) < 1:
            yield event.plain_result(self._help_text())
            return

        subcmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        try:
            if subcmd == "add":
                text = await self._cmd_add(session_id, rest)
            elif subcmd == "list":
                text = await self._cmd_list(session_id)
            elif subcmd == "remove":
                text = await self._cmd_remove(session_id, rest)
            elif subcmd == "edit":
                text = await self._cmd_edit(session_id, rest)
            elif subcmd == "idle":
                text = await self._cmd_idle(session_id, rest)
            elif subcmd == "activate":
                text = await self._cmd_activate(session_id, rest)
            elif subcmd == "pin":
                text = await self._cmd_pin(session_id, rest)
            elif subcmd == "unpin":
                text = await self._cmd_unpin(session_id, rest)
            elif subcmd == "clear":
                text = await self._cmd_clear(session_id, rest)
            elif subcmd == "help":
                text = self._help_text()
            else:
                text = f"未知子命令: {subcmd}\n\n" + self._help_text()
        except Exception as e:
            logger.error(f"[钉选上下文] 命令执行失败: {e}", exc_info=True)
            text = f"操作失败: {e}"

        yield event.plain_result(text)

    # ── 子命令实现 ──────────────────────────────────────

    async def _cmd_add(self, session_id: str, rest: str) -> str:
        """新增块。"""
        pinned = False
        if rest.endswith("--pinned"):
            pinned = True
            rest = rest[: -len("--pinned")].strip()

        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "用法: /pin add <标签> <内容> [--pinned]"

        label, content = parts[0].strip(), parts[1].strip()
        status = "pinned" if pinned else "active"
        result = await self.block_mgr.add(session_id, label, content, status=status)
        if result["ok"]:
            return f"✅ 已钉选: 【{label}】（{status}）"
        return f"❌ {result['error']}"

    async def _cmd_list(self, session_id: str) -> str:
        """列出所有块。"""
        grouped = await self.block_mgr.list_all(session_id)
        total = sum(len(v) for v in grouped.values())
        if total == 0:
            return "当前没有任何钉选块。使用 /pin add <标签> <内容> 创建。"

        lines = [f"📌 钉选上下文块（共 {total} 个）\n"]
        icons = {"active": "🟢", "idle": "💤", "pinned": "🔒"}

        for status in ("pinned", "active", "idle"):
            blocks = grouped.get(status, [])
            if not blocks:
                continue
            lines.append(f"{icons[status]} {status}（{len(blocks)} 个）:")
            for b in blocks:
                content_preview = b["content"][:60] + ("..." if len(b["content"]) > 60 else "")
                lines.append(f"  【{b['label']}】{content_preview}")
            lines.append("")

        # 检查 token 警告
        active_count = len(grouped["active"]) + len(grouped["pinned"])
        if active_count > 0:
            est_chars = sum(
                len(b["content"]) + len(b["label"]) + 25
                for status in ("active", "pinned")
                for b in grouped[status]
            )
            est_tokens = est_chars * 2  # 中文 ≈ 2tokens/字
            if est_tokens > 1000:
                lines.append(f"⚠️ 估算注入 token: ~{est_tokens}，建议停用不常用的块。")

        return "\n".join(lines)

    async def _cmd_remove(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin remove <标签>"
        result = await self.block_mgr.remove(session_id, label)
        return f"✅ 已删除: {label}" if result["ok"] else f"❌ {result['error']}"

    async def _cmd_edit(self, session_id: str, rest: str) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "用法: /pin edit <标签> <新内容>"
        label, content = parts[0].strip(), parts[1].strip()
        result = await self.block_mgr.update(session_id, label, content)
        return f"✅ 已更新: {label}" if result["ok"] else f"❌ {result['error']}"

    async def _cmd_idle(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin idle <标签>"
        result = await self.block_mgr.set_status(session_id, label, "idle")
        return f"💤 已停用: {label}" if result["ok"] else f"❌ {result['error']}"

    async def _cmd_activate(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin activate <标签>"
        result = await self.block_mgr.set_status(session_id, label, "active")
        return f"🟢 已激活: {label}" if result["ok"] else f"❌ {result['error']}"

    async def _cmd_pin(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin pin <标签>"
        result = await self.block_mgr.set_status(session_id, label, "pinned")
        return f"🔒 已锚定: {label}" if result["ok"] else f"❌ {result['error']}"

    async def _cmd_unpin(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin unpin <标签>"
        result = await self.block_mgr.set_status(session_id, label, "active")
        return f"🟢 已取消锚定: {label}" if result["ok"] else f"❌ {result['error']}"

    async def _cmd_clear(self, session_id: str, confirm: str = "") -> str:
        if confirm != "confirm":
            total = await self.block_mgr.count(session_id)
            return f"⚠️ 将删除全部 {total} 个钉选块。输入 /pin clear confirm 确认。"
        count = await self.block_mgr.clear(session_id)
        return f"✅ 已清空 {count} 个钉选块。"

    # ── 预留扩展点 ──────────────────────────────────────
    # 未来如需 LLM 辅助管理块：
    # @filter.llm_tool(name="manage_pin")
    # async def manage_pin(self, event, action, label, content=""):
    #     """LLM 建议钉选操作，写入 pending 状态。"""
    #     ...

    # ── 生命周期 ────────────────────────────────────────

    async def terminate(self):
        logger.info("[钉选上下文] 插件已停用")

    # ── 工具方法 ────────────────────────────────────────

    @staticmethod
    def _help_text() -> str:
        return """📌 钉选上下文块 — 命令帮助

/pin add <标签> <内容>            新增块（默认 active）
/pin add <标签> <内容> --pinned   新增为长期锚定块
/pin list                          列出所有块（按状态分组）
/pin remove <标签>                 删除块
/pin edit <标签> <新内容>          编辑块
/pin idle <标签>                   停用（→ 闲置，后续自动清理）
/pin activate <标签>               重新激活闲置块
/pin pin <标签>                    长期锚定（永不过期）
/pin unpin <标签>                  取消锚定
/pin clear                         清空全部（需二次确认）

状态说明：
🟢 active  正常注入，不会被自动清理
💤 idle    不注入，闲置 7 天+10 轮后自动删除
🔒 pinned  长期锚定，永不过期，仅手动删除"""
