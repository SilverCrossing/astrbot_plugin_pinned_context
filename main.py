"""
Pinned Context Blocks — 钉选上下文块插件

让用户在对话中手动锚定关键信息作为"钉选上下文块"，
这些块会被注入到每次 LLM 调用的 system prompt 末尾，
不会被滑动窗口截断。

会话作用域：
  私聊  → 按会话隔离
  群聊  → 按**发言人**隔离（每人一份，互不可见），且需 enable_in_group 开启
          并受 group_whitelist 限制

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
  /pin clear                       清空我的全部块（需二次确认）
  /pin help                        显示帮助
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

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
        """每次 LLM 调用前：清理过期 + 注入钉选块。

        注：钉选块注入到 system_prompt 末尾。虽然官方文档建议动态内容
        使用 extra_user_content_parts 以避免破坏 prompt 缓存，但钉选块
        变化频率低（仅在用户执行 /pin 命令时变化），且 system_prompt
        天然免疫 ContextManager 的截断/压缩，符合设计意图。
        """
        # 解析会话键（群聊下按发言人隔离）
        session_id, reason = self._session_key(event)
        if not session_id:
            # no_sender 可能意味着框架层拿不到发言人 ID，会导致整群静默失去
            # 注入能力，必须留痕；其余原因是正常配置状态，不记日志。
            if reason == "no_sender":
                logger.warning(
                    f"[钉选上下文] 无法获取发言人 ID，已跳过注入 "
                    f"(umo={event.unified_msg_origin})"
                )
            return

        # 1. 单事务完成：递增调用计数 + 清理过期 idle 块 + 取回待注入块
        blocks = await self.block_mgr.prepare_injection(session_id)

        # 2. 注入到 system prompt 末尾
        if blocks:
            req.system_prompt = self.injector.inject(req.system_prompt or "", blocks)

    # ── /pin 命令组 ─────────────────────────────────────

    @filter.command("pin")
    async def pin_command(self, event: AstrMessageEvent):
        """钉选块管理命令入口。"""
        # 解析会话键（群聊下按发言人隔离）
        session_id, reason = self._session_key(event)
        if not session_id:
            yield event.plain_result(
                self._REASON_TEXT.get(reason, "❌ 钉选上下文在当前会话不可用")
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
                # 会话键按发言人隔离，clear 只影响调用者自己的块，故无需管理员门禁
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
        # 用正则匹配 " --pinned"（前面必须有空格），避免内容以 --pinned 结尾时误判
        match = re.search(r"\s--pinned\s*$", rest)
        if match:
            pinned = True
            rest = rest[: match.start()].strip()

        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "用法: /pin add <标签> <内容> [--pinned]"

        label, content = parts[0].strip(), parts[1].strip()
        status = "pinned" if pinned else "active"
        result = await self.block_mgr.add(session_id, label, content, status=status)
        if not result["ok"]:
            return f"❌ {result['error']}"
        return f"✅ 已钉选: 【{result['label']}】（{status}）" + self._truncation_note(
            result
        )

    async def _cmd_list(self, session_id: str) -> str:
        """列出所有块。"""
        grouped = await self.block_mgr.list_all(session_id)
        total = sum(len(v) for v in grouped.values())
        if total == 0:
            return "当前没有任何钉选块。使用 /pin add <标签> <内容> 创建。"

        lines = [f"📌 钉选上下文块（共 {total} 个）\n"]
        icons = {"active": "🟢", "idle": "💤", "pinned": "🔒"}
        known = ("pinned", "active", "idle")
        # 未知 status 也要列出来，否则用户看不到、也就删不掉
        extra = sorted(set(grouped) - set(known))

        for status in (*known, *extra):
            blocks = grouped.get(status) or []
            if not blocks:
                continue
            lines.append(f"{icons.get(status, '❔')} {status}（{len(blocks)} 个）:")
            for b in blocks:
                content_preview = b["content"][:60] + (
                    "..." if len(b["content"]) > 60 else ""
                )
                lines.append(f"  【{b['label']}】{content_preview}")
            lines.append("")

        # 检查 token 警告（使用配置的阈值）
        active_blocks = [
            b for status in ("active", "pinned") for b in (grouped.get(status) or [])
        ]
        if active_blocks and self.token_warning_threshold > 0:
            # 估算注入文本：注入头 + 各块（含 【】 与 🔒 标记的开销）。
            # 中文按 ≈1 token/字，其余按 ≈1 token/4 字符。
            text = self.injector.BLOCK_HEADER + "".join(
                b["label"] + b["content"] + "【】🔒" for b in active_blocks
            )
            cjk = sum(1 for c in text if "一" <= c <= "鿿")
            est_tokens = cjk + (len(text) - cjk) // 4
            if est_tokens > self.token_warning_threshold:
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
        if not result["ok"]:
            return f"❌ {result['error']}"
        return f"✅ 已更新: {label}" + self._truncation_note(result)

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
        """清空当前会话（群聊下即调用者本人）的全部块，需二次确认。"""
        grouped = await self.block_mgr.list_all(session_id)
        total = sum(len(v) for v in grouped.values())
        pinned_count = len(grouped.get("pinned", []))

        if confirm.strip().lower() != "confirm":
            hint = f"⚠️ 将删除你的全部 {total} 个钉选块"
            if pinned_count > 0:
                hint += f"（其中含 {pinned_count} 个 🔒 pinned 块）"
            hint += "。输入 /pin clear confirm 确认。"
            return hint

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

    # _session_key 返回空 key 时的原因 → 用户提示
    _REASON_TEXT = {
        "group_disabled": "❌ 钉选上下文在群聊中未启用",
        "group_not_allowed": "❌ 本群不在钉选上下文的允许列表中",
        "no_sender": "❌ 无法识别发送者，钉选在当前会话不可用",
        "no_umo": "❌ 无法识别当前会话",
    }

    @staticmethod
    def _truncation_note(result: dict) -> str:
        """把 BlockManager 回报的截断事实拼成用户提示；未截断则返回空串。"""
        truncated = result.get("truncated") or []
        if not truncated:
            return ""
        return f"\n⚠️ {'、'.join(truncated)}超出长度上限，已截断保存"

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
        if isinstance(raw, (str, int)):
            raw = re.split(r"[,，]", str(raw))
        return {str(item).strip() for item in raw if str(item).strip()}

    def _session_key(self, event: AstrMessageEvent) -> tuple[str, str]:
        """解析当前消息的会话键，返回 (key, reason)。

        key 为空串时 reason 说明未启用的原因。群聊下按**发言人**隔离
        （`<umo>::u:<sender_id>`），避免全群成员共享同一份钉选块——
        那等于把 system prompt 的写入权开放给所有群成员。

        拿不到发言人 ID 时 fail-closed，不退化成全群共享。
        """
        umo = event.unified_msg_origin or ""
        if not umo:
            return "", "no_umo"

        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return umo, "private"

        if not self.enable_in_group:
            return "", "group_disabled"
        if (
            self.group_whitelist
            and group_id not in self.group_whitelist
            and umo not in self.group_whitelist
        ):
            return "", "group_not_allowed"

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return "", "no_sender"

        return f"{umo}::u:{sender_id}", "group"

    def _help_text(self) -> str:
        mgr = self.block_mgr
        text = f"""📌 钉选上下文块 — 命令帮助

/pin add <标签> <内容>            新增块（默认 active）
/pin add <标签> <内容> --pinned   新增为长期锚定块
/pin list                          列出所有块（按状态分组）
/pin remove <标签>                 删除块
/pin edit <标签> <新内容>          编辑块
/pin idle <标签>                   停用（→ 闲置，后续自动清理）
/pin activate <标签>               重新激活闲置块
/pin pin <标签>                    长期锚定（永不过期）
/pin unpin <标签>                  取消锚定
/pin clear                         清空你的全部块（二次确认）

状态说明：
🟢 active  正常注入，不会被自动清理
💤 idle    不注入，闲置 {mgr.IDLE_RETENTION_DAYS} 天且又过 {mgr.IDLE_RETENTION_CALLS} 轮，
           或闲置满 {mgr.IDLE_HARD_RETENTION_DAYS} 天后自动删除
🔒 pinned  长期锚定，永不过期，仅手动删除"""
        if self.enable_in_group:
            text += "\n\n群聊中钉选块按发言人隔离，仅你本人可见。"
        return text
