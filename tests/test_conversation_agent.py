"""Unified mode: advisory labels, flexible turns and actual execution boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.engine import AgentEngine
from app.config.settings import settings
from app.data_plan.builder import build_conversation_plan
from app.data_plan.enums import Execution
from app.data_plan.taxonomy import CLASSIFICATION_INTENTS
from app.execution import ReactExecutor
from app.execution.proposals import prepare_proposal
from app.memory.case_manager import CaseManager
from app.reliability import MutationGuard
from app.support.store import SupportStore, BotInterrupted
from app.support.runtime import bot_scope
from app.tools.request_context import set_current_user_id, reset_current_user_id


def reply(text='', tool=None, args=None):
    calls = [SimpleNamespace(id='call', function=SimpleNamespace(name=tool, arguments=json.dumps(args or {})))] if tool else None
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))])


def test_retired_order_draft_cannot_be_written_without_a_task():
    from app.tools.case_context import save_case_context
    result = save_case_context(order_id='ORD-A', expected_version=0, intent='refund_service',
                               state='resolved', summary='claimed complete', fields={})
    assert result['success'] is False and result['error_code'] == 'retired_path'


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


@pytest.mark.parametrize('intent', CLASSIFICATION_INTENTS)
def test_all_31_labels_can_reply_without_tool_or_order(intent):
    client = Client([reply('您好，请问需要什么帮助？')])
    engine = AgentEngine(intent, client=client, model='test')
    result = engine.execute('你好')
    assert result.execution is Execution.REACT
    assert not result.blocked and not result.tool_steps
    assert engine.plan.required_slots == ()
    assert 'issue_invoice' in engine.plan.tools and 'query_logistics' in engine.plan.tools


def test_async_engine_uses_same_conversation_contract():
    async def create(**kwargs):
        return reply('请问您要咨询什么？')
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = asyncio.run(AgentEngine('invoice_inquiry', client=client, model='test').execute_async('个人和公司发票有区别吗'))
    assert result.execution is Execution.REACT and not result.blocked


def test_wrong_label_does_not_prevent_logistics_query():
    client = Client([reply(tool='query_logistics', args={'order_id': 'ORD-A'}), reply('已为您查询。')])
    called = []
    def execute(name, args):
        called.append(name)
        return json.dumps({'success': True, 'logistics': {'status': 'shipped'}})
    result = AgentEngine('greetings').execute('耳机到哪了', react_executor=ReactExecutor(client, 'test', execute=execute))
    assert called == ['query_logistics'] and not result.blocked


def test_confirmed_boolean_cannot_bypass_mutation_guard():
    client = Client([reply(tool='apply_refund', args={'order_id': 'ORD-A', 'reason': '坏了'})])
    def forbidden(*args):
        pytest.fail('No business write may run without a guard')
    result = AgentEngine('greetings').execute('退了吧', confirmed=True,
        react_executor=ReactExecutor(client, 'test', execute=forbidden))
    assert result.blocked and not result.confirmation_token


def test_concrete_refund_proposal_binds_action_not_classifier(tmp_path):
    manager = CaseManager('user', str(tmp_path))
    guard = MutationGuard(manager, 'session')
    calls = []
    def execute(name, args):
        calls.append(name)
        return json.dumps({'success': True, 'order': {'order_id': 'ORD-A'}})
    client = Client([reply(tool='apply_refund', args={'order_id': 'ORD-A', 'reason': '损坏'})])
    result = AgentEngine('product_inquiry').execute('退款', mutation_guard=guard, confirmed=True,
        react_executor=ReactExecutor(client, 'test', execute=execute))
    assert calls == ['query_order']
    assert result.needs_confirmation and 'ORD-A' in result.reply and '损坏' in result.reply
    assert guard.peek_confirmation(result.confirmation_token)['intent'] == 'refund_service'


def test_invoice_missing_company_tax_id_never_issues_proposal():
    def forbidden(*args):
        pytest.fail('Invalid invoice must not reach the database')
    proposal = prepare_proposal('issue_invoice', {'order_id': 'ORD-A', 'preview_version': 'v1',
        'fields': {'invoice_type': 'company', 'title': '测试公司'}}, forbidden)
    assert proposal.error and not proposal.summary


def test_invoice_preflight_version_must_match_and_summary_is_concrete():
    args = {'order_id': 'ORD-A', 'preview_version': 'v1',
            'fields': {'invoice_type': 'personal', 'title': '张三'}}
    stale = prepare_proposal('issue_invoice', args, lambda *a: {'success': True, 'can_issue': True, 'preview_version': 'v2'})
    assert stale.error
    good = prepare_proposal('issue_invoice', args, lambda *a: {'success': True, 'can_issue': True, 'preview_version': 'v1'})
    assert not good.error and '张三' in good.summary and '本地演示' in good.summary


def test_plaintext_never_creates_tasks_or_autofills_pending_questions(tmp_path):
    store = SupportStore(f'sqlite:///{tmp_path / "support.db"}')
    store.ensure('session', 'user')
    state = store.receive('session', 'user', '耳机退了吧，扫地机查物流', 'first', task_input={})
    route = store.tasks.turn('session', 'user', 'first')
    assert route == {'message': '耳机退了吧，扫地机查物流'}
    assert store.tasks.list('session', 'user')['tasks'] == []
    store.start_bot('session', 'user', state['control_version'], 'first')
    store.publish_bot('session', 'user', state['control_version'], 'first', '个人还是公司？',
                      {'question': {'prompt': '个人还是公司？', 'field': 'details'}})
    store.release_bot('session', 'user', 'first')
    store.receive('session', 'user', '两种有什么区别？', 'second', task_input={})
    assert store.tasks.turn('session', 'user', 'second')['message'] == '两种有什么区别？'
    assert store.tasks.list('session', 'user')['tasks'] == []


def test_new_turn_fences_old_confirmation_and_old_job(tmp_path):
    store = SupportStore(f'sqlite:///{tmp_path / "support.db"}')
    store.ensure('session', 'user')
    state = store.receive('session', 'user', '退款', 'first', task_input={})
    guard = MutationGuard(CaseManager('user', str(tmp_path / 'cases')), 'session')
    with bot_scope(store, 'session', 'user', state['control_version']):
        token = guard.request_confirmation('refund_service', 'apply_refund', {'order_id': 'ORD-A', 'reason': '坏了'})
    updated = store.receive('session', 'user', '先不退了，查物流', 'second', task_input={})
    with pytest.raises(BotInterrupted):
        store.check_bot('session', 'user', state['control_version'])
    with bot_scope(store, 'session', 'user', updated['control_version']):
        assert guard.peek_confirmation(token) is None


def test_new_message_during_inflight_write_hands_off_without_rollback(tmp_path):
    store = SupportStore(f'sqlite:///{tmp_path / "support.db"}')
    store.ensure('session', 'user')
    state = store.receive('session', 'user', '退款', 'first', task_input={})
    store.start_bot('session', 'user', state['control_version'], 'first')
    store.begin_operation('session', 'user', state['control_version'], 'apply_refund')
    new = store.receive('session', 'user', '取消退款', 'second', task_input={})
    assert new['mode'] == 'handoff_pending'




def test_draft_claims_are_not_accepted_as_authoritative_evidence(tmp_path):
    from app.memory.case_manager import CaseResolution
    from app.reliability.evidence import UNVERIFIED_SENTENCE
    claim = {'success': True, 'case': {'draft': {'summary': '订单 ORD-A 已发货，金额 ¥129。'}}}
    client = Client([reply(tool='read_case_memory', args={'order_id': 'ORD-A'}), reply('订单 ORD-A 已发货，金额 ¥129。')])
    result = AgentEngine('order_inquiry').execute('发货了吗',
        react_executor=ReactExecutor(client, 'test', execute=lambda *a: json.dumps(claim)))
    assert UNVERIFIED_SENTENCE in result.reply
    manager = CaseManager('user', str(tmp_path))
    turn = manager.begin_turn('session', '发货了吗', CaseResolution())
    manager.record_tool_result('session', turn, 'read_case_memory', json.dumps(claim))
    assert manager.recent_tool_evidence('session') == []
    assert manager.cases == {}
