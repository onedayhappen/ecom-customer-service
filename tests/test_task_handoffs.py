"""Human ownership, explicit handover updates and fresh task resumption."""
import json
from uuid import uuid4

import pytest

from app.support.store import SupportError, SupportStore
from app.tasks.execution_store import TaskRuns
from app.tasks.handoffs import TaskHandoffs
from app.tasks.intake import MessageIntake
from app.tasks.models import SupportSource
from app.tasks.repository import TaskRepository
from app.tasks.replies import ReplyCoordinator
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store, message
from tests.test_task_execution import request, engine, Client, response, local_budgets
from tests.test_coordinated_replies import ready
from tests.test_task_operations import flags


@pytest.fixture(autouse=True)
def staff(store):
    store.seed_staff('agent1', 'agent')
    store.seed_staff('agent2', 'agent')
    store.seed_staff('admin', 'admin')


def linked(store):
    with store.transaction() as tx:
        for sid in ('s', 'old'):
            tx.execute('INSERT INTO message_intake_sessions(session_id) VALUES (?) ON CONFLICT DO NOTHING', (sid,))
    record, source = task(store, 's')
    later = message(store, 'old', 'alice', '同一笔订单继续处理')
    record = TaskRepository(store, 'alice').update(record.task_id, 'old', expected_version=record.version,
        change_id=str(uuid4()), source=later, patch={'goal': record.goal})
    return record, source, later


def claim(store, sid='s'):
    ticket = store.handoff(sid, 'alice')['ticket']
    store.change(ticket['ticket_id'], 'agent1', ticket['version'], 'claim')
    return ticket['ticket_id']


def test_handoff_persists_task_and_stops_other_session_execution(store):
    record, source, later = linked(store)
    ready(store, record, source)
    tid = claim(store)
    detail = store.detail(tid, 'agent1')
    handover = detail['task_handovers'][0]['handover']
    assert handover['task']['task_id'] == record.task_id
    assert handover['task']['goal'] == record.goal
    assert {m['session_id'] for m in handover['customer_statements']} == {'s', 'old'}
    assert handover['observed_evidence'][0]['facts']['order_id'] == 'ORD-A'
    req = request(record, later)
    req['session_id'] = 'old'
    with pytest.raises(SupportError, match='人工'):
        engine(Client()).execute_task(SupportStore(store.dsn), 'alice', req)
    assert ReplyCoordinator(store, 'alice').publish('s') is None
    progress = MessageIntake(store).progress('old', 'alice')
    assert progress['processing'] == 'waiting_human' and progress['human_tasks'] == [record.task_id]
    assert len(store.detail(tid, 'agent1')['task_handovers']) == 1
    assert store.handoff('s', 'alice')['ticket']['ticket_id'] == tid


def test_only_last_explicit_resume_releases_task_and_changes_version(store):
    record, source, _ = linked(store)
    first, second = claim(store), claim(store, 'old')
    ticket = store.detail(first, 'agent1')['ticket']
    store.change(first, 'agent1', ticket['version'], 'resolved')
    assert store.state('s', 'alice')['mode'] == 'human'
    with pytest.raises(SupportError):
        engine(Client()).execute_task(store, 'alice', request(record, source))
    store.resume('s', 'alice')
    assert TaskRepository(store, 'alice').get(record.task_id).version == record.version
    assert MessageIntake(store).progress('s', 'alice')['processing'] == 'waiting_human'
    store.resume('old', 'alice')
    current = TaskRepository(store, 'alice').get(record.task_id)
    assert current.version == record.version + 1
    assert engine(Client(response('需要重新查询订单。'))).execute_task(store, 'alice', request(current, source))['state'] == 'ready'
    store.resume('old', 'alice')
    assert TaskRepository(store, 'alice').get(record.task_id).version == current.version
    assert not store.detail(second, 'agent1')['task_handovers'][0]['active']


def test_staff_update_is_versioned_idempotent_and_used_after_resume(store):
    from app.memory.task_context import task_history
    record, source, _ = linked(store)
    tid = claim(store)
    handoffs = TaskHandoffs(store, 'alice')
    args = dict(request_id='update-one', task_id=record.task_id, expected_version=record.version,
                details={'reason': '左耳没有声音'}, goal='核实耳机售后')
    after = handoffs.update(tid, 'agent1', **args)
    assert handoffs.update(tid, 'agent1', **args) == after
    with pytest.raises(SupportError):
        handoffs.update(tid, 'agent1', **{**args, 'goal': '另一个要求'})
    store.reply(tid, 'agent1', '内部备注不得进入机器人上下文', 'private', private=True)
    store.reply(tid, 'agent1', '已记录您的耳机问题。', 'public')
    store.resume('s', 'alice')
    current = TaskRepository(store, 'alice').get(record.task_id)
    history = task_history(TaskRuns(store, 'alice'), current)
    assert history['human_handoff']['staff_updates'][-1]['patch']['details']['reason'] == '左耳没有声音'
    statement = history['human_handoff']['staff_statements'][-1]
    assert statement['content'] == '已记录您的耳机问题。' and statement['actor'] == 'agent1'
    assert statement['session_id'] == 's' and statement['sequence'] > 0
    assert '内部备注不得' not in json.dumps(history, ensure_ascii=False)
    def inspect(params):
        text = json.dumps(params['messages'], ensure_ascii=False)
        assert '左耳没有声音' in text and '人工更新' in text and '内部备注不得' not in text
        return response('我会根据补充重新核实。')
    result = engine(Client(inspect)).execute_task(store, 'alice', request(current, source))
    assert result['state'] == 'ready' and current.version == after.version + 1


def test_staff_permissions_and_scope_cannot_be_borrowed_for_updates(store):
    record, _, _ = linked(store)
    other, _ = task(store, 'old2', details={'order_id': 'ORD-OTHER'})
    tid = claim(store)
    handoffs = TaskHandoffs(store, 'alice')
    args = dict(request_id='update', task_id=record.task_id, expected_version=record.version, details={'reason': '有问题'})
    for actor in ('alice', 'agent2'):
        with pytest.raises(SupportError):
            handoffs.update(tid, actor, **args)
    with pytest.raises(SupportError):
        handoffs.update(tid, 'agent1', **{**args, 'task_id': other.task_id})
    with pytest.raises(SupportError):
        handoffs.update(tid, 'agent1', **{**args, 'details': {'authorized_compensation': 1000}})
    store.update_staff('admin', 'agent1', 'agent', False)
    with pytest.raises(SupportError):
        handoffs.update(tid, 'agent1', **args)


def test_support_source_requires_exact_recorded_task_and_cannot_create_another(store):
    record, source, _ = linked(store)
    tid = claim(store)
    after = TaskHandoffs(store, 'alice').update(tid, 'agent1', request_id='source',
        task_id=record.task_id, expected_version=record.version, details={'reason': '确认问题'})
    with store.transaction(read_only=True) as tx:
        row = tx.one("SELECT session_id,sequence FROM support_events WHERE kind='task_staff_update'")
    reference = dict(kind='support_event', **row, task_id=record.task_id)
    repo = TaskRepository(store, 'alice')
    other, _ = task(store, 'old2')
    with pytest.raises(SupportError):
        repo.update(other.task_id, 's', expected_version=other.version, change_id='bad', source=reference, patch={'goal': '篡改'})
    with pytest.raises(SupportError):
        repo.create('s', change_id='bad-new', source=reference, goal='借用授权')
    with pytest.raises(SupportError):
        repo.update(after.task_id, 's', expected_version=after.version, change_id='bad-event',
            source={**reference, 'sequence': source['sequence']}, patch={'goal': '篡改'})


def test_cross_session_unknown_operation_blocks_resume_and_never_repeats_write(store):
    from tests.test_task_operations import ready as prepare, confirm, change
    operations, record, _, card = prepare(store, sid='old')
    change(store, record, {'goal': record.goal}, 's')
    # Rebuild after changing the task version, then lose the write response.
    record = TaskRepository(store, 'alice').get(record.task_id)
    operations.business.uncertain = True
    operations.prepare('old', record.task_id, record.version, 'apply_refund')
    card = next(c for c in operations.list('old') if c['can_confirm'])
    assert confirm(operations, card, 'old')['status'] == 'unknown'
    tid = claim(store)
    with pytest.raises(SupportError, match='核实'):
        store.resume('s', 'alice')
    assert store.state('s', 'alice')['mode'] == 'human'
    assert operations.list('old')[0]['can_confirm'] is False
    assert len(operations.business.writes) == 1
    assert any(o['status'] == 'unknown' for o in store.detail(tid, 'agent1')['task_handovers'][0]['handover']['operations'])


def test_archived_handover_does_not_reveal_later_task_updates(store):
    record, source, _ = linked(store)
    tid = claim(store)
    store.resume('s', 'alice')
    current = TaskRepository(store, 'alice').get(record.task_id)
    TaskRepository(store, 'alice').update(record.task_id, 's', expected_version=current.version,
        change_id='later', source=source, patch={'details': {'order_id': 'ORD-NEW', 'reason': '后来新增信息'}})
    archived = store.detail(tid, 'agent1')['task_handovers'][0]
    assert archived['active'] is False
    assert '后来新增信息' not in json.dumps(archived, ensure_ascii=False)
    assert archived['handover']['task']['details']['order_id'] == 'ORD-A'


def test_migration_preserves_existing_handoff_and_rolls_back_atomically(store, monkeypatch):
    from app.tasks import migrations
    record, _, _ = linked(store)
    tid = claim(store)
    with store.transaction() as tx:
        tx.execute('DROP TABLE task_external_receipts')
        tx.execute('DROP TABLE task_external_waits')
        tx.execute('DROP TABLE task_handoffs')
        tx.execute('DELETE FROM support_schema_versions WHERE version>17')
        tx.execute("INSERT INTO support_schema_versions VALUES (17,'before-upgrade') ON CONFLICT DO NOTHING")
    original = migrations.migrate_task_handoffs
    def fail(tx):
        original(tx)
        raise RuntimeError('simulated failed handover migration')
    monkeypatch.setattr(migrations, 'migrate_task_handoffs', fail)
    upgraded = SupportStore(store.dsn)
    with pytest.raises(RuntimeError):
        upgraded.initialize()
    with upgraded._connection() as tx:
        assert tx.one('SELECT MAX(version) AS v FROM support_schema_versions')['v'] == 17
    monkeypatch.setattr(migrations, 'migrate_task_handoffs', original)
    upgraded.initialize()
    SupportStore(store.dsn).initialize()
    with upgraded.transaction(read_only=True) as tx:
        rows = tx.all('SELECT * FROM task_handoffs')
        assert len(rows) == 1 and rows[0]['ticket_id'] == tid and rows[0]['task_id'] == record.task_id
    assert upgraded.detail(tid, 'agent1')['task_handovers'][0]['active']


def test_agent_escalation_creates_one_real_ticket_and_returns_durable_receipt(store):
    record, source, _ = linked(store)
    req = request(record, source)
    client = Client(response(calls=[('escalate_to_human', {})]))
    result = engine(client).execute_task(store, 'alice', req)
    assert result['state'] == 'needs_human' and result['ticket_id']
    assert store.state('s', 'alice')['ticket']['ticket_id'] == result['ticket_id']
    assert engine(Client()).execute_task(store, 'alice', req) == result
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM support_tickets')['n'] == 1
        assert tx.one('SELECT task_id FROM task_handoffs')['task_id'] == record.task_id


def test_agent_cannot_escalate_another_session_or_claim_fake_transfer(store):
    record, source, _ = linked(store)
    def invalid(params):
        value = json.loads(next(m['content'] for m in reversed(params['messages']) if m['role'] == 'tool'))
        assert value['success'] is False and value['error'] == 'invalid_handoff_arguments'
        return response('已经转人工并完成赔付。')
    client = Client(response(calls=[('escalate_to_human', {'session_id': 'other'})]), invalid)
    engine(client).execute_task(store, 'alice', request(record, source))
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM support_tickets')['n'] == 0
    reply = ReplyCoordinator(store, 'alice').publish('s')
    assert '已经转人工' not in reply['content'] and '完成赔付' not in reply['content']


def test_failed_handoff_rolls_back_ticket_confirmation_and_job_together(store, monkeypatch):
    from tests.test_task_operations import ready as prepare
    from app.tasks.execution_models import TaskExecutionInput
    operations, record, source, card = prepare(store)
    req = TaskExecutionInput.model_validate(request(record, source))
    TaskRuns(store, 'alice').begin(req)
    original = store._event
    def fail(tx, control, kind, actor, payload, **kwargs):
        if kind == 'support_state' and payload.get('mode') == 'handoff_pending':
            raise RuntimeError('handoff event storage failed')
        return original(tx, control, kind, actor, payload, **kwargs)
    monkeypatch.setattr(store, '_event', fail)
    with pytest.raises(RuntimeError, match='storage failed'):
        store.handoff('s', 'alice')
    assert store.state('s', 'alice')['mode'] == 'bot'
    assert operations.list('s')[0]['can_confirm'] is True
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM task_handoffs')['n'] == 0
        assert tx.one('SELECT COUNT(*) AS n FROM support_tickets')['n'] == 0
        assert tx.one('SELECT status FROM runtime_jobs WHERE job_id=?', (req.job_id,))['status'] == 'running'


def test_concurrent_staff_updates_do_not_overwrite_each_other(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    record, _, _ = linked(store)
    tid = claim(store)
    barrier = Barrier(2)
    def change(reason):
        service = TaskHandoffs(SupportStore(store.dsn), 'alice')
        barrier.wait(10)
        try:
            return service.update(tid, 'agent1', request_id=reason, task_id=record.task_id,
                expected_version=record.version, details={'reason': reason})
        except SupportError as exc:
            return exc
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(change, ['左耳没有声音', '右耳没有声音']))
    assert sum(isinstance(x, SupportError) for x in outcomes) == 1
    current = TaskRepository(store, 'alice').get(record.task_id)
    assert current.version == record.version + 1
    assert current.details['reason'] == next(x.details['reason'] for x in outcomes if not isinstance(x, SupportError))
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM support_events WHERE kind='task_staff_update'")['n'] == 1


def test_resume_uses_fresh_query_and_never_trusts_staff_promise_as_authority(store):
    record, source, _ = linked(store)
    ready(store, record, source, total=12.5)
    tid = claim(store)
    store.reply(tid, 'agent1', '客服说可以额外赔付一千元。', 'promise')
    store.reply(tid, 'agent1', '不应进入交接的私有备注', 'internal', private=True)
    store.resume('s', 'alice')
    current = TaskRepository(store, 'alice').get(record.task_id)
    calls = []
    def query(name, args):
        calls.append(name)
        return {'success': True, 'order': {'order_id': 'ORD-A', 'status': '运输中', 'total': 39}}
    def inspect(params):
        context = json.dumps(params['messages'], ensure_ascii=False)
        assert '客服说可以额外赔付一千元' in context and '不是业务回执或权益授权' in context
        assert '不应进入交接的私有备注' not in context
        return response(calls=[('query_order', {'order_id': 'ORD-A'})])
    result = engine(Client(inspect, response('已赔付一千元'))).execute_task(store, 'alice', request(current, source), tools=query)
    assert result['state'] == 'ready' and calls == ['query_order']
    published = ReplyCoordinator(store, 'alice').publish('s')['content']
    assert '¥39.00' in published and '运输中' in published and '赔付' not in published


def test_staff_cannot_move_started_operation_and_retains_external_wait(store):
    from tests.test_task_operations import ready as prepare, confirm
    operations, record, _, card = prepare(store)
    assert confirm(operations, card)['status'] == 'succeeded'
    current = TaskRepository(store, 'alice').get(record.task_id)
    tid = claim(store)
    handoffs = TaskHandoffs(store, 'alice')
    with pytest.raises(SupportError, match='原对象'):
        handoffs.update(tid, 'agent1', request_id='move', task_id=record.task_id,
            expected_version=current.version, details={'order_id': 'ORD-B'})
    after = handoffs.update(tid, 'agent1', request_id='supplement', task_id=record.task_id,
        expected_version=current.version, details={'reason': '等待退款结果'})
    assert after.waiting_for == current.waiting_for and after.status == 'waiting_external'
    store.resume('s', 'alice')
    resumed = TaskRepository(store, 'alice').get(record.task_id)
    assert resumed.waiting_for['operation_id'] == card['operation_id']
    assert len(operations.business.writes) == 1
