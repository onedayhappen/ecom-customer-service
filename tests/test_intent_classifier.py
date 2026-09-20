"""意图识别层测试：微调分类器（解析 / 回退 / 多轮上下文）。

不加载真实模型（CPU 推理太慢且非必需）：通过注入 fake 分类器与
fake tokenizer/model 验证路由逻辑与 prompt 构造。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data_plan.enums import Intent
from app.intent import recognize
from app.intent.classifier import (
    IntentClassifier,
    SYSTEM_PROMPT,
    parse_intent,
)

# ----------------------------------------------------------------------
# parse_intent
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"intent": "order_inquiry"}', "order_inquiry"),
    ('  输出：{"intent":"logistics_inquiry"}  ', "logistics_inquiry"),
    ('{"intent": "product_inquiry"}\n', "product_inquiry"),
])
def test_parse_intent_valid(text: str, expected: str) -> None:
    assert parse_intent(text) == expected


@pytest.mark.parametrize("text", [
    "",
    "我不知道",
    '{"intent": "中文标签"}',
    '{"intent": "not_a_valid_label"}',
    '{"action": "order_inquiry"}',
    "order_inquiry",  # 裸标签不合法，必须 JSON 结构
])
def test_parse_intent_invalid(text: str) -> None:
    assert parse_intent(text) is None


def test_system_prompt_contains_all_labels() -> None:
    for label in ("order_inquiry", "product_recommendation", "others"):
        assert label in SYSTEM_PROMPT


# ----------------------------------------------------------------------
# recognize：模型优先 + 规则回退
# ----------------------------------------------------------------------

def test_recognize_falls_back_to_rules_when_model_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(classify=lambda msgs: None)
    monkeypatch.setattr("app.intent.get_classifier", lambda: fake)
    assert recognize("我要退款") == Intent.AFTER_SALE_REQUEST
    assert recognize("你好") == Intent.GREETINGS


def test_recognize_prefers_model_over_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 规则会把"退款"判为 after_sale_request，但模型输出 logistics_inquiry
    fake = SimpleNamespace(classify=lambda msgs: Intent.LOGISTICS_INQUIRY)
    monkeypatch.setattr("app.intent.get_classifier", lambda: fake)
    assert recognize("退款") == Intent.LOGISTICS_INQUIRY


def test_recognize_passes_context_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_classify(msgs: list[dict]):
        captured["msgs"] = msgs
        return Intent.CONFIRMATION

    monkeypatch.setattr("app.intent.get_classifier", lambda: SimpleNamespace(classify=fake_classify))
    history = [
        {"role": "user", "content": "我要退款"},
        {"role": "assistant", "content": "好的，请提供订单号"},
        {"role": "user", "content": "好的"},
    ]
    result = recognize("好的", messages=history)
    assert result == Intent.CONFIRMATION
    # 上下文原样透传（含历史 assistant 回复），末轮是当前 user 消息
    assert captured["msgs"] == history


def test_recognize_single_turn_builds_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_classify(msgs: list[dict]):
        captured["msgs"] = msgs
        return None

    monkeypatch.setattr("app.intent.get_classifier", lambda: SimpleNamespace(classify=fake_classify))
    recognize("查一下物流")
    assert captured["msgs"] == [{"role": "user", "content": "查一下物流"}]


def test_recognize_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.intent.get_classifier",
        lambda: SimpleNamespace(classify=lambda msgs: None),
    )
    assert recognize("") == Intent.OTHERS


# ----------------------------------------------------------------------
# IntentClassifier：上下文裁剪
# ----------------------------------------------------------------------

def test_trim_context_keeps_last_user_turn() -> None:
    msgs = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好，请问有什么可以帮您"},
        {"role": "user", "content": "查订单"},
    ]
    assert IntentClassifier._trim_context(msgs) == msgs


def test_trim_context_drops_trailing_assistant() -> None:
    # 末轮不是 user（异常输入）：截到最近一条 user
    msgs = [
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": "好的"},
    ]
    trimmed = IntentClassifier._trim_context(msgs)
    assert trimmed == [{"role": "user", "content": "查订单"}]


def test_trim_context_limits_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.intent.classifier.settings.intent_max_context_turns", 2)
    msgs = [
        {"role": "user", "content": f"第{i}轮"} for i in range(6)
    ]
    trimmed = IntentClassifier._trim_context(msgs)
    assert len(trimmed) == 2
    assert trimmed[0]["content"] == "第4轮"
    assert trimmed[-1]["content"] == "第5轮"


def test_trim_context_empty() -> None:
    trimmed = IntentClassifier._trim_context([])
    assert trimmed == [{"role": "user", "content": ""}]
    assert IntentClassifier._trim_context(None) == [{"role": "user", "content": ""}]


# ----------------------------------------------------------------------
# IntentClassifier：推理映射（fake tokenizer / model）
# ----------------------------------------------------------------------

class _FakeInputs:
    """模拟张量：支持 .to() / 下标取 input_ids / shape 切片（decode 用）。"""

    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.shape = (1, len(ids))

    def to(self, device: str):
        return self

    def __getitem__(self, key):
        return self.ids


class _FakeTokenizer:
    pad_token_id = 0
    eos_token_id = 151643

    def __init__(self, decode_text: str) -> None:
        self._decode_text = decode_text
        self.captured_messages: list | None = None

    def apply_chat_template(self, messages, tokenize, add_generation_prompt, return_tensors, return_dict):
        assert add_generation_prompt is True
        self.captured_messages = messages
        return {"input_ids": _FakeInputs([1, 2, 3])}

    def decode(self, token_ids, skip_special_tokens=True):
        return self._decode_text


class _FakeModel:
    def __init__(self, device: str) -> None:
        self.device = device

    def to(self, device):
        return self

    def eval(self):
        return self

    def generate(self, inputs, **kwargs):
        assert kwargs["do_sample"] is False
        return [[1, 2, 3, 4]]


def _make_classifier(decode_text: str) -> tuple[IntentClassifier, _FakeTokenizer]:
    clf = IntentClassifier()
    tokenizer = _FakeTokenizer(decode_text)
    clf._tokenizer = tokenizer
    clf._model = _FakeModel("cpu")
    clf._device = "cpu"
    return clf, tokenizer


def test_classify_maps_valid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.intent.classifier.settings.intent_model_enabled", True)
    clf, tokenizer = _make_classifier('{"intent": "invoice"}')
    messages = [{"role": "user", "content": "帮我开个发票"}]
    assert clf.classify(messages) == Intent.INVOICE
    # system 在前，对话在后，且对话原样透传
    assert tokenizer.captured_messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert tokenizer.captured_messages[1:] == messages


def test_classify_returns_none_on_unparsable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.intent.classifier.settings.intent_model_enabled", True)
    clf, _ = _make_classifier("抱歉，我没理解")
    assert clf.classify([{"role": "user", "content": "你好"}]) is None


def test_classify_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.intent.classifier.settings.intent_model_enabled", False)
    clf, _ = _make_classifier('{"intent": "greetings"}')
    assert clf.classify([{"role": "user", "content": "你好"}]) is None
