"""会话上下文预算的保守估算工具。

这里不依赖某一家模型的 tokenizer，避免模型切换或部署环境缺少 tokenizer
导致压缩失效。估算值用于提前触发压缩，因此宁可略高，不用作账单统计。
"""

from __future__ import annotations

import json
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
    # Include names, call ids/arguments, reasoning fields and content arrays.
    # JSON overhead deliberately errs upwards; this is not billing tokenization.
    return sum(8 + estimate_text_tokens(_serialized(message)) for message in messages)


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def estimate_request_tokens(params: dict) -> int:
    """Estimate the complete provider input, including tool/response schemas."""
    messages = params.get("messages", [])
    # Output limits and sampling controls don't consume the input window. All
    # other options are counted, including provider-specific structured input.
    non_input = {
        "messages", "model", "scene", "max_tokens", "max_completion_tokens",
        "temperature", "top_p", "stream", "stream_options", "seed", "n",
        "presence_penalty", "frequency_penalty", "timeout", "user",
    }
    extra = {key: value for key, value in params.items() if key not in non_input}
    return 8 + estimate_message_tokens(messages) + (estimate_text_tokens(_serialized(extra)) if extra else 0)
