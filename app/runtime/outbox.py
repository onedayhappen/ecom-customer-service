"""Durable admission, delivery and execution state in the conversation database.

Redis delivery is at-least-once. Only the database execution reservation permits
business execution. A started job is never replayed by recovery.
"""
from __future__ import annotations

import json
from uuid import uuid4

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runtime_jobs (
      job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
      control_version INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
      created_at DOUBLE PRECISION NOT NULL, queue_deadline DOUBLE PRECISION NOT NULL,
      run_timeout DOUBLE PRECISION NOT NULL, started_at DOUBLE PRECISION,
      finished_at DOUBLE PRECISION, run_deadline DOUBLE PRECISION,
      next_delivery DOUBLE PRECISION NOT NULL, delivery_owner TEXT,
      delivery_lease DOUBLE PRECISION NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
      error TEXT, response TEXT)""",
    """CREATE INDEX IF NOT EXISTS runtime_jobs_delivery ON runtime_jobs(next_delivery)
       WHERE status IN ('pending','dispatched')""",
    """CREATE INDEX IF NOT EXISTS runtime_jobs_running ON runtime_jobs(run_deadline)
       WHERE status='running'""",
    """CREATE INDEX IF NOT EXISTS runtime_jobs_status ON runtime_jobs(status,created_at)""",
    """CREATE INDEX IF NOT EXISTS runtime_jobs_session ON runtime_jobs(session_id,status)""",
    """CREATE INDEX IF NOT EXISTS runtime_jobs_retire ON runtime_jobs(finished_at)
       WHERE status IN ('completed','failed','expired','interrupted','superseded') AND next_delivery>0""",
)
TERMINAL = {'completed', 'failed', 'expired', 'interrupted', 'superseded'}


def clock(tx):
    sql = "SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS t" if tx.postgres else "SELECT (julianday('now') - 2440587.5)*86400 AS t"
    return float(tx.one(sql)['t'])


class DurableJobs:
    def __init__(self, store):
        self.store = store

    def admit(self, tx, c, job_id, message, confirmation_token, *, max_pending, queue_timeout, run_timeout, kind="conversation"):
        from app.support.store import SupportError
        # Bound the durable backlog even when Redis is unavailable. Serialize
        # only this short count+insert section, never network/LLM work.
        if tx.postgres:
            tx.execute('SELECT pg_advisory_xact_lock(82344711)')
        count = tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status IN ('pending','dispatched','running')")['n']
        if count >= max_pending:
            raise SupportError('客服任务容量已满，请稍后重试', 429)
        timestamp = clock(tx)
        payload = dict(job_id=job_id, session_id=c['session_id'], user_id=c['user_id'],
                       control_version=c['control_version'], message=message, confirmation_token=confirmation_token)
        tx.execute('''INSERT INTO runtime_jobs
            (job_id,session_id,user_id,control_version,payload,status,created_at,queue_deadline,run_timeout,next_delivery,kind)
            VALUES (?,?,?,?,?,'pending',?,?,?,?,?)''',
                   (job_id, c['session_id'], c['user_id'], c['control_version'], json.dumps(payload, ensure_ascii=False),
                    timestamp, timestamp + queue_timeout, run_timeout, timestamp, kind))

    def get(self, job_id, user):
        from app.support.store import SupportError
        with self.store.transaction(read_only=True) as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE job_id=? AND user_id=? AND kind='conversation'", (job_id, user))
            if not row:
                return None
            response = row['response']
            if row['payload'] == '{}' and not response:
                archived = tx.one('SELECT response FROM runtime_job_archives WHERE job_id=?', (job_id,))
                response = archived['response'] if archived else None
            result = json.loads(response) if response else None
            return {k: row[k] for k in ('job_id', 'session_id', 'user_id', 'status', 'error')} | {'result': result}

    def claim_delivery(self, lease_seconds=15):
        with self.store.transaction() as tx:
            timestamp = clock(tx)
            row = tx.one("""SELECT * FROM runtime_jobs WHERE status IN ('pending','dispatched')
                AND kind='conversation' AND next_delivery<=? AND delivery_lease<=? ORDER BY next_delivery,created_at LIMIT 1""" +
                (' FOR UPDATE SKIP LOCKED' if tx.postgres else ''), (timestamp, timestamp))
            if not row:
                return None
            owner = str(uuid4())
            tx.execute('UPDATE runtime_jobs SET delivery_owner=?,delivery_lease=?,attempts=attempts+1 WHERE job_id=?',
                       (owner, timestamp + lease_seconds, row['job_id']))
            return {**row, 'delivery_owner': owner}

    def delivered(self, job_id, owner, error=None):
        from app.runtime.metrics import DELIVERIES
        DELIVERIES.labels(result='retry' if error else 'delivered').inc()
        with self.store.transaction() as tx:
            timestamp = clock(tx)
            row = tx.one('SELECT attempts FROM runtime_jobs WHERE job_id=? AND delivery_owner=?', (job_id, owner))
            if not row:
                return
            delay = min(30, 2 ** min(row['attempts'], 5)) if error else 5
            # A Worker can start before this ACK: never overwrite running/completed.
            tx.execute("""UPDATE runtime_jobs SET status=?,error=?,next_delivery=?,delivery_lease=0,
                delivery_owner=NULL WHERE job_id=? AND delivery_owner=? AND status IN ('pending','dispatched')""",
                       ('pending' if error else 'dispatched', error, timestamp + delay, job_id, owner))

    def started(self, tx, c, job_id):
        from app.support.store import BotInterrupted
        row = tx.one('SELECT * FROM runtime_jobs WHERE job_id=?', (job_id,))
        if row is None:  # Synchronous compatibility path.
            return
        timestamp = clock(tx)
        if row['kind'] != 'conversation' or row['status'] not in {'pending', 'dispatched'} or row['queue_deadline'] <= timestamp:
            raise BotInterrupted()
        tx.execute("UPDATE runtime_jobs SET status='running',started_at=?,run_deadline=?,error=NULL WHERE job_id=?",
                   (timestamp, timestamp + row['run_timeout'], job_id))
        from app.runtime.metrics import QUEUE_WAIT
        QUEUE_WAIT.observe(max(0, timestamp - row['created_at']))
        self.store._event(tx, c, 'job_state', 'system', {'job_id': job_id, 'status': 'running'})

    def completed(self, tx, job_id, response):
        tx.execute("""UPDATE runtime_jobs SET status='completed',response=?,finished_at=?,error=NULL
                    WHERE job_id=? AND status='running'""",
                   (json.dumps(response, ensure_ascii=False), clock(tx), job_id))

    def finish(self, job, response=None, error=None):
        """Persist non-message outcomes (handoff/suppression/errors), too."""
        with self.store.transaction() as tx:
            c = self.store._control(tx, job.session_id, job.user_id)
            row = tx.one("SELECT status FROM runtime_jobs WHERE job_id=? AND kind='conversation'", (job.job_id,))
            if not row or row['status'] in TERMINAL:
                return
            if response is not None:
                self.completed(tx, job.job_id, response)
            else:
                inflight = tx.one("SELECT operation_id FROM support_operations WHERE session_id=? AND status IN ('running','unknown') LIMIT 1", (job.session_id,))
                if c['control_version'] == job.control_version and inflight:
                    self.store._handoff(tx, c, 'business_result_unknown', 'system')
                elif c['bot_job_id'] == job.job_id:
                    self.store.tasks.failed(tx, c, job.job_id)
                    tx.execute('UPDATE support_conversations SET bot_job_id=NULL WHERE session_id=?', (job.session_id,))
                tx.execute("UPDATE runtime_jobs SET status=?,error=?,finished_at=? WHERE job_id=? AND status NOT IN ('completed','interrupted','superseded')",
                           ('interrupted' if inflight else 'failed', error or '任务执行失败，请查看会话或转人工核实', clock(tx), job.job_id))
            final = tx.one('SELECT status FROM runtime_jobs WHERE job_id=?', (job.job_id,))
            self.store._event(tx, c, 'job_state', 'system', {'job_id': job.job_id, 'status': final['status']})

    def supersede(self, tx, c):
        tx.execute("""UPDATE runtime_jobs SET status='superseded',error='任务已取消、切换或转人工',finished_at=?
                      WHERE session_id=? AND control_version<>? AND status IN ('pending','dispatched','running')""",
                   (clock(tx), c['session_id'], c['control_version']))

    def retire_deliveries(self, queue, limit=100):
        # Terminal DB state is immutable for execution. Cleaning its broker
        # record cannot authorize a replay; execution tombstones remain in DB.
        with self.store.transaction(read_only=True) as tx:
            rows = tx.all("""SELECT job_id FROM runtime_jobs WHERE
                kind='conversation' AND status IN ('completed','failed','expired','interrupted','superseded') AND next_delivery>0
                ORDER BY finished_at LIMIT ?""", (limit,))
        for row in rows:
            queue.retire(row['job_id'])
            with self.store.transaction() as tx:
                tx.execute('UPDATE runtime_jobs SET next_delivery=0 WHERE job_id=?', (row['job_id'],))
        return len(rows)

    def recover(self, limit=100):
        """Deadline enforcement independent of any client polling.

        Lock order is always conversation -> job. A recovered running job fences
        its original Worker before releasing the conversation to human review.
        """
        with self.store.transaction(read_only=True) as tx:
            timestamp = clock(tx)
            rows = tx.all("""SELECT job_id,session_id,user_id FROM runtime_jobs WHERE kind='conversation' AND (
                (status IN ('pending','dispatched') AND queue_deadline<=?) OR
                (status='running' AND run_deadline<=?)) ORDER BY created_at LIMIT ?""", (timestamp, timestamp, limit))
        recovered = 0
        for row in rows:
            with self.store.transaction() as tx:
                c = self.store._control(tx, row['session_id'], row['user_id'])
                job = tx.one('SELECT * FROM runtime_jobs WHERE job_id=?', (row['job_id'],))
                timestamp = clock(tx)
                if job['status'] in TERMINAL:
                    continue
                if job['status'] == 'running':
                    if job['run_deadline'] > timestamp:
                        continue
                    if c['control_version'] == job['control_version']:
                        self.store._handoff(tx, c, 'worker_execution_unknown', 'system')
                    state, error = 'interrupted', '执行超时或进程退出，已停止旧任务，请人工核实业务结果'
                else:
                    if job['queue_deadline'] > timestamp:
                        continue
                    if c['bot_job_id'] == job['job_id']:
                        self.store.tasks.failed(tx, c, job['job_id'])
                        tx.execute('UPDATE support_conversations SET bot_job_id=NULL WHERE session_id=?', (row['session_id'],))
                    state, error = 'expired', '排队超时，任务未执行'
                tx.execute('UPDATE runtime_jobs SET status=?,error=?,finished_at=? WHERE job_id=?', (state, error, timestamp, job['job_id']))
                self.store._event(tx, c, 'job_state', 'system', {'job_id': job['job_id'], 'status': state})
                recovered += 1
                from app.runtime.metrics import RECOVERIES
                RECOVERIES.labels(status=state).inc()
        return recovered


class OutboxDispatcher:
    def __init__(self, store, queue):
        self.store, self.queue = store, queue

    def deliver_one(self):
        from app.runtime.chat_queue import ChatJob
        from app.runtime.concurrency import CoordinationUnavailable
        from app.runtime.chat_queue import SessionBusy, RequestConflict
        record = self.store.jobs.claim_delivery()
        if not record:
            return False
        try:
            self.queue.submit(ChatJob(**json.loads(record['payload'])))
        except (CoordinationUnavailable, OverflowError, SessionBusy, RequestConflict) as exc:
            # Do not persist provider exception strings (may contain endpoints).
            self.store.jobs.delivered(record['job_id'], record['delivery_owner'], type(exc).__name__)
        else:
            self.store.jobs.delivered(record['job_id'], record['delivery_owner'])
        return True
