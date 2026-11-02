"""Short natural replies cross the real publication boundary without templates."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.agents.reply_composition import compose
from app.tasks.natural_reply import UNAVAILABLE_REPLY, VerifiedReply, fingerprint
from app.tasks.replies import ReplyCoordinator
from tests.test_coordinated_replies import ready, replies
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store


class Model:
    def __init__(self, text, *, unknown_source=False):
        self.text = text
        self.unknown_source = unknown_source
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **params):
        self.requests.append(params)
        data = json.loads(params['messages'][1]['content'])
        assert len(self.requests) == 1, 'Reply composition must use a single model call'
        assert all('text' not in c and 'selection_hint' not in c
                   for m in data['matters'] for c in m['claims'])
        assert data['matters'][0]['receipts']
        content = dict(reply=self.text,
                       claim_ids=['unknown'] if self.unknown_source else
                       [m['claims'][0]['claim_id'] for m in data['matters'] if m['claims']])
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content=json.dumps(content, ensure_ascii=False), tool_calls=None))])


def publish(store, model):
    record, source = task(store, 's', goal='这件发货了吗？')
    ready(store, record, source)
    return ReplyCoordinator(store, 'alice', composer=lambda context: compose(model, 'test', context)).publish('s')


def test_natural_wording_is_published_instead_of_evidence_template(store):
    model = Model('记录显示已发货。')
    result = publish(store, model)
    assert result['content'] == '记录显示已发货。'
    assert len(result['content']) <= 20
    assert result['evidence_ids'] and len(model.requests) == 1
    assert all('tools' not in r for r in model.requests)
    assert model.requests[0]['timeout'] is None
    assert ReplyCoordinator(store, 'alice').publish('s') is None
    assert len(replies(store)) == 1


@pytest.mark.parametrize('text', ['', '   ', '{"status":"已发货"}', '["已发货"]', '```已发货```'])
def test_empty_or_structured_draft_is_rejected_without_another_model_call(store, text):
    model = Model(text)
    assert publish(store, model)['content'] == UNAVAILABLE_REPLY
    assert len(model.requests) == 1


@pytest.mark.parametrize('text', ['已经退款999元。', '已经签收。', '保证明天送达。'])
def test_unsupported_critical_claim_is_rejected_locally(store, text):
    model = Model(text)
    assert publish(store, model)['content'] == UNAVAILABLE_REPLY
    assert len(model.requests) == 1


def test_unknown_evidence_is_rejected_without_another_model_call(store):
    model = Model('记录显示已发货。', unknown_source=True)
    assert publish(store, model)['content'] == UNAVAILABLE_REPLY
    assert len(model.requests) == 1


def test_verified_natural_reply_cannot_be_reused_after_evidence_changes():
    original = dict(matters=[dict(task_id='t', receipts=[dict(status='paid')])])
    result = VerifiedReply('记录显示已支付。', fingerprint(original))
    changed = deepcopy(original)
    changed['matters'][0]['receipts'][0]['status'] = 'cancelled'
    with pytest.raises(ValueError):
        result.render(changed)
    from app.evaluation.agent.runtime import plain
    assert json.loads(json.dumps(plain(result)))['text'] == result.text


@pytest.mark.parametrize('text', [
    '订单记录显示已发货，实际运输进展还需核实。',
    '订单记录显示已发货；当前提供的记录不含物流轨迹和预计送达时间，暂时无法确认包裹所在位置，也无法确定何时送达。',
])
def test_reply_preserves_necessary_details_without_character_limit(store, text):
    model = Model(text)
    assert len(text) > 20
    assert publish(store, model)['content'] == text
    assert len(model.requests) == 1


def test_disabled_composer_never_exposes_long_evidence_paragraphs(store):
    record, source = task(store, 's')
    ready(store, record, source)
    result = ReplyCoordinator(store, 'alice').publish('s')
    assert result['content'] == UNAVAILABLE_REPLY
    assert len(result['content']) <= 20
    assert result['evidence_ids']
