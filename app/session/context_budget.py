"""会话上下文预算的保守估算工具。

这里不依赖某一家模型的 tokenizer，避免模型切换或部署环境缺少 tokenizer
导致压缩失效。估算值用于提前触发压缩，因此宁可略高，不用作账单统计。
"""

from __future__ import annotations

from math import ceil
from typing import Any


def estimate_text_tokens(value: Any) -> int:
    """粗略估算文本 token 数，中文按字、其他文本按约 3 字符计。"""
    text = str(value or "")
    chinese_chars = sum("\u3400" <= char <= "\u9fff" for char in text)
    other_chars = len(text) - chinese_chars
    return ceil(chinese_chars * 1.1 + other_chars / 3)


def estimate_message_tokens(messages: list[dict]) -> int:
    """估算 Chat Completions 消息数组的输入 token 数。"""
    total = 0
    for message in messages:
        # role、消息边界和工具调用的结构本身也会消耗 token。
        total += 8 + estimate_text_tokens(message.get("content"))
        if message.get("tool_calls"):
            total += estimate_text_tokens(message["tool_calls"])
    return total
