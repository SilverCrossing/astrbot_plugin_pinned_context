"""
上下文注入——将钉选块格式化并追加到 system prompt。
"""
from __future__ import annotations


class ContextInjector:
    """将 active + pinned 块注入到 LLM system prompt 末尾。"""

    BLOCK_HEADER = "\n\n【📌 钉选上下文】"
    BLOCK_TEMPLATE = "【{label}{pinned}】{content}"

    def format(self, blocks: list[dict]) -> str:
        """将块列表格式化为注入文本。"""
        active_blocks = [b for b in blocks if b.get("status") in ("active", "pinned")]
        if not active_blocks:
            return ""

        lines = [self.BLOCK_HEADER]
        for b in sorted(active_blocks, key=lambda x: -(x.get("priority") or 0)):
            pinned_mark = " 🔒" if b.get("status") == "pinned" else ""
            lines.append(
                self.BLOCK_TEMPLATE.format(
                    label=b["label"],
                    pinned=pinned_mark,
                    content=b["content"],
                )
            )
        return "\n".join(lines)

    def inject(self, system_prompt: str, blocks: list[dict]) -> str:
        """将钉选块追加到 system prompt 末尾，不覆盖原始内容。"""
        block_text = self.format(blocks)
        if not block_text:
            return system_prompt
        return (system_prompt or "") + block_text
