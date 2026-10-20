"""Dataset integrity, held-out isolation and final-turn-only supervision."""
import copy
import json

import pytest

from app.data_plan.taxonomy import VALID_INTENTS
from app.prompts.intent_system import SYSTEM_PROMPT
from scripts.intent_dataset_validation import model_input, validate_splits
from scripts.eval_intent_model import classification_metrics, prepare_example, strict_output_valid
from scripts.train_intent_lora import IntentDataset, validate_dataset_pair, validate_training_row


@pytest.fixture(scope='module')
def corpus():
    """In-memory test inputs only; no dependency on a retired training corpus."""
    splits = {split: [] for split in ('train', 'dev', 'test')}
    for split, rows in splits.items():
        for intent in sorted(VALID_INTENTS):
            row = {
                'id': f'{split}-{intent}', 'split': split,
                'family_id': f'{split}-{intent}',
                'messages': [
                    {'role': 'user', 'content': f'{split} {intent} 测试上下文'},
                    {'role': 'assistant', 'content': '请补充您的需求。'},
                    {'role': 'user', 'content': '这是补充信息。'},
                    {'role': 'assistant', 'content': '还有其他要求吗？'},
                    {'role': 'user', 'content': '请继续处理。'},
                ],
            }
            if split == 'test':
                row['expected_intent'] = intent
            else:
                row['messages'].append({'role': 'assistant', 'content': json.dumps({'intent': intent})})
            rows.append(row)
    return splits


def test_split_validator_requires_full_labels_and_unique_ids(corpus):
    validate_splits(corpus)
    bad = copy.deepcopy(corpus)
    bad['train'].pop()
    with pytest.raises(ValueError, match='missing canonical labels'):
        validate_splits(bad)
    bad = copy.deepcopy(corpus)
    bad['test'][0]['id'] = bad['train'][0]['id']
    with pytest.raises(ValueError, match='duplicate id'):
        validate_splits(bad)


def test_no_source_family_or_input_can_cross_splits(corpus):
    validate_dataset_pair(corpus['train'], corpus['dev'])
    bad = copy.deepcopy(corpus)
    bad['test'][0]['family_id'] = bad['train'][0]['family_id']
    with pytest.raises(ValueError, match='source family'):
        validate_splits(bad)
    bad = copy.deepcopy(corpus)
    bad['test'][0]['messages'] = model_input(bad['train'][0])
    with pytest.raises(ValueError, match='duplicate input'):
        validate_splits(bad)


def test_test_rows_cannot_be_used_for_training_or_model_selection(corpus):
    row = copy.deepcopy(corpus['dev'][0])
    row['split'] = 'test'
    with pytest.raises(ValueError, match='split=test'):
        validate_dataset_pair(corpus['train'], [row])
    with pytest.raises(ValueError, match='split=test'):
        validate_dataset_pair([row], None)


@pytest.mark.parametrize('target', [
    {'intent': ['refund_service']},
    {'intent': 'refund_service', 'order_id': 'ORD-001'},
    {'intent': 'refund_service', 'confidence': 0.9},
    {'intent': 'answer_question'},
])
def test_training_target_is_exactly_one_canonical_intent(target):
    row = {'messages': [{'role': 'user', 'content': '退款'},
                        {'role': 'assistant', 'content': json.dumps(target)}]}
    with pytest.raises(ValueError, match='31 类'):
        validate_training_row(row)


def test_empty_agent_or_wrong_roles_are_rejected():
    row = {'messages': [{'role': 'user', 'content': '请提供订单号'},
                        {'role': 'assistant', 'content': ''},
                        {'role': 'user', 'content': '这单'},
                        {'role': 'assistant', 'content': '{"intent":"refund_service"}'}]}
    with pytest.raises(ValueError):
        validate_training_row(row)
    row['messages'][1] = {'role': 'user', 'content': '我要退款'}
    with pytest.raises(ValueError, match='交替'):
        validate_training_row(row)


def test_only_final_label_is_supervised_not_history(corpus):
    class Tokenizer:
        def apply_chat_template(self, messages, add_generation_prompt=False, **kwargs):
            result = ''.join(f"<{m['role']}>{m['content']}<end>" for m in messages)
            return result + ('<assistant>' if add_generation_prompt else '')

        def __call__(self, text, **kwargs):
            return {'input_ids': [ord(c) for c in text], 'attention_mask': [1] * len(text)}

    row = next(r for r in corpus['train'] if len(r['messages']) == 6)
    tokenizer = Tokenizer()
    item = IntentDataset([row], tokenizer, 20000)[0]
    visible = ''.join(chr(t) for t in item['labels'] if t != -100)
    assert visible == row['messages'][-1]['content'] + '<end>'
    assert SYSTEM_PROMPT in ''.join(chr(t) for t in item['input_ids'])
    assert 'source_id' not in ''.join(chr(t) for t in item['input_ids'])


def test_eval_input_never_contains_gold_target(corpus):
    for split in ('train', 'dev', 'test'):
        row = corpus[split][0]
        original = copy.deepcopy(row)
        messages, expected = prepare_example(row)
        assert messages[-1]['role'] == 'user' and expected in VALID_INTENTS
        assert row == original


def test_metrics_count_invalid_output_and_confusion():
    details = [
        {'expected': 'refund_service', 'predicted': 'refund_service', 'ok': True},
        {'expected': 'refund_service', 'predicted': 'exchange_service', 'ok': False},
        {'expected': 'exchange_service', 'predicted': None, 'ok': False},
    ]
    result = classification_metrics(details)
    assert result['accuracy'] == pytest.approx(1 / 3)
    assert result['macro_f1'] == pytest.approx(1 / 3)
    assert result['per_class']['refund_service']['recall'] == 0.5
    assert result['invalid_output_count'] == 1
    assert len(result['confusions']) == 2
    assert strict_output_valid('{"intent":"refund_service"}')
    assert not strict_output_valid('答案：{"intent":"refund_service"}')
    assert not strict_output_valid('{"intent":"refund_service","reason":"退款"}')
