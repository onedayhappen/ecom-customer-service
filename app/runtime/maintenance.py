# 文件说明：归档终态任务并补齐工单快照，保留幂等和执行停止依据。
"""Bounded background compaction; execution and idempotency tombstones survive."""
import json

from app.runtime.outbox import clock

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS runtime_job_archives (
       job_id TEXT PRIMARY KEY, archived_at DOUBLE PRECISION NOT NULL,
       payload TEXT NOT NULL, response TEXT)''',
    '''CREATE INDEX IF NOT EXISTS runtime_jobs_archive_candidates ON runtime_jobs(finished_at)
       WHERE payload<>'{}' AND status IN ('completed','failed','expired','interrupted','superseded')''',
)


def archive_jobs(store, retention_days=30, limit=200):
    if retention_days < 1 or not 1 <= limit <= 1000:
        raise ValueError('Retention must be >=1 day; batch size must be 1..1000')
    with store.transaction() as tx:
        timestamp = clock(tx)
        rows = tx.all("""SELECT job_id,payload,response FROM runtime_jobs WHERE
            status IN ('completed','failed','expired','interrupted','superseded')
            AND finished_at<? AND payload<>'{}' ORDER BY finished_at LIMIT ?""" +
            (' FOR UPDATE SKIP LOCKED' if tx.postgres else ''), (timestamp - retention_days * 86400, limit))
        for row in rows:
            payload = json.loads(row['payload'])
            payload.pop('confirmation_token', None)
            tx.execute('INSERT INTO runtime_job_archives VALUES (?,?,?,?) ON CONFLICT(job_id) DO NOTHING',
                       (row['job_id'], timestamp, json.dumps(payload, ensure_ascii=False), row['response']))
            tx.execute("UPDATE runtime_jobs SET payload='{}',response=NULL WHERE job_id=?", (row['job_id'],))
    return len(rows)


def backfill_ticket_snapshots(store, limit=25):
    """Move legacy event replay out of repeat ticket reads, in bounded batches."""
    if not 1 <= limit <= 100:
        raise ValueError('Snapshot batch size must be 1..100')
    with store.transaction(read_only=True) as tx:
        rows = tx.all("""SELECT t.ticket_id,t.session_id,e.sequence FROM support_tickets t
            JOIN support_events e ON e.session_id=t.session_id AND e.event_key='ticket-closed:' || t.ticket_id
            LEFT JOIN support_ticket_task_snapshots s ON s.ticket_id=t.ticket_id
            WHERE s.ticket_id IS NULL ORDER BY t.created_at LIMIT ?""", (limit,))
    for row in rows:
        with store.transaction() as tx:
            # Events below the closed ticket's ceiling are immutable. No
            # conversation row lock is needed while reading legacy history.
            payload = store.tasks.snapshot(tx, {'session_id': row['session_id']}, row['sequence'])
            tx.execute('INSERT INTO support_ticket_task_snapshots VALUES (?,?,?) ON CONFLICT(ticket_id) DO NOTHING',
                       (row['ticket_id'], row['sequence'], json.dumps(payload, ensure_ascii=False)))
    return len(rows)
