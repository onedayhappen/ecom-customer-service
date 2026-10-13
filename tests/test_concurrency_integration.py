"""Opt-in REAL PostgreSQL/Redis process tests; no FLUSHDB or business DB.

Use deploy/compose.integration.yml and ECOM_INTEGRATION=1. Each Redis test uses
a UUID namespace; PostgreSQL is the dedicated ecom_concurrency_test database.
"""
import json
import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from uuid import uuid4

import pytest

from app.runtime.chat_queue import ChatJob, ChatQueue
from app.runtime.outbox import OutboxDispatcher
from app.support.store import SupportStore, SupportError

DSN = 'postgresql://test_runner:isolated_test_only@127.0.0.1:25432/ecom_concurrency_test'
REDIS = 'redis://127.0.0.1:26379/0'
pytestmark = pytest.mark.skipif(os.getenv('ECOM_INTEGRATION') != '1', reason='dedicated integration environment only')


def dispatch_process(args):
    dsn, prefix = args
    store = SupportStore(dsn)
    dispatcher = OutboxDispatcher(store, ChatQueue(REDIS, prefix=prefix))
    count = 0
    while dispatcher.deliver_one():
        count += 1
    return count


def reserve_process(args):
    dsn, payload = args
    store = SupportStore(dsn)
    job = ChatJob(**payload)
    try:
        store.start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
        return True
    except SupportError:
        return False


def mutation_process(args):
    from app.memory.case_postgres import PostgresCaseManager
    from app.reliability.mutations import MutationGuard
    dsn, token = args
    store = SupportStore(dsn)
    guard = MutationGuard(PostgresCaseManager('integration-user', store), 'case-session')
    def effect(action, arguments):
        import time
        with store.transaction() as tx:
            tx.execute('INSERT INTO integration_case_effects VALUES (?)', (uuid4().hex,))
        time.sleep(.3)
        return {'success': True, 'refund_id': 'fixture-refund'}
    return guard.execute('refund', 'refund', {'order_id': 'ORD-CASE'}, token, effect).result


def lock_other_user(args):
    from app.memory.case_postgres import PostgresCaseManager
    dsn, user = args
    manager = PostgresCaseManager(user, SupportStore(dsn))
    with manager.transaction():
        manager.pending_order_selections['new'] = {'prompt': 'choose'}
        manager.save()
    return True


def consume_shared_budget(prefix):
    from app.runtime.budgets import Buckets, RateExceeded
    try:
        Buckets(REDIS, prefix).consume([('global', 1, 1), ('tenant', 1, 1)])
        return True
    except RateExceeded:
        return False


@pytest.fixture
def infra():
    import psycopg
    from psycopg import sql
    schema = 'itest_' + uuid4().hex
    with psycopg.connect(DSN) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    store = SupportStore(DSN + '?options=-csearch_path%3D' + schema)
    store.initialize()
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT current_database() AS name')['name'] == 'ecom_concurrency_test'
    prefix = 'itest:' + uuid4().hex
    queue = ChatQueue(REDIS, prefix=prefix)
    queue.ping()
    sid = str(uuid4())
    store.ensure(sid, 'integration-user')
    yield store, queue, sid
    keys = list(queue._redis.scan_iter(match=prefix + ':*'))
    if keys:
        queue._redis.delete(*keys)


def new_job(store, sid):
    jid = uuid4().hex
    state = store.receive(sid, 'integration-user', '查物流 ORD-A', jid, task_input={},
                          delivery=dict(max_pending=1000, queue_timeout=120, run_timeout=600))
    return ChatJob(jid, 'integration-user', sid, '查物流 ORD-A', control_version=state['control_version'])


def test_postgres_multi_process_dispatch_and_execution_reservation(infra):
    store, queue, sid = infra
    job = new_job(store, sid)
    with ProcessPoolExecutor(2, mp_context=get_context('spawn')) as pool:
        assert sum(pool.map(dispatch_process, [(store.dsn, queue.prefix)] * 2)) == 1
    assert queue.get(job.job_id)['status'] == 'queued'
    with ProcessPoolExecutor(3, mp_context=get_context('spawn')) as pool:
        assert sum(pool.map(reserve_process, [(store.dsn, job.to_dict())] * 3)) == 1
    store.publish_bot(sid, job.user_id, job.control_version, job.job_id, 'real DB result', {'reply': 'real DB result'})
    assert SupportStore(store.dsn).jobs.get(job.job_id, job.user_id)['result']['reply'] == 'real DB result'


def test_postgres_recovers_expired_delivery_owner_after_crash(infra):
    store, queue, sid = infra
    job = new_job(store, sid)
    claim = store.jobs.claim_delivery()
    assert claim['job_id'] == job.job_id
    queue.submit(job)  # Process disappears before marking delivery complete.
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET delivery_lease=0 WHERE job_id=?', (job.job_id,))
    assert OutboxDispatcher(SupportStore(store.dsn), queue).deliver_one()
    assert queue._redis.llen(queue.queue_key) == 1
    store.start_bot(sid, job.user_id, job.control_version, job.job_id)
    store.publish_bot(sid, job.user_id, job.control_version, job.job_id, 'done', {})


def test_redis_loss_redelivers_only_not_started_jobs(infra):
    store, queue, sid = infra
    job = new_job(store, sid)
    dispatcher = OutboxDispatcher(store, queue)
    assert dispatcher.deliver_one()
    # Simulate volatile broker loss only in this test's isolated namespace.
    keys = list(queue._redis.scan_iter(match=queue.prefix + ':*'))
    queue._redis.delete(*keys)
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET next_delivery=0 WHERE job_id=?', (job.job_id,))
    assert dispatcher.deliver_one()
    assert queue.claim('worker').job_id == job.job_id
    store.start_bot(sid, job.user_id, job.control_version, job.job_id)
    assert not dispatcher.deliver_one()
    store.publish_bot(sid, job.user_id, job.control_version, job.job_id, 'done', {})


def test_shared_confirmation_consumed_once_across_processes(infra):
    from app.memory.case_postgres import PostgresCaseManager
    from app.reliability.mutations import MutationGuard
    store, _, _ = infra
    with store.transaction() as tx:
        tx.execute('CREATE TABLE integration_case_effects (id TEXT PRIMARY KEY)')
    guard = MutationGuard(PostgresCaseManager('integration-user', store), 'case-session')
    token = guard.request_confirmation('refund', 'refund', {'order_id': 'ORD-CASE'})
    with ProcessPoolExecutor(3, mp_context=get_context('spawn')) as pool:
        results = list(pool.map(mutation_process, [(store.dsn, token)] * 3))
    assert any(result.get('success') for result in results)
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM integration_case_effects')['n'] == 1
    assert MutationGuard(PostgresCaseManager('integration-user', store), 'case-session').query_idempotency_status(
        'refund', 'refund', {'order_id': 'ORD-CASE'})['status'] == 'succeeded'


def test_shared_case_merge_archive_and_rollback(infra):
    from datetime import datetime, timedelta
    from app.memory.case_postgres import PostgresCaseManager, CaseStateConflict
    store, _, _ = infra
    first = PostgresCaseManager('integration-user', store)
    case = first.open_case_for_mutation('refund', 'ORD-MERGE')
    first.save()
    stale = PostgresCaseManager('integration-user', store)
    with first.transaction():
        first.cases[case.case_id].conversation_state['pending_mutation'] = {'used': True}
        first.save()
    stale.cases[case.case_id].business_state = {'source': 'order', 'value': 'paid'}
    stale.save()  # Disjoint edits preserve the consumed confirmation.
    assert stale.cases[case.case_id].conversation_state['pending_mutation']['used']
    old = PostgresCaseManager('integration-user', store)
    with stale.transaction():
        stale.cases[case.case_id].conversation_state['pending_mutation']['used'] = False
        stale.save()
    old.cases[case.case_id].conversation_state['pending_mutation']['used'] = 'stale edit'
    with pytest.raises(CaseStateConflict): old.save()
    with pytest.raises(RuntimeError):
        with first.transaction():
            first.cases[case.case_id].status = 'human_owned'
            first.save()
            raise RuntimeError('rollback')
    first.load()
    assert first.cases[case.case_id].status == 'open'
    with first.transaction():
        first.cases[case.case_id].status = 'closed'
        first.cases[case.case_id].last_active_at = (datetime.now() - timedelta(days=100)).isoformat()
        first.save()
    stale_before_archive = PostgresCaseManager('unrelated', store)
    # Load without constructor pruning to simulate an already-running Worker.
    stale_before_archive.user_id = 'integration-user'
    stale_before_archive.load()
    assert first.prune_cases() >= 1
    first.save()  # A second save must not resurrect removed snapshots/events.
    stale_before_archive.save()
    assert case.case_id not in PostgresCaseManager('integration-user', store).cases
    with store.transaction(read_only=True) as tx:
        archive = json.loads(tx.one('SELECT payload FROM case_archives WHERE case_id=?', (case.case_id,))['payload'])
    assert archive['case']['status'] == 'archived'
    assert any(event['event_type'] == 'case_archived' for event in archive['events'])


def test_user_row_lock_does_not_block_other_users_or_readers(infra):
    from app.memory.case_postgres import PostgresCaseManager
    store, _, sid = infra
    manager = PostgresCaseManager('locked-user', store)
    with ProcessPoolExecutor(1, mp_context=get_context('spawn')) as pool:
        with manager.transaction():
            with store.transaction() as tx:
                tx.one('SELECT * FROM support_conversations WHERE session_id=? FOR UPDATE', (sid,))
                assert store.state(sid, 'integration-user')['mode'] == 'bot'
                assert pool.submit(lock_other_user, (store.dsn, 'other-user')).result(timeout=20)


def test_case_migration_preserves_confirmation_and_refuses_overwrite(infra, tmp_path):
    from scripts.migrate_cases_to_postgres import read_snapshot, import_snapshot
    from app.memory.case_manager import CaseManager
    from app.memory.case_postgres import PostgresCaseManager
    from app.reliability.mutations import MutationGuard
    store, _, _ = infra
    local = CaseManager('migration-user', str(tmp_path))
    token = MutationGuard(local, 'migration-session').request_confirmation('refund', 'refund', {'order_id': 'ORD-A'})
    payload, archives = read_snapshot(local.path)
    import_snapshot(store, payload, archives)
    import_snapshot(store, payload, archives)  # Safe rerun after partial migration.
    shared = PostgresCaseManager('migration-user', store)
    assert MutationGuard(shared, 'migration-session').peek_confirmation(token)
    with shared.transaction():
        shared.pending_order_selections['new-session'] = {'prompt': 'new choice'}
        shared.save()
    with pytest.raises(ValueError, match='different Case state'): import_snapshot(store, payload, archives)
    assert 'new-session' in PostgresCaseManager('migration-user', store).pending_order_selections
    assert local.path.exists()


def test_distributed_rate_budget_is_atomic_across_processes(infra):
    _, queue, _ = infra
    with ProcessPoolExecutor(4, mp_context=get_context('spawn')) as pool:
        assert sum(pool.map(consume_shared_budget, [queue.prefix] * 8)) == 1


def test_terminal_database_job_retires_broker_state_without_replay(infra):
    store, queue, sid = infra
    job = new_job(store, sid)
    assert OutboxDispatcher(store, queue).deliver_one()
    assert queue.claim('crashed-worker').job_id == job.job_id
    store.start_bot(sid, job.user_id, job.control_version, job.job_id)
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?', (job.job_id,))
    assert store.jobs.recover() == 1
    assert store.jobs.retire_deliveries(queue) == 1
    assert store.jobs.retire_deliveries(queue) == 0
    assert queue.get(job.job_id)['status'] == 'retired'
    assert queue._redis.ttl(queue.status_key(job.job_id)) > 0
    assert queue._redis.get(queue.session_key(f'{sid}:v{job.control_version}')) is None
    assert store.jobs.get(job.job_id, job.user_id)['status'] == 'interrupted'
    with pytest.raises(SupportError): store.start_bot(sid, job.user_id, job.control_version, job.job_id)


def test_blocked_user_confirmation_does_not_serialize_other_users(infra):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    import threading
    from app.memory.case_postgres import PostgresCaseManager
    from app.reliability.mutations import MutationGuard
    store, _, _ = infra
    first = PostgresCaseManager('blocked-user', store)
    second = PostgresCaseManager('free-user', store)
    waiting = threading.Event()
    original = first.transaction
    @contextmanager
    def observed():
        waiting.set()
        with original(): yield
    first.transaction = observed
    with ThreadPoolExecutor(2) as pool:
        with store.transaction() as tx:
            tx.one("SELECT * FROM case_snapshots WHERE user_id='blocked-user' FOR UPDATE")
            blocked = pool.submit(MutationGuard(first, 's1').request_confirmation, 'refund', 'refund', {'order_id': 'ORD-A'})
            assert waiting.wait(2)
            free = pool.submit(MutationGuard(second, 's2').request_confirmation, 'refund', 'refund', {'order_id': 'ORD-B'})
            assert free.result(timeout=5)
        assert blocked.result(timeout=5)
