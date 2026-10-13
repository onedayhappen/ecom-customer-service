"""Persistent delivery guarantees, independent of API response delivery."""
import json
from uuid import uuid4

import pytest

from app.runtime.chat_queue import ChatJob
from app.runtime.outbox import OutboxDispatcher
from app.runtime.concurrency import CoordinationUnavailable
from app.support.store import SupportStore, SupportError, BotInterrupted


@pytest.fixture
def store(tmp_path):
    store = SupportStore(f'sqlite:///{tmp_path / "outbox.db"}')
    store.ensure('session', 'user')
    return store


def admit(store, message='查物流 ORD-A', sid='session', job_id=None, capacity=100):
    job_id = job_id or str(uuid4())
    state = store.receive(sid, 'user', message, job_id, task_input={},
                          delivery=dict(max_pending=capacity, queue_timeout=120, run_timeout=600))
    return ChatJob(job_id, 'user', sid, message, control_version=state['control_version'])


def test_background_archive_preserves_result_ownership_and_execution_tombstone(store):
    from app.runtime.maintenance import archive_jobs
    job = admit(store)
    store.start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
    store.publish_bot(job.session_id, job.user_id, job.control_version, job.job_id, 'done', {'reply': 'done'})
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET finished_at=0 WHERE job_id=?', (job.job_id,))
    assert archive_jobs(store, retention_days=1, limit=1) == 1
    assert archive_jobs(store, retention_days=1) == 0
    assert store.jobs.get(job.job_id, 'user')['result'] == {'reply': 'done'}
    assert store.jobs.get(job.job_id, 'intruder') is None
    with pytest.raises(SupportError): store.start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT payload FROM runtime_jobs WHERE job_id=?', (job.job_id,))['payload'] == '{}'
        archived = json.loads(tx.one('SELECT payload FROM runtime_job_archives WHERE job_id=?', (job.job_id,))['payload'])
        assert 'confirmation_token' not in archived
    store.ensure('pending', 'user')
    pending = admit(store, sid='pending')
    assert archive_jobs(store, retention_days=1) == 0
    assert store.jobs.get(pending.job_id, 'user')['status'] == 'pending'


def test_admission_and_outbox_are_atomic(store):
    first = admit(store, capacity=1)
    store.ensure('second', 'user')
    with pytest.raises(SupportError, match='容量'):
        admit(store, sid='second', capacity=1)
    assert store.public_history('second', 'user') == []
    assert store.tasks.list('second', 'user')['tasks'] == []
    assert store.jobs.get(first.job_id, 'user')['status'] == 'pending'
    assert store.jobs.get(first.job_id, 'intruder') is None


def test_api_restart_and_idempotent_receive_retain_outbox(store):
    job = admit(store)
    restored = SupportStore(store.dsn)
    replay = admit(restored, job_id=job.job_id)
    assert replay == job
    with restored.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM runtime_jobs')['n'] == 1


def test_delivery_survives_redis_failure(store):
    job = admit(store)
    class Offline:
        def submit(self, job):
            raise CoordinationUnavailable('offline')
    assert OutboxDispatcher(store, Offline()).deliver_one()
    assert store.jobs.get(job.job_id, 'user')['status'] == 'pending'
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET next_delivery=0 WHERE job_id=?', (job.job_id,))
    delivered = []
    class Online:
        def submit(self, job): delivered.append(job)
    assert OutboxDispatcher(SupportStore(store.dsn), Online()).deliver_one()
    assert delivered == [job]
    assert store.jobs.get(job.job_id, 'user')['status'] == 'dispatched'


def test_dispatch_crash_after_send_is_replayed_but_execution_once(store):
    job = admit(store)
    claim = store.jobs.claim_delivery()
    assert claim
    assert store.jobs.claim_delivery() is None
    # Simulate dispatcher disappearing after Redis accepts, before DB ACK.
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET delivery_lease=0 WHERE job_id=?', (job.job_id,))
    replacement = store.jobs.claim_delivery()
    assert replacement['delivery_owner'] != claim['delivery_owner']
    store.start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
    with pytest.raises(SupportError):
        store.start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
    store.jobs.delivered(job.job_id, replacement['delivery_owner'])
    assert store.jobs.get(job.job_id, 'user')['status'] == 'running'
    store.publish_bot(job.session_id, job.user_id, job.control_version, job.job_id, 'done', {'reply': 'done'})
    assert SupportStore(store.dsn).jobs.get(job.job_id, 'user')['result'] == {'reply': 'done'}


def test_recovery_expires_unstarted_job_without_client_polling(store):
    job = admit(store)
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET queue_deadline=0 WHERE job_id=?', (job.job_id,))
    assert store.jobs.recover() == 1
    assert store.jobs.get(job.job_id, 'user')['status'] == 'expired'
    with pytest.raises(BotInterrupted):
        store.start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
    assert store.tasks.list('session', 'user')['tasks'][0]['status'] == 'paused'


def test_recovery_fences_dead_worker_and_never_replays_mutation(store):
    job = admit(store, '退款 ORD-A')
    store.start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
    store.begin_operation(job.session_id, job.user_id, job.control_version, 'apply_refund')
    with store.transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?', (job.job_id,))
    assert store.jobs.recover() == 1
    assert store.jobs.get(job.job_id, 'user')['status'] == 'interrupted'
    assert store.state('session', 'user')['mode'] == 'handoff_pending'
    assert store.jobs.claim_delivery() is None
    with pytest.raises(BotInterrupted): store.check_bot('session', 'user', job.control_version)
    with pytest.raises(BotInterrupted): store.publish_bot('session', 'user', job.control_version, job.job_id, 'late')


def test_cancel_before_dispatch_is_terminal(store):
    job = admit(store)
    store.receive('session', 'user', '取消', str(uuid4()), task_input={})
    assert store.jobs.get(job.job_id, 'user')['status'] == 'superseded'
    assert store.jobs.claim_delivery() is None


def test_readonly_snapshot_does_not_write(store):
    store.events('session', 'user')
    with store.transaction(read_only=True) as tx:
        assert store._control(tx, 'session', 'user')['user_id'] == 'user'
