"""Execution handoff -> atomic question publication -> next user answer."""
import json
from types import SimpleNamespace

import pytest

from app.agents.reply_composition import compose
from app.tasks.replies import ReplyCoordinator
from app.tasks.repository import TaskRepository
from app.tasks.natural_reply import UNAVAILABLE_REPLY
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store
from tests.test_task_execution import Client, response, request, engine, local_budgets
from tests.test_message_association import batch_for, service, proposal, update


def handoff(refs=(), status='needs_input'):
    return response(json.dumps(dict(status=status,
        missing_information='需要区分具体订单' if status == 'needs_input' else None,
        candidate_refs=list(refs)), ensure_ascii=False))


def candidates(name, args):
    assert name == 'find_orders'
    return dict(success=True, data=dict(items=[
        dict(order_ref='ref-a', order_id='A', paid_amount='200.00', status='shipped',
             purchase_summary=[dict(name='红鞋', quantity=1)]),
        dict(order_ref='ref-b', order_id='B', paid_amount='400.00', status='paid',
             purchase_summary=[dict(name='红鞋', quantity=2)]),
    ]))


def execute(store, *, refs=('ref-a', 'ref-b'), query=True):
    record, source = task(store, 's', goal='查红鞋物流', details={'item_name': '红鞋'})
    responses = ([response(calls=[('find_orders', {'query': '红鞋'})])] if query else []) + [handoff(refs)]
    client = Client(*responses)
    result = engine(client).execute_task(store, 'alice', request(record, source), tools=candidates)
    assert result['state'] == 'needs_input' and result['question'] is None
    assert all('ask_user' not in {t['function']['name'] for t in r.get('tools', [])} for r in client.requests)
    assert TaskRepository(store, 'alice').get(record.task_id).status == 'in_progress'
    return record, source, result


class QuestionModel:
    def __init__(self, mutate=None):
        self.calls = 0
        self.mutate = mutate
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **params):
        self.calls += 1
        context = json.loads(params['messages'][1]['content'])
        matter = next(m for m in context['matters'] if m.get('clarification'))
        items = matter['clarification']['candidates']
        assert all('status' not in c for c in items)
        value = dict(reply='', claim_ids=[], question=dict(task_id=matter['task_id'],
            prompt='您买的是哪一笔？' if items else '您买的是什么商品？',
            options=[dict(label=f"{c['paid_amount']}元", ref=c['ref']) for c in items]))
        if self.mutate:
            self.mutate(value, context)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content=json.dumps(value, ensure_ascii=False), tool_calls=None))])


def coordinator(store, model):
    return ReplyCoordinator(store, 'alice', composer=lambda context: compose(model, 'test', context))


def test_published_question_and_answer_continue_same_task_with_bound_candidate(store):
    record, source, result = execute(store)
    model = QuestionModel()
    output = coordinator(store, model).publish('s')
    saved = TaskRepository(store, 'alice').get(record.task_id)
    assert saved.status == 'waiting_user' and saved.version == record.version + 1
    assert output['question_id'] == saved.waiting_for['question_id']
    assert output['task_version'] == saved.version
    assert output['content'] == '您买的是哪一笔？\n1. 200.00元；2. 400.00元'
    assert 'ref-a' not in output['content']
    assert saved.waiting_for['options'][0]['value'] == 'ref-a'
    assert coordinator(store, model).publish('s') is None and model.calls == 1

    batch = batch_for(store, '第一个')
    svc = service(store)
    svc.commit(batch, svc.snapshot(batch), proposal(update(batch, saved, effect='answer', question_id=output['question_id'])))
    current = TaskRepository(store, 'alice').get(record.task_id)
    assert current.status == 'in_progress' and current.waiting_for is None
    assert current.details['selected_candidate']['ref'] == 'ref-a'
    assert len(TaskRepository(store, 'alice').list(session_id='s')) == 1
    seen = []

    def select(params):
        view = json.loads(params['messages'][1]['content'])
        target = view['task']['details']['selected_candidate']['ref']
        return response(calls=[('get_logistics', {'target_ref': target})])

    def logistics(name, args):
        seen.append((name, args))
        return dict(success=True, data=dict(items=[]))

    source = dict(kind='message', session_id='s', sequence=batch.messages[0]['sequence'])
    out = engine(Client(select, handoff(status='ready'))).execute_task(store, 'alice', request(current, source), tools=logistics)
    assert out['state'] == 'ready' and out['reply'] == ''
    assert seen == [('get_logistics', {'target_ref': 'ref-a'})]
    batch = batch_for(store, '不是红鞋，是黑色外套')
    changed = update(batch, current, effect='amend')
    changed['goal'] = '查询黑色外套物流'
    svc.commit(batch, svc.snapshot(batch), proposal(changed))
    assert TaskRepository(store, 'alice').get(record.task_id).details['selected_candidate'] is None


def test_history_candidates_survive_live_fact_ttl_for_question_only(store):
    record, source, result = execute(store)
    with store.transaction() as tx:
        row = tx.one("SELECT sequence,payload FROM support_events WHERE kind='task_tool_result'")
        value = json.loads(row['payload'])
        value['observed_epoch'] -= 120
        tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
                   (json.dumps(value), 's', row['sequence']))
    model = QuestionModel()
    assert coordinator(store, model).publish('s')['question_id']


def test_next_execution_reuses_historical_refs_without_repeating_lookup(store):
    record, source = task(store, 's', goal='查红鞋物流', details={'item_name': '红鞋'})
    first = engine(Client(response(calls=[('find_orders', {'query': '红鞋'})]), handoff(status='ready')))
    first.execute_task(store, 'alice', request(record, source), tools=candidates)
    ReplyCoordinator(store, 'alice').publish('s')
    with store.transaction() as tx:
        row = tx.one("SELECT sequence,payload FROM support_events WHERE kind='task_tool_result'")
        value = json.loads(row['payload'])
        value['observed_epoch'] -= 120
        tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
                   (json.dumps(value), 's', row['sequence']))

    def reuse(params):
        view = json.loads(params['messages'][1]['content'])
        assert not view['recent_tool_evidence']
        assert view['order_lookup_history'][0]['evidence_id'] == value['event_id']
        return handoff(('ref-a', 'ref-b'))

    result = engine(Client(reuse)).execute_task(store, 'alice', request(record, source),
        tools=lambda *args: pytest.fail('Repeated database lookup'))
    assert result['state'] == 'needs_input' and result['tool_calls'] == 0
    assert coordinator(store, QuestionModel()).publish('s')['question_id']


def test_unknown_candidate_in_execution_handoff_is_not_sent_to_composer(store):
    record, _, _ = execute(store, refs=('other-user-ref',))
    model = QuestionModel()
    output = coordinator(store, model).publish('s')
    assert output['content'] == UNAVAILABLE_REPLY and model.calls == 0
    assert TaskRepository(store, 'alice').get(record.task_id).waiting_for is None


def test_answer_and_question_for_different_matters_share_one_generation(store):
    from tests.test_coordinated_replies import ready
    record, _, _ = execute(store)
    other, source = task(store, 's', goal='查询耳机发货状态', details={'order_id': 'ORD-B'})
    ready(store, other, source)

    def include_answer(value, context):
        value['claim_ids'] = [c['claim_id'] for m in context['matters'] for c in m.get('claims', [])]
        value['reply'] = '耳机记录显示已发货。'

    model = QuestionModel(include_answer)
    output = coordinator(store, model).publish('s')
    assert output['content'].startswith('耳机记录显示已发货。\n\n您买的是哪一笔？')
    assert len(output['result_ids']) == 2 and model.calls == 1
    assert output['task_id'] == record.task_id


def test_question_without_business_evidence_is_composed_and_persisted(store):
    record, _, _ = execute(store, refs=(), query=False)
    output = coordinator(store, QuestionModel()).publish('s')
    assert output['content'] == '您买的是什么商品？'
    assert TaskRepository(store, 'alice').get(record.task_id).status == 'waiting_user'


@pytest.mark.parametrize('bad', ['ref', 'task', 'missing'])
def test_bad_question_does_not_leave_an_unpublished_wait(store, bad):
    record, _, _ = execute(store)

    def mutate(value, context):
        if bad == 'ref':
            value['question']['options'][0]['ref'] = 'unqueried-other-user-ref'
        elif bad == 'task':
            value['question']['task_id'] = 'other-task'
        else:
            value['question'] = None

    output = coordinator(store, QuestionModel(mutate)).publish('s')
    assert output['content'] == UNAVAILABLE_REPLY and 'question_id' not in output
    saved = TaskRepository(store, 'alice').get(record.task_id)
    assert saved.status == 'in_progress' and saved.waiting_for is None


def test_new_input_during_composition_discards_question_and_wait(store):
    record, _, _ = execute(store)
    model = QuestionModel(lambda value, context: batch_for(store, '不是红鞋，是黑色外套'))
    assert coordinator(store, model).publish('s') is None
    saved = TaskRepository(store, 'alice').get(record.task_id)
    assert saved.version == record.version and saved.waiting_for is None


def test_publication_failure_rolls_back_question_and_version(store, monkeypatch):
    record, _, _ = execute(store)
    original = store._event

    def fail(tx, control, kind, actor, payload, **kwargs):
        if kind == 'message' and payload.get('question_id'):
            raise RuntimeError('publication failure')
        return original(tx, control, kind, actor, payload, **kwargs)

    monkeypatch.setattr(store, '_event', fail)
    with pytest.raises(RuntimeError, match='publication failure'):
        coordinator(store, QuestionModel()).publish('s')
    saved = TaskRepository(store, 'alice').get(record.task_id)
    assert saved.version == record.version and saved.waiting_for is None
    monkeypatch.setattr(store, '_event', original)
    assert coordinator(store, QuestionModel()).publish('s')['question_id']


def test_only_one_question_is_published_when_two_matters_need_input(store):
    first, _, _ = execute(store)
    second, _, _ = execute(store)
    model = QuestionModel()
    output = coordinator(store, model).publish('s')
    records = [TaskRepository(store, 'alice').get(r.task_id) for r in (first, second)]
    assert sum(r.status == 'waiting_user' for r in records) == 1
    assert output['question_id']
    assert coordinator(store, model).publish('s') is None and model.calls == 1
