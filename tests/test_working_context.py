"""Shared dialogue, task boundaries and evidence reuse in actual ReAct requests."""
from copy import deepcopy
import json

import pytest

from app.config.settings import settings
from app.memory.task_context import TaskMemory
from app.session.short_term import ContextService
from app.support.store import SupportError
from app.tasks.execution_store import TaskRuns
from tests.test_task_candidates import task
from tests.test_message_association import store
from tests.test_task_storage import empty_store, message
from tests.test_task_execution import Client, engine, request, response, local_budgets


def assistant(store, text, sid='s', private=False):
    with store.transaction() as tx:
        control = store._control(tx, sid, 'alice')
        return store._event(tx, control, 'message', 'bot', dict(role='assistant', content=text), private=private)


def test_shared_recent_dialogue_keeps_question_and_other_task_without_duplicate_sources(store):
    current, source = task(store, 's', goal='查询耳机物流')
    other, _ = task(store, 's', goal='键盘退款先别提交', details={'order_id': 'ORD-B'})
    assistant(store, '你指耳机还是键盘？')
    message(store, 's', 'alice', '第二个，只查询。')
    assistant(store, 'PRIVATE-MARKER', private=True)
    message(store, 'other', 'bob', 'BOB-MARKER')
    snapshots = []
    def inspect(params):
        snapshots.append(deepcopy(params))
        return response('需要核实。')
    result = engine(Client(inspect)).execute_task(store, 'alice', request(current, source))
    assert result['state'] == 'ready'
    view = json.loads(snapshots[0]['messages'][1]['content'])
    assert [m['content'] for m in view['recent_dialogue']] == [
        '查询耳机物流', '键盘退款先别提交', '你指耳机还是键盘？', '第二个，只查询。']
    assert view['task']['task_id'] == current.task_id != other.task_id
    assert view['sources'] == []
    assert view['source_refs'] == [dict(session_id='s', sequence=source['sequence'])]
    assert 'PRIVATE-MARKER' not in str(view) and 'BOB-MARKER' not in str(view)
    with pytest.raises(SupportError):
        ContextService(store).recent_dialogue('other', 'alice')


def test_recent_dialogue_retains_whole_exchanges_and_source_invalidation(store, monkeypatch):
    monkeypatch.setattr(settings, 'short_term_recent_turns', 1)
    message(store, 's', 'alice', '旧问题')
    assistant(store, '旧回答')
    message(store, 's', 'alice', '耳机还是键盘？')
    seq = assistant(store, '选项一耳机，选项二键盘')
    message(store, 's', 'alice', '第二个')
    recent = ContextService(store).recent_dialogue('s', 'alice')
    assert [m['content'] for m in recent] == ['耳机还是键盘？', '选项一耳机，选项二键盘', '第二个']
    memory = TaskMemory(store, 'alice')
    memory.watch_sources([dict(session_id=m['session_id'], sequence=m['sequence'],
                               role=m['role'], text=m['content']) for m in recent])
    assert memory.current()
    with store.transaction() as tx:
        tx.execute("UPDATE support_events SET visibility='private' WHERE session_id='s' AND sequence=?", (seq,))
    assert not memory.current()


def test_context_read_does_not_repeat_historical_receipt_or_persist_it_as_new_evidence(store):
    current, source = task(store, 's', goal='查询订单', details={'order_id': 'ORD-A'})
    proof = 'EVIDENCE-UNIQUE-' + 'x' * 2000
    seed = Client(response(calls=[('get_order', {'order_id': 'ORD-A'})]), response('已查询。'))
    engine(seed).execute_task(store, 'alice', request(current, source), tools=lambda *args:
        {'success': True, 'data': {'order_id': 'ORD-A', 'status': 'paid', 'description': proof}})
    historical = TaskRuns(store, 'alice').evidence(current)
    assert len(historical) == 1
    snapshots = []
    def inspect(params):
        snapshots.append(deepcopy(params))
        return response('已查询。')
    client = Client(response(calls=[('read_task_context', {})]), inspect)
    result = engine(client).execute_task(store, 'alice', request(current, source))
    assert result['state'] == 'ready'
    visible = snapshots[0]['messages']
    assert json.dumps(visible, ensure_ascii=False).count(proof) == 1
    delta = json.loads(next(m['content'] for m in visible if m['role'] == 'tool'))
    assert delta['data']['unchanged'] is True
    assert delta['data']['changes'] == {}
    assert all(e['tool'] != 'read_task_context' for e in TaskRuns(store, 'alice').evidence(current))


def test_repeated_business_read_references_original_message_without_copying_body(store):
    current, source = task(store, 's', goal='查询订单', details={'order_id': 'ORD-A'})
    proof = 'BUSINESS-UNIQUE-' + 'z' * 1500
    called, snapshots = [], []
    def tool(name, args):
        called.append(name)
        return {'success': True, 'data': {'order_id': 'ORD-A', 'description': proof}}
    def inspect(params):
        snapshots.append(deepcopy(params))
        return response('已查询。')
    call = lambda: response(calls=[('get_order', {'order_id': 'ORD-A'})])
    result = engine(Client(call(), call(), inspect)).execute_task(store, 'alice', request(current, source), tools=tool)
    assert result['state'] == 'ready' and called == ['get_order']
    messages = snapshots[0]['messages']
    assert json.dumps(messages, ensure_ascii=False).count(proof) == 1
    outputs = [m for m in messages if m['role'] == 'tool']
    assert json.loads(outputs[1]['content'])['result_tool_call_id'] == outputs[0]['tool_call_id']
    assert outputs[1]['tool_call_id'] != outputs[0]['tool_call_id']


def test_current_tool_evidence_is_referenced_by_context_delta(store):
    from app.tools.registry import execute_tool
    current, source = task(store, 's', goal='查询订单', details={'order_id': 'ORD-A'})
    proof = 'CURRENT-RECEIPT-UNIQUE'
    snapshots = []
    def tools(name, args):
        if name == 'read_task_context':
            return execute_tool(name, args)
        return {'success': True, 'data': {'order_id': 'ORD-A', 'description': proof}}
    def inspect(params):
        snapshots.append(deepcopy(params))
        return response('已查询。')
    client = Client(response(calls=[('get_order', {'order_id': 'ORD-A'})]),
                    response(calls=[('read_task_context', {})]), inspect)
    result = engine(client).execute_task(store, 'alice', request(current, source), tools=tools)
    assert result['state'] == 'ready'
    messages = snapshots[0]['messages']
    assert json.dumps(messages, ensure_ascii=False).count(proof) == 1
    delta = json.loads([m for m in messages if m['role'] == 'tool'][-1]['content'])['data']
    assert list(delta['changes']) == ['current_evidence_refs']
    assert delta['changes']['current_evidence_refs'][0]['tool'] == 'get_order'


def test_replanning_rebuilds_dialogue_and_context_delta_baseline(store):
    from tests.test_message_association import batch_for, service, update, proposal
    current, source = task(store, 's', goal='查询耳机物流')
    svc = service(store)
    snapshots = []
    def changed(params):
        batch = batch_for(store, '还要核实耳机是否开始派送')
        decision = update(batch, current, effect='supplement')
        decision['goal'] = '查询耳机物流，核实是否开始派送'
        svc.commit(batch, svc.snapshot(batch), proposal(decision))
        return response('过时的回复')
    def replanned(params):
        snapshots.append(deepcopy(params))
        return response(calls=[('read_task_context', {})])
    def final(params):
        snapshots.append(deepcopy(params))
        return response('需要查询最新状态。')
    result = engine(Client(response(calls=[('read_task_context', {})]), changed, replanned, final)).execute_task(
        store, 'alice', request(current, source))
    assert result['state'] == 'ready' and result['task_version'] == 2
    first = json.loads(snapshots[0]['messages'][1]['content'])
    assert first['task']['version'] == 2
    assert first['recent_dialogue'][-1]['content'] == '还要核实耳机是否开始派送'
    assert not any(m['role'] == 'tool' for m in snapshots[0]['messages'])
    delta = json.loads([m for m in snapshots[1]['messages'] if m['role'] == 'tool'][-1]['content'])['data']
    assert delta['changes'] == {} and delta['unchanged']
