"""业务意图识别的零样本 LLM 提示词（intent + confidence）。

与 :data:`app.intent.classifier.SYSTEM_PROMPT` 的区别：

- 后者是可选本地分类器的推理 prompt；如果外部模型使用同一套标签，
  应保持其 system prompt 与训练时的版本一致；
- 本模块是面向通用 LLM 的零样本提示词，输出 ``{"intent", "confidence"}``，
  供需要置信度与完整边界规则的 LLM 识别链路使用，不参与微调模型推理。

标签集合由 app.data_plan.taxonomy 统一定义，不包含旧任务兼容标签。
"""

from __future__ import annotations

from app.data_plan.taxonomy import DEFINITIONS
from app.prompts.intent_system import BOUNDARY_RULES

INTENT_LABELS: tuple[str, ...] = tuple(intent.value for intent in DEFINITIONS)
INTENT_CLASSIFIER_PROMPT = (
    "你是电商客服意图分类器。结合多轮上下文，只判断当前用户目标。\n"
    + "\n".join(f"{intent.value}：{description}。" for intent, (_, description) in DEFINITIONS.items())
    + "\n" + BOUNDARY_RULES
    + '\n输出格式：{"intent": "xxx", "confidence": 0.00}。只输出该 JSON；confidence 为 0 到 1 的判断确定程度，不表示执行授权。'
    + "\n## 对话上下文\n{conversation_context}\n## 用户当前消息\n{user_input}"
)


def format_conversation_context(
    messages: list[dict] | None,
    max_turns: int | None = None,
) -> str:
    """把 OpenAI 格式消息渲染为「用户/客服」对话文本，供 prompt 注入。"""
    msgs = [
        m for m in (messages or [])
        if isinstance(m, dict) and m.get("role") in {"user", "assistant"} and str(m.get("content", "")).strip()
    ]
    if max_turns is not None and max_turns > 0:
        msgs = msgs[-max_turns:]
    if not msgs:
        return "（无历史对话，仅根据当前消息判断）"
    lines = []
    for msg in msgs:
        role = "用户" if msg.get("role") == "user" else "客服"
        lines.append(f"{role}：{str(msg['content']).strip()}")
    return "\n".join(lines)


def build_intent_classification_prompt(
    user_input: str,
    messages: list[dict] | None = None,
    max_turns: int | None = None,
) -> str:
    """渲染完整的意图识别提示词。

    :param user_input: 用户当前消息
    :param messages: 对话上下文（OpenAI 格式，不含 system）。可传入完整对话，
        若末轮 user 消息与 ``user_input`` 相同会自动从上下文中去重，避免重复注入。
    :param max_turns: 最多注入的消息条数；None 表示全部
    """
    from app.intent.rules import conversation_messages
    current = str(user_input or "").strip()
    history = conversation_messages(current, messages)[:-1]
    context = format_conversation_context(history, max_turns=max_turns)
    return (
        INTENT_CLASSIFIER_PROMPT
        .replace("{conversation_context}", context)
        .replace("{user_input}", current)
    )


__all__ = [
    "INTENT_CLASSIFIER_PROMPT",
    "INTENT_LABELS",
    "build_intent_classification_prompt",
    "format_conversation_context",
]
