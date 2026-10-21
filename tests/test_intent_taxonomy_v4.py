"""Hand-reviewed boundaries, migration safeguards and dispatch compatibility."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.agents.engine import AgentEngine
from app.data_plan import build_data_plan
from app.data_plan.enums import Intent
from app.data_plan.taxonomy import (
    CLASSIFICATION_INTENTS, LEGACY_INTENTS, MANIFEST_NAME, VALID_INTENTS,
    validate_manifest, write_manifest,
)
from app.execution import dispatch, dispatch_async
from app.intent.classifier import IntentClassifier, parse_intent
from app.intent.rules import recognize, conversation_messages
from scripts.eval_intent_model import prepare_example
from scripts.train_intent_lora import validate_training_row

FIXTURE = Path(__file__).resolve().parents[1] / 'data/evaluation/multiturn_intent_eval_v4.jsonl'
CASES = [json.loads(line) for line in FIXTURE.read_text(encoding='utf-8').splitlines()]


@pytest.mark.parametrize('row', CASES, ids=[r['id'] for r in CASES])
def test_reviewed_classification_boundaries(row):
    messages, expected = prepare_example(row)
    assert recognize(messages[-1]['content'], messages).value == expected


def test_fixture_covers_every_canonical_class():
    assert {row['expected_intent'] for row in CASES} == VALID_INTENTS


@pytest.mark.parametrize('text', [
    '{"intent":"refund_service"',
    '{"intent":"refund_service"} {"intent":"order_cancel"}',
    '{"intent": ["refund_service"]}',
])
def test_malformed_model_output_is_not_accepted(text):
    assert parse_intent(text) is None


@pytest.mark.parametrize('intent', list(CLASSIFICATION_INTENTS | LEGACY_INTENTS))
def test_model_parser_rejects_retired_labels(intent):
    parsed = parse_intent(json.dumps({'intent': intent.value}))
    assert parsed == (intent.value if intent in CLASSIFICATION_INTENTS else None)


def test_old_model_is_rejected_before_loading_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr('app.intent.classifier.settings.intent_adapter_path', str(tmp_path))
    classifier = IntentClassifier()
    assert not classifier._ensure_loaded()
    assert MANIFEST_NAME in classifier._load_error
    assert not classifier.available


def test_manifest_requires_exact_label_set_and_version(tmp_path):
    write_manifest(tmp_path)
    validate_manifest(tmp_path)
    path = tmp_path / MANIFEST_NAME
    good = json.loads(path.read_text(encoding='utf-8'))
    for bad in ({**good, 'taxonomy_version': 'v3'},
                {**good, 'labels': good['labels'][:-1]},
                {**good, 'labels': [*good['labels'][:-1], 'confirmation']}):
        path.write_text(json.dumps(bad), encoding='utf-8')
        with pytest.raises(ValueError):
            validate_manifest(tmp_path)


def test_training_rejects_legacy_targets():
    for intent in LEGACY_INTENTS:
        with pytest.raises(ValueError, match='31 类'):
            validate_training_row({'messages': [
                {'role': 'user', 'content': '我要退款'},
                {'role': 'assistant', 'content': json.dumps({'intent': intent.value})},
            ]})


def test_evaluation_removes_target_without_mutating_data():
    row = {'messages': [{'role': 'user', 'content': '你好'},
                        {'role': 'assistant', 'content': '{"intent":"greetings"}'}]}
    original = copy.deepcopy(row)
    messages, expected = prepare_example(row)
    assert expected == 'greetings' and len(messages) == 1
    assert row == original
    with pytest.raises(ValueError):
        prepare_example({**row, 'expected_intent': 'closure'})


def test_classifier_context_excludes_future_answers_and_tool_output():
    messages = [{'role': 'user', 'content': '你好'},
                {'role': 'tool', 'content': '订单退款'},
                {'role': 'assistant', 'content': '未来回复'}]
    original = copy.deepcopy(messages)
    assert conversation_messages('你好', messages) == [messages[0]]
    assert messages == original


@pytest.mark.parametrize('intent', [i for i in CLASSIFICATION_INTENTS
                                  if not build_data_plan(i).spec.execution_ready])
def test_pending_execution_never_calls_a_tool_or_requires_a_client(intent, monkeypatch):
    from app.config.settings import settings
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f'未接入执行路径不能调用 {name}')
    plan = build_data_plan(intent)
    results = [dispatch(plan, runner=Forbidden(), react_executor=Forbidden()),
               asyncio.run(dispatch_async(plan, runner=Forbidden(), react_executor=Forbidden()))]
    for result in results:
        assert result.intent == intent and result.blocked and result.tool_steps == []
        assert not result.needs_confirmation
        assert '尚未接入' in result.reply




def test_refund_keeps_existing_guarded_chain_with_narrow_tool_set():
    plan = build_data_plan(Intent.REFUND_SERVICE)
    assert plan.spec.execution_ready
    assert 'apply_refund' in plan.tools
    assert 'apply_exchange' not in plan.tools
    assert 'apply_price_protect' not in plan.tools
