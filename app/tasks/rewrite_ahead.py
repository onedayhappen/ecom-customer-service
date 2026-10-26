"""Bounded, disposable look-ahead for raw-dialogue rewriting.

Only association owns/acknowledges messages. This cache cannot commit decisions;
another worker may safely ignore it and perform the normal durable path.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import json

from app.harness.bounded_call import bounded_calls
from app.observability.interactions import bind, span
from app.runtime.outbox import clock
from app.tasks.candidates import CandidateContext, CandidateService


@dataclass
class PreparedRewrite:
    dialogue: CandidateContext
    future: Future

    @property
    def sequences(self):
        return tuple(m["sequence"] for m in self.dialogue.current_messages)

    def matches(self, dialogue):
        return (self.dialogue.current_messages == dialogue.current_messages
                and self.dialogue.recent_messages == dialogue.recent_messages
                and self.dialogue.scope == dialogue.scope
                and self.dialogue.gaps == dialogue.gaps)


class RewriteAhead:
    def __init__(self, store, engine, *, capacity=2, batch_size=50):
        self.store, self.engine = store, engine
        self.capacity, self.batch_size = capacity, batch_size
        self.pool = ThreadPoolExecutor(capacity, thread_name_prefix="query-rewrite")
        self.pending = {}

    def _run(self, dialogue):
        with bind(self.store, dialogue.session_id):
            with span("query_rewrite.prefetch", input=dialogue.data()) as result:
                value = bounded_calls.run(lambda: self.engine.rewrite(dialogue), 15)
                result["output"] = value
                return value

    def tick(self, job_ids=None):
        # Retain in-flight work until it really completes, even if another worker
        # claimed the messages. This bounds speculative model calls per process.
        for sid, prepared in list(self.pending.items()):
            with self.store.transaction(read_only=True) as tx:
                waiting = {r["sequence"] for r in tx.all("""SELECT sequence FROM message_intake_items
                    WHERE session_id=? AND association_status='pending'""", (sid,))}
            if prepared.future.done() and not set(prepared.sequences) <= waiting:
                self.pending.pop(sid)
        if len(self.pending) >= self.capacity:
            return
        with self.store.transaction(read_only=True) as tx:
            active = tx.all("""SELECT j.job_id,j.session_id,j.user_id,j.payload FROM runtime_jobs j
                JOIN message_intake_sessions i ON i.active_job_id=j.job_id
                JOIN support_conversations c ON c.session_id=j.session_id
                WHERE j.kind='association' AND j.status='running' AND j.run_deadline>?
                AND c.mode='bot' ORDER BY j.created_at LIMIT 100""", (clock(tx),))
        for job in active:
            if job_ids is not None and job["job_id"] not in job_ids:
                continue
            sid, payload = job["session_id"], json.loads(job["payload"])
            if sid in self.pending or not payload.get("query_rewrite") or not payload.get("sequences"):
                continue
            with self.store.transaction(read_only=True) as tx:
                messages = tx.all("""SELECT sequence FROM message_intake_items
                    WHERE session_id=? AND sequence>? AND association_status='pending'
                    ORDER BY sequence LIMIT ?""", (sid, max(payload["sequences"]), self.batch_size))
            if messages:
                dialogue = CandidateService(self.store, job["user_id"]).dialogue(sid, [r["sequence"] for r in messages])
                self.pending[sid] = PreparedRewrite(dialogue, self.pool.submit(self._run, dialogue))
            if len(self.pending) >= self.capacity:
                break

    def limits(self):
        return {sid: p.sequences[-1] for sid, p in self.pending.items()}

    def take(self, batch):
        prepared = self.pending.get(batch.session_id)
        if prepared and prepared.sequences == tuple(m["sequence"] for m in batch.messages):
            return self.pending.pop(batch.session_id)
        return None

    def close(self):
        self.pool.shutdown(wait=True)
