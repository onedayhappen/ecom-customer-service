# 文件说明：消费来源更正与删除产生的记忆失效任务，并清理旧派生记录。
"""Consume correction/deletion work without blocking message acceptance."""
from dataclasses import replace
import json
from uuid import uuid4

from app.runtime.outbox import clock
from app.support.store import SupportError
from app.tasks.provenance import event_ref, lock_input_user, revision


def forget_source(store, user, sid, sequence):
    """Authenticated server entry point: exclude a source from model memory.

    This is a model-access deletion, not a promise to erase transaction/audit
    records. The tombstone is authoritative even if cache cleanup fails.
    """
    if type(sequence) is not int or sequence < 1:
        raise SupportError('原话序号必须是正整数', 422)
    key = 'forget:' + event_ref(sid, sequence)
    with store.transaction() as tx:
        lock_input_user(tx, user)
        control = store._control(tx, sid, user)
        revision(tx, user, lock=True)
        row = tx.one("""SELECT sequence,payload FROM support_events WHERE session_id=? AND sequence=?
            AND kind='message' AND visibility='public' AND actor=?""", (sid, sequence, user))
        if not row or json.loads(row['payload']).get('role') != 'user':
            raise SupportError('原话不存在', 404)
        saved = tx.execute("""INSERT INTO task_invalid_sources VALUES (?,'event',?,?)
            ON CONFLICT DO NOTHING""", (user, event_ref(sid, sequence), key)).rowcount
        if not saved:
            return
        tx.execute('UPDATE task_provenance_users SET revision=revision+1 WHERE user_id=?', (user,))
        tx.execute('DELETE FROM support_context_snapshots WHERE session_id=?', (sid,))
        from app.session.short_term import enqueue_context_job
        enqueue_context_job(tx, control)
        store._event(tx, control, 'memory_source_deleted', 'system',
            dict(source_session=sid, source_sequence=sequence), private=True, key=key)
        timestamp = clock(tx)
        tx.execute("""INSERT INTO runtime_jobs
            (job_id,session_id,user_id,control_version,payload,status,created_at,queue_deadline,run_timeout,next_delivery,kind)
            VALUES (?,?,?,?,?,'pending',?,?,?,?, 'task_invalidation')""",
            (str(uuid4()), sid, user, control['control_version'], '{}',
             timestamp, timestamp + 86400, 60, timestamp))


def process_memory_invalidations(store, *, limit=20, manager_factory=None):
    """Idempotent cross-store reconciliation; expired leases can be retried."""
    from app.memory.runtime import build_memory_manager
    from app.memory.source_policy import SourcePolicy
    manager_factory = manager_factory or build_memory_manager
    counts = dict(processed=0, failed=0, invalidated=0)
    with store.transaction(read_only=True) as tx:
        jobs = tx.all("""SELECT job_id,user_id FROM runtime_jobs WHERE kind='task_invalidation'
            AND status='pending' AND next_delivery<=? AND delivery_lease<=?
            ORDER BY created_at LIMIT ?""", (clock(tx), clock(tx), min(100, max(0, limit))))
    for job in jobs:
        owner = str(uuid4())
        with store.transaction() as tx:
            claimed = tx.execute("""UPDATE runtime_jobs SET delivery_owner=?,delivery_lease=?
                WHERE job_id=? AND status='pending' AND delivery_lease<=?""",
                (owner, clock(tx) + 120, job['job_id'], clock(tx))).rowcount
        if not claimed:
            continue
        try:
            manager = manager_factory(job['user_id'], store=store)
            policy = SourcePolicy(store, job['user_id'])
            # Work on the authoritative ledger under its own user lock; model
            # and embedding generation are unnecessary for invalidation.
            if hasattr(manager, 'invalidate_sources'):
                invalidated = manager.invalidate_sources()
            else:
                invalidated = _invalidate_legacy(manager, policy, job['user_id'])
            with store.transaction() as tx:
                saved = tx.execute("""UPDATE runtime_jobs SET status='completed',finished_at=?,
                    delivery_lease=0,response=? WHERE job_id=? AND status='pending' AND delivery_owner=?""",
                    (clock(tx), json.dumps(dict(invalidated=invalidated)), job['job_id'], owner)).rowcount
            counts['processed'] += saved
            counts['invalidated'] += invalidated if saved else 0
        except Exception as error:
            with store.transaction() as tx:
                tx.execute("""UPDATE runtime_jobs SET delivery_lease=0,next_delivery=?,attempts=attempts+1,error=?
                    WHERE job_id=? AND status='pending' AND delivery_owner=?""",
                    (clock(tx) + 30, type(error).__name__, job['job_id'], owner))
            counts['failed'] += 1
    return counts


def _invalidate_legacy(manager, policy, user):
    with manager.ltm.store.transaction(user):
        ledger = manager.ltm.history_facts()
        obsolete = [f for f in ledger if not f.metadata.get('reset') and not policy.allows(f)]
        manager.ltm.store.commit_versions(user, [
            manager.ltm._fact_to_item(replace(f, status='retracted',
                metadata={**f.metadata, 'reset': True, 'source_invalidated': True})) for f in obsolete])
    return len(obsolete)
