"""
Pinned Context Blocks — 钉选上下文块插件

让用户在对话中手动锚定关键信息作为"钉选上下文块"，
强约束注入 system prompt，软上下文作为本轮临时用户内容注入。

私聊块按会话隔离；群聊块分为发言人个人块与管理员维护的群共享块。
命令详见 README 与 /pin help。
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

from .block_manager import BlockManager
from .db import init_db
from .injector import ContextInjector


class PinnedContextPlugin(Star):
    """钉选上下文块插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.injector = ContextInjector()

        # 通过 AstrBotConfig 接收插件配置（官方推荐方式）
        self.block_mgr = BlockManager(
            db_session_factory=self._get_db,
            max_blocks=config.get("max_blocks_per_session", 20),
        )
        self.block_mgr.MAX_LABEL_LEN = config.get("max_label_length", 20)
        self.block_mgr.MAX_CONTENT_LEN = config.get("max_content_length", 500)
        self.block_mgr.IDLE_RETENTION_DAYS = config.get("idle_retention_days", 7)
        self.block_mgr.IDLE_RETENTION_CALLS = config.get("idle_retention_calls", 10)
        # 长阈值不得低于软阈值，否则会把轮次条件架空
        self.block_mgr.IDLE_HARD_RETENTION_DAYS = max(
            config.get("idle_hard_retention_days", 30),
            self.block_mgr.IDLE_RETENTION_DAYS,
        )
        self.token_warning_threshold = config.get("token_warning_threshold", 1000)
        self.enable_in_group = config.get("enable_in_group", False)
        self.group_whitelist = self._normalize_ids(config.get("group_whitelist", []))
        self.group_shared_admin_ids = self._normalize_ids(
            config.get("group_shared_admin_ids", [])
        )
        self.group_strong_user_ids = self._normalize_ids(
            config.get("group_strong_user_ids", [])
        )

    async def initialize(self):
        """初始化数据库。"""
        async with self._get_db() as session:
            await init_db(session)
        logger.info("[钉选上下文] 插件初始化完成")

    # ── 数据库会话工厂 ──────────────────────────────────

    @asynccontextmanager
    async def _get_db(self):
        """获取数据库会话（通过 AstrBot 公开 API）。"""
        # context.get_db() 返回 BaseDatabase 实例
        # BaseDatabase.get_db() 返回 AsyncSession 上下文管理器
        async with self.context.get_db().get_db() as session:
            yield session

    # ── LLM 请求钩子 ────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """清理当前作用域的 idle 块，并分别注入两级内容。"""
        personal_key, shared_key, reason = self._scope_keys(event)
        if not personal_key:
            if reason in ("no_sender", "no_group", "unknown_type"):
                logger.warning(f"[钉选上下文] 无法解析消息作用域，已跳过注入: {reason}")
            return
        personal = await self.block_mgr.prepare_injection(personal_key)
        shared = (
            await self.block_mgr.prepare_injection(shared_key) if shared_key else []
        )
        if shared_key and not self._can_write_strong(event):
            # 权限撤销后，旧 strong 块也不能继续生效。
            personal = [b for b in personal if b["level"] != "strong"]
        strong_text = self.injector.format_strong(shared, personal)
        if strong_text:
            req.system_prompt = (req.system_prompt or "") + strong_text
        context_text = self.injector.format_context(shared, personal)
        if context_text:
            req.extra_user_content_parts.append(
                TextPart(text=context_text).mark_as_temp()
            )

    # ── /pin 命令组 ─────────────────────────────────────

    @filter.command("pin")
    async def pin_command(self, event: AstrMessageEvent):
        """钉选块管理命令入口。"""
        personal_key, shared_key, reason = self._scope_keys(event)
        if not personal_key:
            yield event.plain_result(
                self._REASON_TEXT.get(reason, "钉选上下文在当前会话不可用")
            )
            return

        message_str = self._strip_command(event.message_str)

        # 解析子命令
        parts = message_str.split(maxsplit=1)
        if not parts or not parts[0]:
            yield event.plain_result(self._help_text())
            return

        subcmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        shared = subcmd == "shared"
        if shared:
            if not shared_key:
                yield event.plain_result("共享块只能在已启用的群聊中管理")
                return
            nested = rest.split(maxsplit=1)
            subcmd = nested[0].lower() if nested else "help"
            rest = nested[1] if len(nested) > 1 else ""

        try:
            text = await self._run_command(
                event, shared_key if shared else personal_key, subcmd, rest, shared
            )
        except Exception as e:
            logger.error(f"[钉选上下文] 命令执行失败: {e}", exc_info=True)
            text = "操作失败，请查看日志"

        yield event.plain_result(text)

    async def _run_command(
        self,
        event: AstrMessageEvent,
        session_id: str,
        subcmd: str,
        rest: str,
        shared: bool,
    ) -> str:
        is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
        if subcmd in ("help", ""):
            return self._help_text()
        if subcmd == "list":
            return await self._cmd_list(session_id, shared)
        if shared and not self._can_write_shared(event):
            return "只有 AstrBot 管理员或配置的共享块管理员可以修改群共享块"
        if subcmd in ("add", "add-strong", "add-context"):
            level = (
                "strong"
                if subcmd == "add-strong"
                else "context"
                if subcmd == "add-context" or is_group
                else "strong"
            )
            if (
                level == "strong"
                and is_group
                and not shared
                and not self._can_write_strong(event)
            ):
                return "你没有群个人强约束写入权限"
            return await self._cmd_add(session_id, rest, level)
        if subcmd == "level":
            parts = rest.split()
            if len(parts) != 2 or parts[1] not in ("strong", "context"):
                return "用法: /pin level <标签> strong|context"
            if (
                parts[1] == "strong"
                and is_group
                and not shared
                and not self._can_write_strong(event)
            ):
                return "你没有群个人强约束写入权限"
            result = await self.block_mgr.set_level(session_id, parts[0], parts[1])
            return (
                f"已将 {parts[0]} 设为 {parts[1]}"
                if result["ok"]
                else f"{result['error']}"
            )
        if subcmd == "remove":
            return await self._cmd_remove(session_id, rest)
        if subcmd == "edit":
            if is_group and not shared and not self._can_write_strong(event):
                label = rest.split(maxsplit=1)[0] if rest.strip() else ""
                if (
                    label
                    and await self.block_mgr.get_level(session_id, label) == "strong"
                ):
                    return "你没有群个人强约束写入权限"
            return await self._cmd_edit(session_id, rest)
        if subcmd == "idle":
            return await self._cmd_idle(session_id, rest)
        if subcmd == "activate":
            return await self._cmd_activate(session_id, rest)
        if subcmd == "pin":
            return await self._cmd_pin(session_id, rest)
        if subcmd == "unpin":
            return await self._cmd_unpin(session_id, rest)
        if subcmd == "clear":
            return await self._cmd_clear(session_id, rest, shared)
        return f"未知子命令: {subcmd}\n\n" + self._help_text()

    # ── 子命令实现 ──────────────────────────────────────

    async def _cmd_add(self, session_id: str, rest: str, level: str) -> str:
        """新增块。"""
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "用法: /pin add <标签> <内容> [--pinned]"

        label, content = parts[0].strip(), parts[1].strip()
        pinned = False
        if content.startswith("-- "):
            # -- 后面的内容全部视为正文，可写入字面的尾部 --pinned。
            content = content[3:].strip()
        else:
            match = re.search(r"\s--pinned\s*$", content)
            if match:
                pinned = True
                content = content[: match.start()].strip()
        status = "pinned" if pinned else "active"
        result = await self.block_mgr.add(
            session_id, label, content, status=status, level=level
        )
        if not result["ok"]:
            return f"{result['error']}"
        return (
            f"已钉选: 【{result['label']}】（{level} / {status}）"
            + self._truncation_note(result)
        )

    async def _cmd_list(self, session_id: str, shared: bool = False) -> str:
        """列出所有块。"""
        grouped = await self.block_mgr.list_all(session_id)
        total = sum(len(v) for v in grouped.values())
        if total == 0:
            return "当前没有任何钉选块。使用 /pin add <标签> <内容> 创建。"

        scope = "群共享" if shared else "个人"
        lines = [f"{scope}钉选块（共 {total} 个）\n"]
        known = ("pinned", "active", "idle")
        # 未知 status 也要列出来，否则用户看不到、也就删不掉
        extra = sorted(set(grouped) - set(known))

        for status in (*known, *extra):
            blocks = grouped.get(status) or []
            if not blocks:
                continue
            lines.append(f"{status}（{len(blocks)} 个）:")
            for b in blocks:
                content_preview = b["content"][:60] + (
                    "..." if len(b["content"]) > 60 else ""
                )
                lines.append(f"  【{b['label']}】[{b['level']}] {content_preview}")
            lines.append("")

        # 检查 token 警告（使用配置的阈值）
        active_blocks = [
            b for status in ("active", "pinned") for b in (grouped.get(status) or [])
        ]
        if active_blocks and self.token_warning_threshold > 0:
            # 按当前作用域的两级真实格式估算；中文按约 1 token/字，
            # 其他字符按约 1 token/4 字符。它是提醒，不是硬限制。
            group_blocks = active_blocks if shared else []
            personal_blocks = [] if shared else active_blocks
            text = self.injector.format_strong(group_blocks, personal_blocks)
            text += self.injector.format_context(group_blocks, personal_blocks)
            cjk = sum(1 for c in text if "一" <= c <= "鿿")
            est_tokens = cjk + (len(text) - cjk) // 4
            if est_tokens > self.token_warning_threshold:
                lines.append(f"估算注入 token: ~{est_tokens}，建议停用不常用的块。")

        return "\n".join(lines)

    async def _cmd_remove(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin remove <标签>"
        result = await self.block_mgr.remove(session_id, label)
        return f"已删除: {label}" if result["ok"] else f"{result['error']}"

    async def _cmd_edit(self, session_id: str, rest: str) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "用法: /pin edit <标签> <新内容>"
        label, content = parts[0].strip(), parts[1].strip()
        result = await self.block_mgr.update(session_id, label, content)
        if not result["ok"]:
            return f"{result['error']}"
        return f"已更新: {label}" + self._truncation_note(result)

    async def _cmd_idle(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin idle <标签>"
        result = await self.block_mgr.set_status(session_id, label, "idle")
        return f"已停用: {label}" if result["ok"] else f"{result['error']}"

    async def _cmd_activate(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin activate <标签>"
        result = await self.block_mgr.set_status(session_id, label, "active")
        return f"已激活: {label}" if result["ok"] else f"{result['error']}"

    async def _cmd_pin(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin pin <标签>"
        result = await self.block_mgr.set_status(session_id, label, "pinned")
        return f"已锚定: {label}" if result["ok"] else f"{result['error']}"

    async def _cmd_unpin(self, session_id: str, label: str) -> str:
        if not label:
            return "用法: /pin unpin <标签>"
        result = await self.block_mgr.set_status(session_id, label, "active")
        return f"已取消锚定: {label}" if result["ok"] else f"{result['error']}"

    async def _cmd_clear(
        self, session_id: str, confirm: str = "", shared: bool = False
    ) -> str:
        """清空指定作用域的全部块，需显式输入 confirm。"""
        grouped = await self.block_mgr.list_all(session_id)
        total = sum(len(v) for v in grouped.values())
        pinned_count = len(grouped.get("pinned", []))

        if confirm.strip().lower() != "confirm":
            scope = "本群共享" if shared else "你的"
            hint = f"将删除{scope}全部 {total} 个钉选块"
            if pinned_count > 0:
                hint += f"（其中含 {pinned_count} 个pinned 块）"
            hint += "。输入 /pin clear confirm 确认。"
            return hint

        count = await self.block_mgr.clear(session_id)
        return f"已清空 {count} 个钉选块。"

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

    # _scope_keys 返回空个人键时的原因 → 用户提示
    _REASON_TEXT = {
        "group_disabled": "钉选上下文在群聊中未启用",
        "group_not_allowed": "本群不在钉选上下文的允许列表中",
        "no_sender": "无法识别发送者，钉选在当前会话不可用",
        "no_umo": "无法识别当前会话",
        "no_group": "无法识别群 ID，已拒绝群聊操作",
        "unknown_type": "无法识别消息类型",
    }

    @staticmethod
    def _truncation_note(result: dict) -> str:
        """把 BlockManager 回报的截断事实拼成用户提示；未截断则返回空串。"""
        truncated = result.get("truncated") or []
        if not truncated:
            return ""
        return f"\n{'、'.join(truncated)}超出长度上限，已截断保存"

    @staticmethod
    def _strip_command(raw: str) -> str:
        """剥掉命令名 "pin" 本身，返回剩余参数文本。

        CommandFilter 是拿 `re.sub(r"\\s+", " ", ...)` 归一化**之后**的文本
        做匹配的（astrbot/core/star/filter/command.py:199），所以 "pin\\nadd ..."
        这类消息能通过过滤器；但它只把归一化结果留在局部变量里，不会写回
        event，所以插件这边必须做等价处理，否则会落到「未知子命令」。

        这里用 `\\b\\s*` 吃掉命令名及其后的任意分隔符（含换行），
        而**不**整体折叠空白——内容里的换行是有意义的，要原样保留。
        """
        m = re.match(r"(?i)pin\b\s*(.*)", raw.strip(), re.S)
        return m.group(1) if m else raw.strip()

    @staticmethod
    def _normalize_ids(raw) -> set[str]:
        """归一化 ID 列表配置为字符串集合。

        WebUI 里数字群号可能被反序列化成 int，也可能整体被写成逗号分隔
        字符串，不归一化会静默匹配不上。
        """
        if raw is None:
            return set()
        if isinstance(raw, str | int):
            raw = re.split(r"[,，]", str(raw))
        return {str(item).strip() for item in raw if str(item).strip()}

    def _scope_keys(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """返回个人键、群共享键和作用域；信息缺失时拒绝操作。"""
        umo = event.unified_msg_origin or ""
        if not umo:
            return "", "", "no_umo"

        message_type = event.get_message_type()
        if message_type == MessageType.FRIEND_MESSAGE:
            if str(event.get_group_id() or "").strip():
                return "", "", "unknown_type"
            return umo, "", "private"
        if message_type != MessageType.GROUP_MESSAGE:
            return "", "", "unknown_type"
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return "", "", "no_group"
        if not self.enable_in_group:
            return "", "", "group_disabled"
        if (
            self.group_whitelist
            and group_id not in self.group_whitelist
            and umo not in self.group_whitelist
        ):
            return "", "", "group_not_allowed"

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return "", "", "no_sender"

        return f"{umo}::u:{sender_id}", f"{umo}::scope:shared", "group"

    def _is_astrbot_admin(self, event: AstrMessageEvent) -> bool:
        return callable(getattr(event, "is_admin", None)) and event.is_admin()

    def _can_write_shared(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "").strip()
        return bool(sender) and (
            self._is_astrbot_admin(event)
            or sender in self.group_shared_admin_ids
            or f"{event.unified_msg_origin}::u:{sender}" in self.group_shared_admin_ids
        )

    def _can_write_strong(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "").strip()
        return bool(sender) and (
            self._can_write_shared(event)
            or sender in self.group_strong_user_ids
            or f"{event.unified_msg_origin}::u:{sender}" in self.group_strong_user_ids
        )

    def _session_key(self, event: AstrMessageEvent) -> tuple[str, str]:
        """兼容原有会话键查询入口。"""
        personal_key, _, reason = self._scope_keys(event)
        return personal_key, reason

    def _help_text(self) -> str:
        mgr = self.block_mgr
        text = f"""钉选上下文块 — 命令帮助

/pin add <标签> <内容>            新增个人块（私聊 strong，群聊 context）
/pin add-strong <标签> <内容>     新增强约束（群聊需授权）
/pin add-context <标签> <内容>    新增软上下文
/pin add <标签> <内容> --pinned   新增为长期保留块
/pin level <标签> strong|context 切换注入级别
/pin shared <子命令>             管理群共享块（写入需管理员权限）
/pin list                          列出个人块（含级别与状态）
/pin remove <标签>                 删除块
/pin edit <标签> <新内容>          编辑块
/pin idle <标签>                   停用（→ 闲置，后续自动清理）
/pin activate <标签>               重新激活闲置块
/pin pin <标签>                    长期锚定（永不过期）
/pin unpin <标签>                  取消锚定
/pin clear                         清空你的全部块（二次确认）

状态说明：
active  正常注入，不会被自动清理
idle    不注入，闲置 {mgr.IDLE_RETENTION_DAYS} 天且又过 {mgr.IDLE_RETENTION_CALLS} 轮，
           或闲置满 {mgr.IDLE_HARD_RETENTION_DAYS} 天后自动删除
pinned  长期锚定，永不过期，仅手动删除"""
        if self.enable_in_group:
            text += (
                "\n\n群个人块仅在本人发言时注入；群共享块对全群生效。"
                "群内回复可能公开个人块的信息，请勿存放秘密。"
            )
        return text
