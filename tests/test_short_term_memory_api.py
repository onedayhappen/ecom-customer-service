"""Short-term context reaches the actual chat path without mixing tasks or turns."""
import json

import pytest

from app.agents.engine import AgentEngine
from app.api import main
from app.config.settings import settings
from app.data_plan.enums import Intent
from app.execution.types import ExecutionResult
from app.intent.query_rewrite import rewrite_query as real_rewrite_query
from app.support.runtime import bot_scope, get_store

from tests.test_multiturn_tasks_api import api  # noqa: F401 - shared isolated API fixture


def _append_messages(store, sid, messages, *, task_id=None):
    with store.transaction() as tx:
        control = store._control(tx, sid, 'user')
        for role, content in messages:
            store._event(tx, control, 'message', 'user' if role == 'user' else 'bot',
                         {'role': role, 'content': content, 'task_id': task_id})


def _capture_execution(monkeypatch, *, question=None):
    calls = []

    def execute(engine, text='', **kwargs):
        calls.append({'text': text, **kwargs})
        return ExecutionResult(intent=engine.intent, execution=engine.plan.execution,
                               reply='请继续补充信息。' if question else '已收到。',
                               question=question)

    monkeypatch.setattr(AgentEngine, 'execute', execute)
    return calls


def test_current_job_event_is_excluded_without_losing_an_earlier_identical_message(api):
    _, _, sid, _, _, _ = api
    store = get_store()
    store.ensure(sid, 'user')
    text = '补充一句相同内容'
    _append_messages(store, sid, [('user', text), ('assistant', '已收到之前的补充。')])
    state = store.receive(sid, 'user', text, 'current-job')
    # A background task control can append public messages while this job runs.
    _append_messages(store, sid, [('user', '后到消息-LATE'), ('assistant', '后到答复-LATE')])

    with bot_scope(store, sid, 'user', state['route_version']):
        normalized, history = main._build_engine('user', sid, query=text)

    assert normalized == sid
    assert [m['content'] for m in history if m['role'] == 'user'] == [text]
    assert 'LATE' not in json.dumps(history, ensure_ascii=False)
    assert any(m['content'] == '已收到之前的补充。' for m in history)


def test_api_recalls_an_early_commitment_after_more_than_forty_messages(api, monkeypatch):
    _, _, sid, send, _, _ = api
    monkeypatch.setattr(settings, 'short_term_memory_enabled', True)
    monkeypatch.setattr(settings, 'short_term_recent_turns', 4)
    calls = _capture_execution(monkeypatch)
    store = get_store()
    store.ensure(sid, 'user')
    _append_messages(store, sid, [
        ('user', '请确认回电安排。'),
        ('assistant', '已承诺周五回电，核验码 EARLY-CALLBACK。'),
        *[(role, f'无关信息第 {index} 轮。') for index in range(30)
          for role in ('user', 'assistant')],
    ])

    response = send('之前承诺的时间是什么？')

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    history = calls[0]['messages']
    assert 'EARLY-CALLBACK' in json.dumps(history, ensure_ascii=False)
    assert sum(m['role'] == 'user' and m['content'] == '之前承诺的时间是什么？'
               for m in history) == 1
    # The public transcript remains complete independently of model context.
    assert len(store.public_history(sid, 'user')) == 64


def test_newly_adopted_task_rebuilds_context_and_keeps_its_first_user_event(api, monkeypatch):
    import app.intent

    _, _, sid, send, tasks, _ = api
    monkeypatch.setattr(settings, 'short_term_memory_enabled', True)
    monkeypatch.setattr(app.intent, 'recognize', lambda *args, **kwargs: Intent.ISSUE_REPORT)
    calls = _capture_execution(monkeypatch, question={
        'field': 'details', 'prompt': '具体有哪些异常？', 'options': [],
    })
    store = get_store()
    store.ensure(sid, 'user')
    _append_messages(store, sid, [('user', '另一个已结束的问题是 ORD-OLD。'),
                                  ('assistant', '已记录 ORD-OLD。')])

    response = send('耳机坏了')

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert 'ORD-OLD' not in json.dumps(calls[0]['messages'], ensure_ascii=False)
    assert sum(m['role'] == 'user' and m['content'] == '耳机坏了'
               for m in calls[0]['messages']) == 1
    task = tasks()[0]
    history = store.tasks.context(sid, 'user', task['task_id'])
    assert any(m['role'] == 'user' and m['content'] == '耳机坏了' for m in history)
    assert 'ORD-OLD' not in json.dumps(history, ensure_ascii=False)


@pytest.mark.parametrize(('current_message', 'expected_order'), [
    ('那它到哪了', 'ORD-CURRENT'),
    ('请查 ORD-EXPLICIT', 'ORD-EXPLICIT'),
])
def test_task_order_slot_survives_history_eviction_and_respects_current_explicit_order(
    current_message, expected_order,
):
    history = [{'role': 'system', 'name': 'short_term_summary',
                'content': '历史对话曾提及 ORD-OBSOLETE，但当前任务已更换订单。'},
               *[{'role': 'user', 'content': f'无订单号的后续说明 {i}'} for i in range(45)]]
    result = real_rewrite_query(current_message, intent=Intent.LOGISTICS_INQUIRY,
                                case_manager=None, session_context=history,
                                task_order_id='ORD-CURRENT')
    assert result.entities.order_id == expected_order
    assert expected_order in result.message
    assert 'ORD-OBSOLETE' not in result.message


def test_api_passes_active_task_slot_to_query_rewrite(api, monkeypatch):
    from app.intent import query_rewrite

    _, _, sid, send, tasks, _ = api
    _capture_execution(monkeypatch)
    store = get_store()
    store.ensure(sid, 'user')
    with store.transaction() as tx:
        control = store._control(tx, sid, 'user')
        task = store.tasks.create(tx, control,
                                  {'intent': 'logistics_inquiry', 'text': '查物流'}, 'user')
        task['slots']['order_id'] = 'ORD-CURRENT'
        task['status'] = 'paused'
        store.tasks.save(tx, control, task, 'user')
    _append_messages(store, sid,
                     [(role, f'没有订单号的补充 {i}') for i in range(25)
                      for role in ('user', 'assistant')], task_id=task['task_id'])
    captured = []

    def rewrite(message, **kwargs):
        captured.append(kwargs)
        return query_rewrite.RewriteOutcome(message=message)

    monkeypatch.setattr(query_rewrite, 'rewrite_query', rewrite)
    response = send('继续物流', task_action='resume', task_id=task['task_id'],
                    task_version=task['version'])

    assert response.status_code == 200, response.text
    assert captured[0]['task_order_id'] == 'ORD-CURRENT'
    assert tasks()[0]['task_id'] == task['task_id']


def test_context_budget_failure_pauses_task_with_a_user_facing_reply(api, monkeypatch):
    from app.session.request_budget import ContextBudgetExceeded

    _, _, sid, send, tasks, _ = api

    def too_large(*args, **kwargs):
        raise ContextBudgetExceeded(20000, 12000)

    monkeypatch.setattr(AgentEngine, 'execute', too_large)
    response = send('查物流 ORD-CURRENT')

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['task_outcome'] == 'paused'
    assert payload['requires_human'] is False
    assert '信息过多' in payload['reply'] and '简要' in payload['reply']
    assert tasks()[0]['status'] == 'paused'
    assert get_store().public_history(sid, 'user')[-1]['content'] == payload['reply']
