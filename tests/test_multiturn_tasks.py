"""Task controls, durable questions and interrupted execution invariants."""
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.support.store import SupportStore, SupportError, BotInterrupted
from app.tasks.routing import parse


@pytest.fixture
def store(tmp_path):
    store = SupportStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    store.ensure('session', 'user')
    return store


def receive(store, text, data=None, job=None):
    job = job or str(uuid4())
    state = store.receive('session', 'user', text, job, task_input=data or {})
    return job, state, store.tasks.turn('session', 'user', job)


def finish(store, job, state, *, question=None, token=None):
    if store.bot_result('session', 'user', job):
        return
    store.start_bot('session', 'user', state['control_version'], job)
    store.publish_bot('session', 'user', state['control_version'], job, 'reply',
                      {'question': question, 'confirmation_token': token})


def tasks(store):
    return store.tasks.list('session', 'user')['tasks']


def ask_refund(store):
    job, state, route = receive(store, '我要退款')
    finish(store, job, state, question={'field': 'order_id', 'prompt': '哪笔订单？', 'options': [
        {'value': 'ORD-A', 'label': 'ORD-A：耳机'}, {'value': 'ORD-B', 'label': 'ORD-B：鞋子'},
    ]})
    return tasks(store)[0]


def test_answer_continues_original_task_after_restart(store):
    original = ask_refund(store)
    restored = SupportStore(store.dsn)
    job, state, route = receive(restored, 'ORD-A', {'task_action': 'answer', 'task_id': original['task_id'],
        'task_version': original['version'], 'question_id': original['question']['question_id']})
    assert route['task_id'] == original['task_id']
    assert route['intent'] == 'after_sale_request'
    assert '我要退款' in route['message'] and 'ORD-A' in route['message']
    finish(restored, job, state, token='confirmation')
    assert tasks(restored)[0]['status'] == 'waiting_confirmation'


def test_free_text_order_selection_and_invalid_answer(store):
    ask_refund(store)
    job, state, route = receive(store, '不知道')
    assert route.get('reply')
    finish(store, job, state)
    assert tasks(store)[0]['status'] == 'waiting_input'
    _, _, route = receive(store, '2')
    assert 'ORD-B' in route['message']


def test_change_mind_cancels_old_task_and_invalidates_old_question(store):
    original = ask_refund(store)
    job, state, route = receive(store, '不退款了，改查物流')
    assert route['intent'] == 'logistics_inquiry'
    assert [t['status'] for t in tasks(store)] == ['cancelled', 'ready']
    with pytest.raises(SupportError):
        receive(store, 'ORD-A', {'task_action': 'answer', 'task_id': original['task_id'],
            'task_version': original['version'], 'question_id': original['question']['question_id']})


def test_pause_other_task_and_resume_without_losing_question(store):
    first = ask_refund(store)
    job, state, _ = receive(store, '退款先放着，查另一单物流')
    finish(store, job, state)
    first = tasks(store)[0]
    assert first['status'] == 'paused' and first['question']
    _, _, route = receive(store, '继续退款', {'task_action': 'resume', 'task_id': first['task_id'], 'task_version': first['version']})
    assert route['reply'] == '哪笔订单？'
    assert tasks(store)[0]['status'] == 'waiting_input'


def test_multiple_todos_retained_and_only_one_active(store):
    job, state, route = receive(store, '查物流，再退款，还要开发票')
    assert len(tasks(store)) == 3
    assert route['intent'] == 'logistics_inquiry'
    assert [t['status'] for t in tasks(store)] == ['ready', 'queued', 'queued']
    finish(store, job, state)
    assert [t['status'] for t in tasks(store)] == ['completed', 'queued', 'queued']
    job, state, route = receive(store, '取消')
    assert route.get('reply')
    finish(store, job, state)
    question = store.tasks.list('session', 'user')['question']
    assert len(question['options']) == 2
    _, _, route = receive(store, question['options'][1]['value'], {'task_action': 'answer', 'question_id': question['question_id']})
    assert tasks(store)[2]['status'] == 'cancelled'
    assert tasks(store)[1]['status'] == 'queued'


def test_concurrent_answer_only_one_advances(store):
    task = ask_refund(store)
    def submit(order):
        try:
            receive(SupportStore(store.dsn), order, {'task_action': 'answer', 'task_id': task['task_id'],
                'task_version': task['version'], 'question_id': task['question']['question_id']})
            return True
        except SupportError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(submit, ['ORD-A', 'ORD-B'])) == 1


def test_cancel_running_task_fences_old_result_and_tool(store):
    job, state, _ = receive(store, '查物流 ORD-A')
    store.start_bot('session', 'user', state['control_version'], job)
    task = tasks(store)[0]
    receive(store, '取消', {'task_action': 'cancel', 'task_id': task['task_id'], 'task_version': task['version']})
    with pytest.raises(BotInterrupted): store.publish_bot('session', 'user', state['control_version'], job, 'late')
    with pytest.raises(BotInterrupted): store.begin_operation('session', 'user', state['control_version'], 'apply_refund')
    assert tasks(store)[0]['status'] == 'cancelled'


def test_cancel_inflight_mutation_hands_off_without_claiming_rollback(store):
    job, state, _ = receive(store, '退款 ORD-A')
    store.start_bot('session', 'user', state['control_version'], job)
    store.begin_operation('session', 'user', state['control_version'], 'apply_refund')
    _, result, _ = receive(store, '取消退款')
    assert result['mode'] == 'handoff_pending'
    assert tasks(store)[0]['status'] == 'needs_human'
    assert '无法保证撤回' in json.dumps(store.public_history('session', 'user'), ensure_ascii=False)


def test_cancel_after_effect_committed_does_not_promise_rollback(store):
    job, state, _ = receive(store, '退款 ORD-A')
    store.start_bot('session', 'user', state['control_version'], job)
    oid = store.begin_operation('session', 'user', state['control_version'], 'apply_refund')
    store.finish_operation(oid, {'success': True})
    _, result, route = receive(store, '取消退款')
    assert result['mode'] == 'bot'
    assert tasks(store)[0]['status'] == 'completed'
    assert '成功' in route['reply']


def test_handoff_freezes_questions_and_archived_ticket_uses_snapshot(store):
    ask_refund(store)
    store.seed_staff('agent', 'agent')
    ticket = store.handoff('session', 'user')['ticket']
    store.change(ticket['ticket_id'], 'agent', ticket['version'], 'claim')
    assert tasks(store)[0]['status'] == 'needs_human'
    assert store.detail(ticket['ticket_id'], 'agent')['tasks'][0]['question']
    task = tasks(store)[0]
    with pytest.raises(SupportError): receive(store, '继续', {'task_action': 'resume', 'task_id': task['task_id'], 'task_version': task['version']})
    store.resume('session', 'user')
    receive(store, '查物流 ORD-NEW')
    assert len(tasks(store)) == 2
    assert len(store.detail(ticket['ticket_id'], 'agent')['tasks']) == 1


def test_idempotent_retry_does_not_duplicate_todos(store):
    job, first, _ = receive(store, '查物流，再退款')
    _, second, _ = receive(store, '查物流，再退款', job=job)
    assert second['duplicate']
    assert second['control_version'] == first['control_version']
    assert len(tasks(store)) == 2


def test_task_scope_rejects_other_conversation(store):
    task = ask_refund(store)
    store.ensure('another', 'user')
    with pytest.raises(SupportError):
        store.receive('another', 'user', '取消', 'job', task_input={'task_action': 'cancel', 'task_id': task['task_id'], 'task_version': task['version']})
    with pytest.raises(SupportError): store.tasks.list('session', 'intruder')


def test_failed_worker_pauses_task_for_explicit_recovery(store):
    job, state, _ = receive(store, '查物流 ORD-A')
    store.start_bot('session', 'user', state['control_version'], job)
    store.release_bot('session', 'user', job)
    assert tasks(store)[0]['status'] == 'paused'


@pytest.mark.parametrize('text,action,target,specs', [
    ('不退款了，改查物流', 'cancel', 'after_sale_request', ['logistics_inquiry']),
    ('退款先放着，查物流', 'pause', 'after_sale_request', ['logistics_inquiry']),
    ('取消订单 ORD-A', None, None, ['order_cancel']),
    ('取消退款申请', 'cancel', 'after_sale_request', []),
    ('继续退款', 'resume', 'after_sale_request', []),
    ('查物流，再退款，还要开发票', None, None, ['logistics_inquiry', 'after_sale_request', 'invoice']),
    ('不要取消退款申请', None, None, []),
    ('查物流和申请退款', None, None, ['logistics_inquiry', 'after_sale_request']),
])
def test_control_parsing(text, action, target, specs):
    parsed = parse(text)
    assert (parsed['action'], parsed['target']) == (action, target)
    assert [s['intent'] for s in parsed['specs']] == specs


def test_cancelling_background_todo_does_not_interrupt_active_worker(store):
    job, state, _ = receive(store, '查物流，再退款')
    store.start_bot('session', 'user', state['control_version'], job)
    background = tasks(store)[1]
    _, new_state, _ = receive(store, '取消退款', {'task_action': 'cancel', 'task_id': background['task_id'], 'task_version': background['version']})
    assert new_state['control_version'] == state['control_version']
    store.check_bot('session', 'user', state['control_version'])
    store.publish_bot('session', 'user', state['control_version'], job, '物流结果', {})
    assert [t['status'] for t in tasks(store)] == ['completed', 'cancelled']


def test_modify_order_keeps_task_and_changes_effective_parameters(store):
    first = ask_refund(store)
    job, state, route = receive(store, '改成第二个')
    assert route['task_id'] == first['task_id'] and 'ORD-B' in route['message']
    finish(store, job, state, token='confirmation')
    _, _, route = receive(store, '改成ORD-C')
    assert 'ORD-C' in route['message'] and 'ORD-B' not in route['message']


def test_task_context_does_not_mix_other_todos(store):
    job, state, route = receive(store, '我要退款 ORD-A')
    refund = route['task_id']
    finish(store, job, state, token='confirmation')
    other_job, other_state, other_route = receive(store, '查物流 ORD-B')
    finish(store, other_job, other_state)
    assert 'ORD-B' not in json.dumps(store.tasks.context('session', 'user', refund))
    assert 'ORD-A' not in json.dumps(store.tasks.context('session', 'user', other_route['task_id']))


def test_answer_containing_another_intent_keyword_still_fills_current_task(store):
    job, state, route = receive(store, '我要退款 ORD-A')
    finish(store, job, state, question={'field': 'details', 'prompt': '退款原因是什么？', 'options': []})
    _, _, continued = receive(store, '快递太慢，商品已经不需要了')
    assert continued['task_id'] == route['task_id']
    assert len(tasks(store)) == 1
    assert '快递太慢' in continued['message']


def test_ambiguous_cancel_preserves_replacement_request_after_selection(store):
    job, state, _ = receive(store, '查物流，再退款 ORD-A，再退款 ORD-B')
    finish(store, job, state)
    job, state, _ = receive(store, '不退款了，改查物流 ORD-C')
    finish(store, job, state)
    question = store.tasks.list('session', 'user')['question']
    assert len(question['options']) == 2
    _, _, route = receive(store, question['options'][0]['value'],
                          {'task_action': 'answer', 'question_id': question['question_id']})
    assert route['intent'] == 'logistics_inquiry' and 'ORD-C' in route['message']
    assert [t['status'] for t in tasks(store)] == ['completed', 'cancelled', 'queued', 'ready']
