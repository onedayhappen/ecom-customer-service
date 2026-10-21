"""Durable admission, delivery and execution state in the conversation database.

Worker reservations are database-owned. The retired chat broker is not used.
"""
from __future__ import annotations

import json

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

    def admit(self, tx, c, job_id, *, max_pending, queue_timeout, run_timeout, kind):
        from app.support.store import SupportError
        if kind != "association":
            raise SupportError("Only message association jobs use this admission path", 422)
        # Bound the durable backlog even when Redis is unavailable. Serialize
        # only this short count+insert section, never network/LLM work.
        if tx.postgres:
            tx.execute('SELECT pg_advisory_xact_lock(82344711)')
        count = tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status IN ('pending','dispatched','running')")['n']
        if count >= max_pending:
            raise SupportError('客服任务容量已满，请稍后重试', 429)
        timestamp = clock(tx)
        payload = dict(job_id=job_id, session_id=c['session_id'], user_id=c['user_id'],
                       control_version=c['control_version'])
        tx.execute('''INSERT INTO runtime_jobs
            (job_id,session_id,user_id,control_version,payload,status,created_at,queue_deadline,run_timeout,next_delivery,kind)
            VALUES (?,?,?,?,?,'pending',?,?,?,?,?)''',
                   (job_id, c['session_id'], c['user_id'], c['control_version'], json.dumps(payload, ensure_ascii=False),
                    timestamp, timestamp + queue_timeout, run_timeout, timestamp, kind))


    def completed(self, tx, job_id, response):
        tx.execute("""UPDATE runtime_jobs SET status='completed',response=?,finished_at=?,error=NULL
                    WHERE job_id=? AND status='running'""",
                   (json.dumps(response, ensure_ascii=False), clock(tx), job_id))


    def supersede(self, tx, c):
        tx.execute("""UPDATE runtime_jobs SET status='superseded',error='任务已取消、切换或转人工',finished_at=?
                      WHERE session_id=? AND control_version<>? AND status IN ('pending','dispatched','running')""",
                   (clock(tx), c['session_id'], c['control_version']))
