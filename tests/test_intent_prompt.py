"""意图识别零样本提示词模块测试。"""

from __future__ import annotations

from app.data_plan.taxonomy import VALID_INTENTS
from app.prompts.intent_classifier import (
    INTENT_CLASSIFIER_PROMPT,
    INTENT_LABELS,
    build_intent_classification_prompt,
    format_conversation_context,
)


def test_intent_labels_match_enum() -> None:
    assert set(INTENT_LABELS) == VALID_INTENTS
    assert len(INTENT_LABELS) == 31


def test_prompt_contains_all_labels_and_output_rules() -> None:
    for label in INTENT_LABELS:
        assert label in INTENT_CLASSIFIER_PROMPT
    assert '{"intent": "xxx", "confidence": 0.00}' in INTENT_CLASSIFIER_PROMPT


def test_build_prompt_renders_current_message() -> None:
    prompt = build_intent_classification_prompt("我要退款")
    assert "用户当前消息" in prompt
    assert "我要退款" in prompt
    assert "无历史对话" in prompt


def test_build_prompt_renders_history() -> None:
    history = [
        {"role": "user", "content": "我买的东西坏了"},
        {"role": "assistant", "content": "请提供订单号"},
        {"role": "user", "content": "ORD-123"},
    ]
    prompt = build_intent_classification_prompt("ORD-123", messages=history)
    assert "我买的东西坏了" in prompt
    assert "请提供订单号" in prompt
    # 当前消息只出现在「用户当前消息」区，避免上下文重复注入
    assert prompt.count("ORD-123") == 1


def test_format_context_handles_empty_and_limits() -> None:
    assert "无历史对话" in format_conversation_context([])
    history = [{"role": "user", "content": f"第{i}轮"} for i in range(6)]
    rendered = format_conversation_context(history, max_turns=2)
    assert "第0轮" not in rendered
    assert "第4轮" in rendered
    assert "第5轮" in rendered
