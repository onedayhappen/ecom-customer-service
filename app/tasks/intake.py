"""Continuous message intake; association workers consume the existing DB outbox.

No model/tool call runs inside these transactions. A lease claims an immutable
message range. Reading never acknowledges it; completing it records association
only, never execution or a user-visible reply.
"""
import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from app.runtime.outbox import TERMINAL, clock
from app.support.store import SupportError
from app.tasks.models import StrictModel
from app.tasks.repository import TaskRepository


class IncomingMessage(StrictModel):
    request_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    action: Literal["handoff", "cancel"] | None = None
    task_id: str | None = Field(default=None, min_length=1, max_length=200)
    task_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_control(self):
        if self.action == "cancel":
            if self.task_id is None or self.task_version is None:
                raise ValueError("取消需要明确的事项及版本")
        elif self.task_id is not None or self.task_version is not None:
            raise ValueError("事项参数只能用于明确取消")
        return self


class AssociationOutcome(StrictModel):
    sequence: int = Field(ge=1)
    status: Literal["associated", "clarification"]
    decision_id: str = Field(min_length=1, max_length=200)


@dataclass(frozen=True)
class MessageBatch:
    session_id: str
    user_id: str
    job_id: str
    owner: str
    messages: tuple[dict, ...]


class MessageIntake:
    def __init__(self, store, *, max_pending=1000, max_unassociated=1000, batch_size=50, lease_seconds=30):
        if not 1 <= batch_size <= 100 or not 1 <= lease_seconds <= 600 or max_pending < 1 or max_unassociated < 1:
            raise ValueError("Invalid intake capacity or lease")
        self.store = store
        self.max_pending, self.batch_size, self.lease_seconds = max_pending, batch_size, lease_seconds
        self.max_unassociated = max_unassociated

    def _state(self, tx, sid):
        row = tx.one("SELECT * FROM message_intake_sessions WHERE session_id=?", (sid,))
        if row is None:
            raise SupportError("持续消息会话不存在", 404)
        return row

    def _wake(self, tx, control):
        sid = control["session_id"]
        state = self._state(tx, sid)
        if control["mode"] != "bot":
            return
        active = tx.one("SELECT status FROM runtime_jobs WHERE job_id=?", (state["active_job_id"],))
        if active and active["status"] not in TERMINAL:
            return
        pending = tx.one("SELECT sequence FROM message_intake_items "
                         "WHERE session_id=? AND association_status='pending' LIMIT 1", (sid,))
        if pending is None:
            return
        job_id = str(uuid4())
        self.store.jobs.admit(tx, control, job_id, "", None, max_pending=self.max_pending,
                              queue_timeout=600, run_timeout=self.lease_seconds, kind="association")
        tx.execute("UPDATE message_intake_sessions SET active_job_id=?,lease_owner=NULL WHERE session_id=?", (job_id, sid))

    def _advance(self, tx, sid):
        state = self._state(tx, sid)
        # Cursor advances over actual intake messages, not arithmetic event IDs:
        # interspersed audit events and already handled controls are not missing messages.
        first_pending = tx.one("""SELECT MIN(sequence) AS seq FROM message_intake_items
            WHERE session_id=? AND association_status='pending'""", (sid,))["seq"]
        ceiling = first_pending if first_pending is not None else state["received_sequence"] + 1
        completed = tx.one("""SELECT MAX(sequence) AS seq FROM message_intake_items
            WHERE session_id=? AND sequence<? AND association_status<>'pending'""", (sid, ceiling))["seq"]
        position = max(state["associated_sequence"], completed or 0)
        tx.execute("UPDATE message_intake_sessions SET associated_sequence=? WHERE session_id=?", (position, sid))

    def receive(self, sid, user, **message):
        incoming = TaskRepository._validate(IncomingMessage, message)
        from app.support.runtime import explicit_handoff
        if incoming.action is None and explicit_handoff(incoming.content):
            incoming = incoming.model_copy(update={"action": "handoff"})
        fingerprint = TaskRepository._fingerprint(dict(session_id=sid, **incoming.model_dump()))
        with self.store.transaction() as tx:
            control = self.store._control(tx, sid, user)
            # Cross-session retries share the user's request namespace.
            old = tx.one("SELECT * FROM message_intake_items WHERE user_id=? AND request_id=?", (user, incoming.request_id))
            if old:
                if old["request_hash"] != fingerprint:
                    raise SupportError("请求编号已用于不同消息")
                return dict(session_id=sid, request_id=incoming.request_id, sequence=old["sequence"], status="accepted")
            state = tx.one("SELECT * FROM message_intake_sessions WHERE session_id=?", (sid,))
            if state is None:
                if control["bot_job_id"] or tx.one("SELECT job_id FROM conversation_task_turns WHERE session_id=? LIMIT 1", (sid,)):
                    raise SupportError("旧链路会话尚未迁移，请使用独立会话测试持续消息入口")
                tx.execute("INSERT INTO message_intake_sessions(session_id) VALUES (?)", (sid,))
            if incoming.action is None:
                count = tx.one("""SELECT COUNT(*) AS n FROM message_intake_items
                    WHERE session_id=? AND association_status='pending'""", (sid,))["n"]
                if count >= self.max_unassociated:
                    raise SupportError("待理解消息过多，请稍后继续或转人工", 429)
            sequence = self.store._event(tx, control, "message", user, {
                "role": "user", "content": incoming.content, "request_id": incoming.request_id,
                "intake_action": incoming.action,
                "control_task_id": incoming.task_id, "control_task_version": incoming.task_version,
            }, key="intake:" + incoming.request_id)
            inserted = tx.execute("""INSERT INTO message_intake_items
                (user_id,request_id,session_id,sequence,request_hash) VALUES (?,?,?,?,?)
                ON CONFLICT(user_id,request_id) DO NOTHING""",
                (user, incoming.request_id, sid, sequence, fingerprint)).rowcount
            if not inserted:
                raise SupportError("请求编号已用于其他会话")
            tx.execute("UPDATE message_intake_sessions SET received_sequence=? WHERE session_id=?", (sequence, sid))
            if incoming.action == "handoff":
                self.store._handoff(tx, control, "customer_requested", user)
                tx.execute("UPDATE message_intake_sessions SET lease_owner=NULL WHERE session_id=?", (sid,))
            elif incoming.action == "cancel":
                if control["mode"] != "bot":
                    raise SupportError("人工服务期间请由客服处理事项取消")
                TaskRepository(self.store, user).update(
                    incoming.task_id, sid, expected_version=incoming.task_version,
                    change_id="cancel:" + fingerprint, source=dict(kind="message", session_id=sid, sequence=sequence),
                    patch={"status": "cancelled", "waiting_for": None}, tx=tx)
            if incoming.action:
                tx.execute("""UPDATE message_intake_items SET association_status='control',decision_id=?
                    WHERE session_id=? AND sequence=?""", ("control:" + incoming.request_id, sid, sequence))
                self._advance(tx, sid)
            self._wake(tx, control)
            return dict(session_id=sid, request_id=incoming.request_id, sequence=sequence, status="accepted")

    def progress(self, sid, user, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise SupportError("消息分页参数不合法", 422)
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, user)
            state = self._state(tx, sid)
            items = tx.all("""SELECT sequence,request_id,association_status,decision_id FROM message_intake_items
                WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?""", (sid, after, limit))
            return {"received_sequence": state["received_sequence"], "associated_sequence": state["associated_sequence"],
                    "messages": items}

    def claim(self, sid, user):
        with self.store.transaction() as tx:
            control = self.store._control(tx, sid, user)
            if control["mode"] != "bot":
                return None
            self._wake(tx, control)
            state = self._state(tx, sid)
            job = tx.one("SELECT * FROM runtime_jobs WHERE job_id=?", (state["active_job_id"],))
            if job is None or job["status"] in TERMINAL:
                return None
            timestamp = clock(tx)
            if job["status"] == "running" and job["run_deadline"] > timestamp:
                return None
            messages = tx.all("""SELECT i.sequence,i.request_id,e.payload FROM message_intake_items i
                JOIN support_events e ON e.session_id=i.session_id AND e.sequence=i.sequence
                WHERE i.session_id=? AND i.association_status='pending' ORDER BY i.sequence LIMIT ?""", (sid, self.batch_size))
            if not messages:
                return None
            owner = str(uuid4())
            payload = dict(session_id=sid, user_id=user, sequences=[r["sequence"] for r in messages],
                           control_version=control["control_version"])
            tx.execute("""UPDATE runtime_jobs SET status='running',payload=?,started_at=?,run_deadline=?,
                attempts=attempts+1,error=NULL WHERE job_id=?""",
                (json.dumps(payload), timestamp, timestamp + self.lease_seconds, job["job_id"]))
            tx.execute("UPDATE message_intake_sessions SET lease_owner=? WHERE session_id=?", (owner, sid))
            return MessageBatch(sid, user, job["job_id"], owner, tuple(
                dict(sequence=r["sequence"], request_id=r["request_id"], content=json.loads(r["payload"])["content"])
                for r in messages))

    def renew(self, batch, seconds=60):
        """Bounded renewal before a model call; an expired owner cannot renew."""
        if not 1 <= seconds <= 600:
            raise ValueError("Invalid renewal")
        with self.store.transaction() as tx:
            c = self.store._control(tx, batch.session_id, batch.user_id)
            state = self._state(tx, batch.session_id)
            timestamp = clock(tx)
            if c["mode"] != "bot" or state["active_job_id"] != batch.job_id or state["lease_owner"] != batch.owner:
                raise SupportError("归属判断占用已失效")
            updated = tx.execute("""UPDATE runtime_jobs SET run_deadline=?
                WHERE job_id=? AND status='running' AND run_deadline>?""",
                (timestamp + seconds, batch.job_id, timestamp)).rowcount
            if updated != 1:
                raise SupportError("归属判断占用已过期")

    def claim_next(self):
        """Poll durable outbox, including an expired lease after worker death.

        Association has no external side effect; recovery can re-read pending
        input. The legacy worker must never consume this job kind.
        """
        with self.store.transaction(read_only=True) as tx:
            timestamp = clock(tx)
            candidates = tx.all("""SELECT j.session_id,j.user_id FROM runtime_jobs j
                JOIN support_conversations c ON c.session_id=j.session_id
                WHERE j.kind='association' AND c.mode='bot' AND
                (j.status IN ('pending','dispatched') OR (j.status='running' AND j.run_deadline<=?))
                ORDER BY j.created_at LIMIT 100""", (timestamp,))
        for candidate in candidates:
            batch = self.claim(candidate["session_id"], candidate["user_id"])
            if batch:
                return batch
        return None

    def complete(self, batch, outcomes, *, tx=None):
        """Use a shared transaction for later association decisions/task writes.

        If the lease or message range is stale, raise so the caller's entire
        transaction (including task changes) rolls back.
        """
        checked = [TaskRepository._validate(AssociationOutcome, item).model_dump() for item in outcomes]
        checked.sort(key=lambda item: item["sequence"])
        fingerprint = TaskRepository._fingerprint(checked)
        with (nullcontext(tx) if tx is not None else self.store.transaction()) as tx:
            control = self.store._control(tx, batch.session_id, batch.user_id)
            state = self._state(tx, batch.session_id)
            job = tx.one("SELECT * FROM runtime_jobs WHERE job_id=? AND session_id=? AND user_id=? AND kind='association'",
                         (batch.job_id, batch.session_id, batch.user_id))
            if not job:
                raise SupportError("归属判断任务不存在", 404)
            if job["status"] == "completed":
                receipt = job["response"]
                if not receipt:
                    archived = tx.one("SELECT response FROM runtime_job_archives WHERE job_id=?", (batch.job_id,))
                    receipt = archived["response"] if archived else None
                if not receipt:
                    raise SupportError("归属判断回执不可用，不能重新执行")
                response = json.loads(receipt)
                if response["owner"] == batch.owner and response["fingerprint"] == fingerprint:
                    return response["associated_sequence"]
                raise SupportError("已完成的判断不能改写")
            timestamp = clock(tx)
            payload = json.loads(job["payload"])
            if (control["mode"] != "bot" or payload.get("control_version") != control["control_version"]
                    or state["active_job_id"] != batch.job_id or state["lease_owner"] != batch.owner
                    or job["status"] != "running" or job["run_deadline"] <= timestamp):
                raise SupportError("归属判断已过期，请重新读取")
            if [item["sequence"] for item in checked] != payload["sequences"]:
                raise SupportError("只能完成本次领取的全部消息范围")
            for item in checked:
                updated = tx.execute("""UPDATE message_intake_items SET association_status=?,decision_id=?
                    WHERE session_id=? AND sequence=? AND association_status='pending'""",
                    (item["status"], item["decision_id"], batch.session_id, item["sequence"])).rowcount
                if updated != 1:
                    raise SupportError("消息处理状态已变化")
            self._advance(tx, batch.session_id)
            position = self._state(tx, batch.session_id)["associated_sequence"]
            self.store.jobs.completed(tx, batch.job_id, dict(owner=batch.owner, fingerprint=fingerprint, associated_sequence=position))
            tx.execute("UPDATE runtime_jobs SET next_delivery=0 WHERE job_id=?", (batch.job_id,))
            tx.execute("UPDATE message_intake_sessions SET lease_owner=NULL WHERE session_id=?", (batch.session_id,))
            self.store._event(tx, control, "association_complete", "system",
                              dict(job_id=batch.job_id, outcomes=checked), private=True)
            self._wake(tx, control)
            return position
