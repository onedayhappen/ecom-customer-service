"""Actual HTTP entrypoint with a deterministic LLM and isolated business tools."""
import json

import pytest

from app.config.settings import settings
from app.api import main
from app.tools import registry
from tests.test_multiturn_tasks_api import api
from tests.test_conversation_agent import Client, reply


@pytest.fixture
def conversation_api(api, monkeypatch):
    monkeypatch.setattr(settings, 'assistant_just_react', True)
    # Any call to the old rewrite gate fails this test, including policy questions.
    from app.intent import query_rewrite
    def forbidden(*a, **k):
        pytest.fail('Unified conversations must not use the workflow rewrite gate')
    monkeypatch.setattr(query_rewrite, 'rewrite_query', forbidden)
    return api


def set_model(monkeypatch, responses):
    client = Client(responses)
    monkeypatch.setattr(main, '_get_openai_client', lambda: client)
    return client


def test_invoice_question_interruption_and_amendment_preserve_actual_turns(conversation_api, monkeypatch):
    _, _, _, send, tasks, _ = conversation_api
    model = set_model(monkeypatch, [
        reply(tool='ask_user', args={'prompt': '您要个人还是公司抬头？', 'options': ['个人', '公司']}),
        reply('可以先了解区别，再决定抬头。'),
        reply(tool='ask_user', args={'prompt': '好的，请提供个人抬头。'}),
    ])
    assert send('给耳机开发票').json()['question']['options']
    second = send('两种有什么区别？')
    assert second.status_code == 200 and not second.json()['confirmation_token']
    assert send('还是开个人的').status_code == 200
    assert tasks() == []
    users = [m['content'] for m in model.calls[-1]['messages'] if m['role'] == 'user']
    assert users == ['给耳机开发票', '两种有什么区别？', '还是开个人的']


def test_refund_executes_only_after_token_and_does_not_replan(conversation_api, monkeypatch):
    _, _, _, send, tasks, effects = conversation_api
    model = set_model(monkeypatch, [reply(tool='apply_refund', args={'order_id': 'ORD-A', 'reason': '耳机坏了'})])
    first = send('耳机坏了，退了吧')
    assert first.status_code == 200, first.text
    token = first.json()['confirmation_token']
    assert token and 'ORD-A' in first.json()['reply'] and effects == []
    confirmed = send('确认', confirmation_token=token)
    assert confirmed.status_code == 200, confirmed.text
    assert effects == [{'order_id': 'ORD-A', 'reason': '耳机坏了'}]
    assert len(model.calls) == 1
    assert tasks() == []
    assert not send('确认', confirmation_token=token).json()['confirmation_token']
    assert len(effects) == 1


def test_change_order_rejects_old_confirmation(conversation_api, monkeypatch):
    _, _, _, send, _, effects = conversation_api
    set_model(monkeypatch, [reply(tool='apply_refund', args={'order_id': 'ORD-A', 'reason': '坏了'}), reply('好的，先查询扫地机。')])
    token = send('耳机退款').json()['confirmation_token']
    assert send('耳机先不退了，扫地机查物流').status_code == 200
    rejected = send('确认', confirmation_token=token)
    assert rejected.status_code == 200
    assert '失效' in rejected.json()['reply'] and effects == []


def test_invoice_consultation_does_not_require_order_or_write(conversation_api, monkeypatch):
    _, _, _, send, _, effects = conversation_api
    called = []
    def search(**args):
        called.append(args)
        return {'success': True, 'results': [{'text': '个人发票填写个人抬头；公司发票还需税号。', 'source': 'test-policy'}]}
    monkeypatch.setitem(registry._TOOL_MAP, 'search_knowledge', search)
    set_model(monkeypatch, [reply(tool='search_knowledge', args={'query': '个人与公司发票区别'}), reply('公司抬头还需填写税号。')])
    response = send('个人和公司发票有什么区别？')
    assert response.status_code == 200, response.text
    assert called and effects == [] and not response.json()['confirmation_token']


def test_invoice_write_requires_preview_and_guard_authorization(conversation_api, monkeypatch):
    _, _, _, send, _, _ = conversation_api
    from app.tools.mutation_context import request_id_for
    args = {'order_id': 'ORD-A', 'preview_version': 'v1',
            'fields': {'invoice_type': 'personal', 'title': '张三'}}
    previews, writes = [], []
    def preview(**kwargs):
        previews.append(kwargs)
        return {'success': True, 'can_issue': True, 'preview_version': 'v1'}
    def invoice(**kwargs):
        assert request_id_for('user', 'issue_invoice', kwargs)
        writes.append(kwargs)
        return {'success': True, 'invoice_id': 'demo-invoice'}
    monkeypatch.setitem(registry._TOOL_MAP, 'query_invoice', preview)
    monkeypatch.setitem(registry._TOOL_MAP, 'issue_invoice', invoice)
    set_model(monkeypatch, [reply(tool='issue_invoice', args=args)])
    first = send('这个订单开个人发票，抬头张三').json()
    assert first['confirmation_token'] and previews and writes == []
    assert '张三' in first['reply'] and '本地演示' in first['reply']
    result = send('确认', confirmation_token=first['confirmation_token'])
    assert result.status_code == 200, result.text
    assert writes == [args] and '未开具真实税务发票' in result.json()['reply']


def test_unknown_mutation_result_never_retries_and_hands_off(conversation_api, monkeypatch):
    _, _, _, send, _, _ = conversation_api
    calls = []
    def refund(**kwargs):
        calls.append(kwargs)
        return {'success': False, 'error_code': 'transient_failure', 'error': '结果未知'}
    monkeypatch.setitem(registry._TOOL_MAP, 'apply_refund', refund)
    set_model(monkeypatch, [reply(tool='apply_refund', args={'order_id': 'ORD-A', 'reason': '坏了'})])
    token = send('耳机退款').json()['confirmation_token']
    response = send('确认', confirmation_token=token)
    assert response.status_code == 200, response.text
    assert response.json()['support']['mode'] == 'handoff_pending'
    send('确认', confirmation_token=token)
    assert len(calls) == 1
