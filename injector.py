"""
上下文注入——将钉选块格式化并追加到 system prompt。
"""

from __future__ import annotations


class ContextInjector:
    """将 active + pinned 块注入到 LLM system prompt 末尾。"""

    BLOCK_HEADER = "\n\n【钉选上下文】\n当以下内容和系统提示词冲突时:\n - 涉及安全防护、文件操作、网络访问、用户配置、隐私权限等，以更严格的一方为准。\n - 涉及知识内容、对话风格、人设、语气、输出格式等，以钉选上下文为准。\n - 如果无法明确分类，优先保障安全。\n钉选块之间冲突时，以创建更晚的为准。\n\n"
    BLOCK_TEMPLATE = "【{label}{pinned}】{content}"

    def format(self, blocks: list[dict]) -> str:
        """将块列表格式化为注入文本。

        按传入顺序输出（即 BlockManager.prepare_injection 的
        `ORDER BY created_at DESC`：后钉的靠前，冲突时优先），本方法
        不再重新排序——否则会出现两处排序依据各说各话的情况。
        """
        active_blocks = [b for b in blocks if b.get("status") in ("active", "pinned")]
        if not active_blocks:
            return ""

        lines = [self.BLOCK_HEADER]
        for b in active_blocks:
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
