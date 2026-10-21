"""External progress must be supported by scope-specific business evidence."""
import json
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.support.store import SupportStore
from app.tasks.external_evidence import after_sale_observation, ExternalEvidenceError, finished, submission_only
from app.tasks.external_progress import ExternalProgress
from app.tasks.repository import TaskRepository
from tests.test_task_candidates import store
from tests.test_task_storage import empty_store, message
from tests.test_task_operations import flags, ready, confirm


@pytest.fixture(autouse=True)
def polling(monkeypatch):
    monkeypatch.setattr(settings, 'task_external_progress_enabled', True)
    monkeypatch.setattr(settings, 'task_external_interval_seconds', 1)
    monkeypatch.setattr(settings, 'task_external_max_attempts', 3)


def evidence(status='submitted', **fields):
    hour = {'submitted': '12', 'warehouse_received': '13', 'refunded': '14'}.get(status, '12')
    return dict(success=True, source='mysql', observed_at='2026-10-01T15:00:00+00:00', records=[dict(
        source='mysql_after_sales', observed_at=f'2026-10-01T{hour}:00:00+00:00',
        order_id='ORD-A', after_sale_id='AS-A', kind='refund', status=status, is_demo=False, **fields)])


class Business:
    def __init__(self, result=None):
        self.result, self.calls = result or evidence(), []

    def read(self, user, binding):
        from copy import deepcopy
        from app.tools.request_context import get_current_user_id
        assert user == 'alice' and get_current_user_id() == user
        self.calls.append(dict(binding))
        if isinstance(self.result, Exception):
            raise self.result
        result = deepcopy(self.result)
        for row in result['records']:
            if binding.get('operation_id'):
                row['request_id'] = binding['operation_id']
        return after_sale_observation(binding, result)


def wait(store):
    operations, record, _, card = ready(store)
    assert confirm(operations, card)['status'] == 'succeeded'
    return TaskRepository(store, 'alice').get(record.task_id), operations


def due(store):
    with store.transaction() as tx:
        tx.execute('UPDATE task_external_waits SET next_check=0')


def record(store, tid):
    return TaskRepository(store, 'alice').get(tid)


def test_application_return_and_settlement_are_distinct_and_survive_restart(store):
    initial, operations = wait(store)
    business = Business()
    worker = ExternalProgress(store, business=business)
    assert worker.tick() == ['updated']
    assert record(store, initial.task_id).status == 'waiting_external'
    assert '已受理' in store.public_history('s', 'alice')[-1]['content']
    business.result = evidence('warehouse_received')
    due(store)
    assert ExternalProgress(SupportStore(store.dsn), business=business).tick() == ['updated']
    assert record(store, initial.task_id).status == 'waiting_external'
    assert '仓库已确认收货' in store.public_history('s', 'alice')[-1]['content']
    business.result = evidence('refunded', settled_at='2026-10-01T11:00:00+00:00', transaction_id='TX-A')
    due(store)
    assert worker.tick() == ['completed']
    final = record(store, initial.task_id)
    assert final.status == 'completed'
    assert final.waiting_for['external_progress']['transaction_id'] == 'TX-A'
    assert worker.tick() == []
    assert len(operations.business.writes) == 1 and len(business.calls) == 3


def test_duplicate_and_older_observations_do_not_repeat_reply_or_regress(store):
    initial, _ = wait(store)
    business = Business(evidence('warehouse_received'))
    worker = ExternalProgress(store, business=business)
    assert worker.tick() == ['updated']
    version, history = record(store, initial.task_id).version, store.public_history('s', 'alice')
    due(store)
    assert worker.tick() == ['unchanged']
    business.result = evidence('submitted')
    due(store)
    assert worker.tick() == ['retry']
    assert record(store, initial.task_id).version == version
    assert store.public_history('s', 'alice') == history
    due(store)
    assert worker.tick() == []
    assert store.state('s', 'alice')['mode'] == 'handoff_pending'
    assert len(business.calls) == 3


def test_uncertain_poll_does_not_retry_write_and_has_finite_attempts(store):
    initial, operations = wait(store)
    business = Business(TimeoutError('lost query result'))
    worker = ExternalProgress(store, business=business)
    for _ in range(3):
        due(store)
        assert worker.tick() == ['retry']
    due(store)
    assert worker.tick() == []
    assert store.state('s', 'alice')['mode'] == 'handoff_pending'
    assert record(store, initial.task_id).status == 'waiting_external'
    assert len(operations.business.writes) == 1


def test_expired_claim_is_fenced_and_restart_counts_original_attempt(store):
    initial, _ = wait(store)
    worker = ExternalProgress(store)
    worker.discover(4)
    first = worker.claim(initial.task_id, 'alice')
    assert first and worker.claim(initial.task_id, 'alice') is None
    with store.transaction() as tx:
        tx.execute('UPDATE task_external_waits SET lease_until=0')
    restarted = ExternalProgress(SupportStore(store.dsn))
    second = restarted.claim(initial.task_id, 'alice')
    assert second['attempts'] == 2 and second['lease_owner'] != first['lease_owner']
    binding = json.loads(first['binding'])
    value = after_sale_observation(binding, evidence(request_id=binding['operation_id']))
    assert worker.apply(first, value) == 'stale'
    assert restarted.apply(second, value) == 'updated'


def test_silence_and_thanks_do_not_finish_a_refund_and_pause_stops_polling(store):
    initial, _ = wait(store)
    source = message(store, 's', 'alice', '谢谢')
    repo = TaskRepository(store, 'alice')
    updated = repo.update(initial.task_id, 's', expected_version=initial.version, change_id='thanks',
                          source=source, patch={'details': initial.details})
    business = Business()
    worker = ExternalProgress(store, business=business)
    assert worker.tick() == ['updated']
    current = repo.get(initial.task_id)
    assert current.status == 'waiting_external'
    repo.update(current.task_id, 's', expected_version=current.version, change_id='pause',
                source=message(store, 's', 'alice', '先暂停'), patch={'status': 'paused'})
    due(store)
    assert worker.tick() == [] and len(business.calls) == 1


def test_pending_correction_and_handoff_stop_publication_of_inflight_poll(store):
    from app.tasks.intake import MessageIntake
    initial, _ = wait(store)
    worker = ExternalProgress(store)
    worker.discover(4)
    lease = worker.claim(initial.task_id, 'alice')
    MessageIntake(store).receive('s', 'alice', request_id='correction', content='不是这笔订单')
    binding = json.loads(lease['binding'])
    value = after_sale_observation(binding, evidence(request_id=binding['operation_id']))
    assert worker.apply(lease, value) == 'stale'
    assert record(store, initial.task_id).version == initial.version
    store.handoff('s', 'alice')
    due(store)
    assert worker.tick() == []


def test_only_explicit_submission_goal_can_finish_on_application_receipt(store):
    initial, _ = wait(store)
    repo = TaskRepository(store, 'alice')
    source = message(store, 's', 'alice', '只需要提交退款申请，后面我自己跟进')
    repo.update(initial.task_id, 's', expected_version=initial.version, change_id='submission-only',
                source=source, patch={'goal': '只需要提交退款申请'})
    worker = ExternalProgress(store, business=Business())
    assert worker.tick() == ['completed']
    assert '到账' not in store.public_history('s', 'alice')[-1]['content']


@pytest.mark.parametrize('fields', [dict(order_id='ORD-B'), dict(source='user_message'), dict(status='completed')])
def test_unverified_scope_source_and_generic_complete_are_rejected(fields):
    result = evidence()
    result['records'][0].update(fields)
    with pytest.raises((ExternalEvidenceError, ValueError)):
        after_sale_observation(dict(order_id='ORD-A', kind='refund'), result)


def test_partial_item_and_application_ids_cannot_borrow_order_wide_progress():
    for scope in (dict(item_id='ITEM-A'), dict(item_name='耳机'), dict(sku_id='SKU-A'), dict(after_sale_id='AS-B'), dict(quantity=1)):
        with pytest.raises(ExternalEvidenceError):
            after_sale_observation(dict(order_id='ORD-A', kind='refund', **scope), evidence())
    result = evidence(item_id='ITEM-A', quantity=1)
    value = after_sale_observation(dict(order_id='ORD-A', kind='refund', item_id='ITEM-A', quantity=1), result)
    assert value['reference'] == 'AS-A'


def test_refunded_without_settlement_proof_is_not_money_received():
    binding = dict(order_id='ORD-A', kind='refund', completion='refund_settled')
    value = after_sale_observation(binding, evidence('refunded'))
    assert not finished(binding, value) and '尚缺资金到账凭据' in value['content']
    assert not submission_only('不只是提交退款申请，还要跟进到账')
    assert not submission_only('帮我解决退款问题')


def test_real_deadlines_preserved_but_sensitive_materials_rejected():
    binding = dict(order_id='ORD-A', kind='refund')
    value = after_sale_observation(binding, evidence('awaiting_return',
        deadline='2026-10-10T12:00:00+00:00', required_materials=['商品照片']))
    assert value['deadline'] == '2026-10-10T12:00:00+00:00' and value['required_materials'] == ['商品照片']
    with pytest.raises(ExternalEvidenceError):
        after_sale_observation(binding, evidence(required_materials=['支付密码']))


def test_prior_application_completion_cannot_finish_a_new_operation():
    binding = dict(order_id='ORD-A', kind='refund', operation_id='new-operation', completion='refund_settled')
    old = evidence('refunded', request_id='old-operation',
                   settled_at='2026-10-01T11:00:00+00:00', transaction_id='old-transaction')
    with pytest.raises(ExternalEvidenceError):
        after_sale_observation(binding, old)
    old['current_order_refund'] = dict(source='mysql_order_fields', status='submitted')
    current = after_sale_observation(binding, old)
    assert current['phase'] == 'submitted' and not finished(binding, current)


def test_receipt_and_publication_rollback_together(store, monkeypatch):
    initial, _ = wait(store)
    worker = ExternalProgress(store, business=Business())
    original = store._event
    def fail(tx, control, kind, actor, payload, **kwargs):
        if payload.get('external_event_id'):
            raise RuntimeError('notification transaction failed')
        return original(tx, control, kind, actor, payload, **kwargs)
    monkeypatch.setattr(store, '_event', fail)
    assert worker.tick() == ['retry']
    assert record(store, initial.task_id).version == initial.version
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM task_external_receipts')['n'] == 0
    monkeypatch.setattr(store, '_event', original)
    due(store)
    assert worker.tick() == ['updated']


def test_schema_upgrade_failure_preserves_old_wait_and_retries(store, monkeypatch):
    from app.tasks import migrations
    initial, _ = wait(store)
    with store.transaction() as tx:
        tx.execute('DROP TABLE task_external_receipts')
        tx.execute('DROP TABLE task_external_waits')
        tx.execute('DELETE FROM support_schema_versions WHERE version>18')
        tx.execute("INSERT INTO support_schema_versions VALUES (18,'before') ON CONFLICT DO NOTHING")
    original = migrations.migrate_external_progress
    def fail(tx):
        original(tx)
        raise RuntimeError('simulated upgrade failure')
    monkeypatch.setattr(migrations, 'migrate_external_progress', fail)
    upgraded = SupportStore(store.dsn)
    with pytest.raises(RuntimeError):
        upgraded.initialize()
    monkeypatch.setattr(migrations, 'migrate_external_progress', original)
    upgraded.initialize()
    SupportStore(store.dsn).initialize()
    assert record(upgraded, initial.task_id).waiting_for == initial.waiting_for
    assert ExternalProgress(upgraded, business=Business()).tick() == ['updated']


def claim_in_process(args):
    dsn, tid = args
    return ExternalProgress(SupportStore(dsn)).claim(tid, 'alice') is not None


def test_multiple_processes_reserve_only_one_external_read(store):
    if not store.postgres:
        pytest.skip('PostgreSQL process concurrency check')
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context
    initial, _ = wait(store)
    ExternalProgress(store).discover(4)
    with ProcessPoolExecutor(2, mp_context=get_context('spawn')) as pool:
        results = list(pool.map(claim_in_process, [(store.dsn, initial.task_id)] * 2))
    assert sum(results) == 1


@pytest.mark.parametrize('supplement', [False, True])
def test_explicit_resume_after_query_limit_starts_a_new_bounded_attempt(store, monkeypatch, supplement):
    monkeypatch.setattr(settings, 'task_external_max_attempts', 1)
    initial, _ = wait(store)
    worker = ExternalProgress(store, business=Business(TimeoutError()))
    assert worker.tick() == ['retry']
    due(store)
    assert worker.tick() == [] and store.state('s', 'alice')['mode'] == 'handoff_pending'
    store.resume('s', 'alice')
    if supplement:
        current = record(store, initial.task_id)
        TaskRepository(store, 'alice').update(current.task_id, 's', expected_version=current.version,
            change_id='after-resume', source=message(store, 's', 'alice', '请再帮我查一下进度'), patch={'details': current.details})
    assert ExternalProgress(store, business=Business()).tick() == ['updated']
    assert record(store, initial.task_id).status == 'waiting_external'


def read_wait(store, goal, details, name, result):
    from tests.test_task_candidates import task
    from tests.test_task_execution import engine, Client, response, request
    current, _ = task(store, 's', goal=goal, details=details)
    source = message(store, 's', 'alice', goal)
    current = TaskRepository(store, 'alice').update(current.task_id, 's', expected_version=current.version,
        change_id=str(uuid4()), source=source, patch={'goal': goal})
    def save(params):
        value = json.loads(next(m['content'] for m in reversed(params['messages']) if m['role'] == 'tool'))
        return response(calls=[('save_case_context', dict(expected_version=current.version, state='waiting_external',
            wait_reason='等待业务进展', evidence_id=value['_task_evidence']['event_id']))])
    reply = engine(Client(response(calls=[(name, {'order_id': details['order_id']})]), save)).execute_task(
        store, 'alice', request(current, source), tools=lambda *_: result)
    assert reply['state'] == 'waiting_external'
    return TaskRepository(store, 'alice').get(current.task_id)


def test_query_after_sale_harness_and_warehouse_goal_have_exact_business_scope(store):
    result = evidence('return_shipped')
    result['records'][0].update(kind='return', item_id='ITEM-A')
    current = read_wait(store, '确认仓库收货即可', {'order_id': 'ORD-A', 'item_id': 'ITEM-A', 'after_sale_id': 'AS-A'},
                        'query_after_sale', result)
    delivered = evidence('warehouse_received')
    delivered['records'][0].update(kind='return', item_id='ITEM-A')
    worker = ExternalProgress(store, business=Business(delivered))
    assert worker.tick() == ['completed']
    assert record(store, current.task_id).status == 'completed'
    content = store.public_history('s', 'alice')[-1]['content']
    assert '仓库已确认收货' in content and '退款结果仍待确认' in content


def test_signed_delivery_dispute_preserves_customer_statement_and_hands_off(store):
    from app.tasks.external_evidence import observation
    current = read_wait(store, '物流显示签收但我没收到', {'order_id': 'ORD-A', 'tracking_number': 'TRACK-A'},
        'query_logistics', dict(success=True, logistics=dict(order_id='ORD-A', tracking_number='TRACK-A', status='运输中')))
    class Logistics:
        def read(self, user, binding):
            return observation(binding, phase='delivered', rank=30, content='物流系统记录这票包裹已签收。',
                source='mysql_logistics', observed_at='2026-10-01T12:00:00+00:00', reference='TRACK-A')
    assert ExternalProgress(store, business=Logistics()).tick() == ['updated']
    assert record(store, current.task_id).status == 'waiting_external'
    assert store.state('s', 'alice')['mode'] == 'handoff_pending'
    history = store.public_history('s', 'alice')
    assert any('没收到' in m['content'] and m['role'] == 'user' for m in history)
    assert any('系统记录这票包裹已签收' in m['content'] for m in history)


def test_same_timestamp_conflict_does_not_override_last_progress(store):
    initial, _ = wait(store)
    business = Business(evidence('submitted'))
    worker = ExternalProgress(store, business=business)
    assert worker.tick() == ['updated']
    business.result = evidence('approved')  # same source time, different fact
    due(store)
    assert worker.tick() == ['retry']
    assert record(store, initial.task_id).waiting_for['external_progress']['phase'] == 'submitted'


def test_submission_receipt_completes_only_submission_goal_without_an_extra_query(store):
    from app.tasks.operations import TaskOperations
    from tests.test_task_operations import Business as OperationsBusiness
    from tests.test_task_candidates import task
    current, _ = task(store, 's', goal='提交退款申请', details={'order_id': 'ORD-A', 'reason': '不想要了'})
    current = TaskRepository(store, 'alice').update(current.task_id, 's', expected_version=current.version,
        change_id='submit-goal', source=message(store, 's', 'alice', '帮我提交退款申请'), patch={'goal': current.goal})
    operations = TaskOperations(store, 'alice', business=OperationsBusiness())
    operations.prepare('s', current.task_id, current.version, 'apply_refund')
    card = operations.list('s')[0]
    assert confirm(operations, card)['status'] == 'succeeded'
    assert record(store, current.task_id).status == 'completed'
    business = Business()
    assert ExternalProgress(store, business=business).tick() == [] and not business.calls
    assert '实际到账仍待确认' in store.public_history('s', 'alice')[-1]['content']


def test_progress_does_not_wake_empty_agent_or_publish_a_second_unverified_reply(store):
    from app.tasks.conversation_worker import ConversationWorker
    from app.tasks.replies import ReplyCoordinator
    from tests.test_task_execution import engine, Client
    current, _ = wait(store)
    worker = ExternalProgress(store, business=Business())
    assert worker.tick() == ['updated']
    before = store.public_history('s', 'alice')
    agent_worker = ConversationWorker(store, engine(Client()))
    try:
        agent_worker.reconcile()
        assert ReplyCoordinator(store, 'alice').publish('s') is None
        assert store.public_history('s', 'alice') == before
        with store.transaction(read_only=True) as tx:
            assert not tx.one("SELECT job_id FROM runtime_jobs WHERE kind='task_execution' AND status IN ('pending','running')")
    finally:
        agent_worker.close()


def test_later_full_refund_request_overrides_an_earlier_submission_only_request(store):
    from app.tasks.external_progress import only_submission_requested
    initial, _ = wait(store)
    repo = TaskRepository(store, 'alice')
    initial = repo.update(initial.task_id, 's', expected_version=initial.version, change_id='old-request',
        source=message(store, 's', 'alice', '只需要提交退款申请'), patch={'goal': '提交退款申请'})
    # Even if the model fails to update the text goal, the newer linked request
    # must not be overridden by an older positive quote.
    initial = repo.update(initial.task_id, 's', expected_version=initial.version, change_id='latest-request',
        source=message(store, 's', 'alice', '不只是提交申请，还要跟进到账'), patch={'details': initial.details})
    with store.transaction(read_only=True) as tx:
        assert not only_submission_requested(tx, initial)
    assert ExternalProgress(store, business=Business()).tick() == ['updated']
    assert record(store, initial.task_id).status == 'waiting_external'


def test_timed_out_live_query_cannot_publish_its_late_result(store, monkeypatch):
    import threading
    monkeypatch.setattr(settings, 'task_external_timeout_seconds', .05)
    initial, _ = wait(store)
    release, finished_query = threading.Event(), threading.Event()
    class SlowBusiness(Business):
        def read(self, user, binding):
            try:
                release.wait(5)
                return super().read(user, binding)
            finally:
                finished_query.set()
    business = SlowBusiness(evidence('refunded', settled_at='2026-10-01T11:00:00+00:00', transaction_id='TX-LATE'))
    try:
        assert ExternalProgress(store, business=business).tick() == ['retry']
        assert record(store, initial.task_id).version == initial.version
    finally:
        release.set()
    assert finished_query.wait(3)
    assert record(store, initial.task_id).status == 'waiting_external'
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM task_external_receipts')['n'] == 0


def test_pending_messages_in_unrelated_human_session_do_not_stop_progress(store):
    from app.tasks.intake import MessageIntake
    initial, _ = wait(store)
    MessageIntake(store).receive('old', 'alice', request_id='other-human', content='另一件事请人工帮忙')
    store.handoff('old', 'alice')
    assert ExternalProgress(store, business=Business()).tick() == ['updated']
    assert record(store, initial.task_id).waiting_for['external_progress']['phase'] == 'submitted'
