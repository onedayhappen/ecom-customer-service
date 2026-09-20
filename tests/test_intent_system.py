"""共用提示词的消息边界与训练输入验证，不验证模型分类准确率。"""

import copy

import pytest

from app.prompts.intent_system import SYSTEM_PROMPT, with_intent_system
from scripts.train_intent_lora import IntentDataset


def test_replaces_stale_system_without_mutating_dataset():
    messages = [
        {"role": "system", "content": "旧版训练提示词"},
        {"role": "assistant", "content": "请提供订单号"},
        {"role": "user", "content": "ORD-TEST-001"},
        {"role": "assistant", "content": '{"intent":"answer_question"}'},
    ]
    original = copy.deepcopy(messages)
    result = with_intent_system(messages)
    assert result == [{"role": "system", "content": SYSTEM_PROMPT}, *messages[1:]]
    assert messages == original
    result[1]["content"] = "changed"
    assert messages == original


class CharacterTokenizer:
    """用字符模拟 token，验证超长输入不会吞掉训练标签。"""

    def apply_chat_template(self, messages, **kwargs):
        return "".join(m["content"] for m in messages)

    def __call__(self, text, **kwargs):
        return {"input_ids": list(range(len(text))), "attention_mask": [1] * len(text)}


def test_training_rejects_truncation_and_keeps_target():
    messages = [
        {"role": "user", "content": "我要退款"},
        {"role": "assistant", "content": '{"intent":"after_sale_request"}'},
    ]
    rows = [{"messages": messages}]
    with pytest.raises(ValueError, match="不能截掉"):
        IntentDataset(rows, CharacterTokenizer(), max_length=20)
    dataset = IntentDataset(rows, CharacterTokenizer(), max_length=10000)
    item = dataset[0]
    prompt_length = len(SYSTEM_PROMPT) + len(messages[0]["content"])
    assert item["labels"][:prompt_length] == [-100] * prompt_length
    assert item["labels"][prompt_length:] == item["input_ids"][prompt_length:]
    assert len(item["labels"][prompt_length:]) == len(messages[-1]["content"])
