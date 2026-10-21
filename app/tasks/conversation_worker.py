"""Compose the existing association and execution harnesses; no extra Agent."""
from concurrent.futures import ThreadPoolExecutor
import logging
from types import SimpleNamespace
from app.config.settings import settings

from app.harness.bounded_call import bounded_calls
from app.runtime.outbox import clock
from app.support.store import SupportError
from app.tasks.association import AssociationService, _id
from app.tasks.intake import MessageIntake
from app.tasks.replies import ReplyCoordinator
from app.tasks.scheduling import TaskSchedule, TaskWorker

log = logging.getLogger(__name__)


class ConversationWorker:
    def __init__(self, store, engine, *, tools=None, concurrency=4, queue=None):
        self.store, self.engine = store, engine
        self.executions = TaskWorker(store, engine, tools=tools, concurrency=concurrency, queue=queue)
        self.intake = MessageIntake(store)
        self.pool = ThreadPoolExecutor(min(concurrency, 4), thread_name_prefix="message-association")
        self.capacity, self.associations = min(concurrency, 4), {}
        self.receipt_pool = ThreadPoolExecutor(1, thread_name_prefix="business-receipt")
        self.receipt_future = None

    def recover_operations(self):
        """One bounded receipt lookup at a time; business writes are never retried."""
        if self.receipt_future:
            if not self.receipt_future.done():
                return
            try:
                self.receipt_future.result()
            except Exception:
                log.exception("Business receipt recovery failed")
            self.receipt_future = None
        if not settings.task_operations_enabled:
            return
        with self.store.transaction(read_only=True) as tx:
            row = tx.one("""SELECT p.operation_id,p.user_id FROM task_operation_proposals p
                JOIN support_operations o ON o.operation_id=p.operation_id
                WHERE o.status='running' AND p.run_deadline<=? ORDER BY p.run_deadline LIMIT 1""", (clock(tx),))
        if row:
            from app.tasks.operations import TaskOperations
            self.receipt_future = self.receipt_pool.submit(
                TaskOperations(self.store, row["user_id"]).reconcile, row["operation_id"])

    def _associate(self, batch):
        engine = SimpleNamespace(associate=lambda context: bounded_calls.run(
            lambda: self.engine.associate(context), 15))
        return AssociationService(self.store, batch.user_id).process(batch, engine)

    def reconcile(self):
        """Recover the commit-to-enqueue gap by scanning durable task versions."""
        with self.store.transaction() as tx:
            rows = tx.all("""SELECT t.task_id,t.user_id,t.version,t.status FROM conversation_tasks t
                JOIN task_execution_wakes w ON t.task_id=w.task_id
                WHERE t.version<>w.version AND w.retry_at<=?
                ORDER BY w.retry_at,t.updated_at,t.task_id LIMIT 64""", (clock(tx),))
        for row in rows:
            try:
                with self.store.transaction() as tx:
                    if settings.task_external_progress_enabled and row['status'] in {'waiting_external', 'completed'}:
                        # External progress belongs to bounded maintenance; the
                        # original Agent/operation already owns its first reply.
                        tx.execute('''UPDATE task_execution_wakes SET version=?,job_id=NULL,retry_at=0
                            WHERE task_id=? AND EXISTS(SELECT 1 FROM conversation_tasks t
                                WHERE t.task_id=? AND t.version=? AND t.status=?)''',
                            (row['version'], row['task_id'], row['task_id'], row['version'], row['status']))
                        continue
                    source = tx.one("""SELECT e.session_id,e.sequence FROM support_task_messages m
                        JOIN support_events e ON e.session_id=m.session_id AND e.sequence=m.sequence
                        WHERE m.task_id=? AND e.kind='message' AND e.actor=?
                        ORDER BY e.created_at DESC,e.sequence DESC LIMIT 1""", (row["task_id"], row["user_id"]))
                    if not source:
                        raise SupportError("事项尚无可用于执行的用户消息来源")
                    schedule = TaskSchedule(self.store, row["user_id"])
                    request = dict(job_id=_id(row["task_id"], "execute:" + str(row["version"])),
                                   task_id=row["task_id"], expected_version=row["version"],
                                   session_id=source["session_id"], source=dict(kind="message", **source))
                    jid = schedule.enqueue(request, tx=tx)
                    tx.execute("""UPDATE task_execution_wakes SET version=?,job_id=?,retry_at=0
                        WHERE task_id=?""", (row["version"], jid, row["task_id"]))
            except SupportError:
                # Ownership, capacity, a concurrent update or human control:
                # retry discovery later, never bypass the scheduler's checks.
                with self.store.transaction() as tx:
                    tx.execute("UPDATE task_execution_wakes SET retry_at=? WHERE task_id=?",
                               (clock(tx) + 2, row["task_id"]))

    def tick(self):
        self.recover_operations()
        for jid, future in list(self.associations.items()):
            if future.done():
                del self.associations[jid]
                try:
                    future.result()
                except SupportError:
                    pass  # Lost lease/control is retried through durable intake.
                except Exception:
                    log.exception("Association worker failed")
        while len(self.associations) < self.capacity:
            batch = self.intake.claim_next()
            if not batch:
                break
            self.associations[batch.job_id] = self.pool.submit(self._associate, batch)
        self.reconcile()
        self.executions.tick()
        with self.store.transaction() as tx:
            sessions = tx.all("""SELECT c.session_id,c.user_id FROM task_reply_sessions r
                JOIN support_conversations c ON c.session_id=r.session_id
                ORDER BY r.checked_at,c.session_id LIMIT 64""")
        published = []
        for row in sessions:
            try:
                reply = ReplyCoordinator(self.store, row["user_id"]).publish(row["session_id"])
            except SupportError as exc:
                log.warning("Reply publication deferred (status %s)", exc.status)
                with self.store.transaction() as tx:
                    tx.execute("UPDATE task_reply_sessions SET checked_at=? WHERE session_id=?",
                               (clock(tx), row["session_id"]))
                continue
            if reply:
                published.append(reply)
        return published

    def close(self):
        self.receipt_pool.shutdown(wait=True)
        self.pool.shutdown(wait=True)
        self.executions.close()
