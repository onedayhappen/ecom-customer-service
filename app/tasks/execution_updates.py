"""Consume committed association updates at ReAct boundaries, never inside tools.

The existing job owns the execution and its budget. Association decisions are
the durable update queue; advancing a job's provenance is its consumption cursor.
"""
import json

from app.runtime.outbox import clock
from app.support.store import SupportError
from app.tasks.provenance import invalid, lock_input_user, revision
from app.tasks.repository import VersionConflict


class ReplanExecution(Exception):
    """Discard an unexecuted model plan and rebuild context for the same job."""


def pending_input(tx, user):
    # Match the existing write/publication fence, including other bot sessions.
    return bool(tx.one("""SELECT i.sequence FROM message_intake_items i
        JOIN support_conversations c ON c.session_id=i.session_id
        WHERE i.user_id=? AND i.association_status='pending' AND c.mode='bot' LIMIT 1""", (user,)))


def refresh(runs, request, payload):
    """Adopt only association-produced versions, under the original live lease."""
    with runs.store.transaction() as tx:
        lock_input_user(tx, runs.user)
        runs.store._control(tx, request.session_id, runs.user)
        revision(tx, runs.user, lock=True)
        row = runs._row(tx, request.job_id)
        stored = json.loads(row["payload"])
        if (row["status"] != "running" or stored.get("owner") != payload["owner"]
                or row["run_deadline"] <= clock(tx)):
            raise SupportError("执行租约已失效")
        control = runs._control(tx, request)
        runs.require_controls(tx, request, payload)
        if control["control_version"] != row["control_version"]:
            raise SupportError("会话控制已改变")
        if pending_input(tx, runs.user):
            return None
        runs._scope(tx, request.task_id)
        current = runs.repo._record(runs.repo._row(tx, request.task_id, lock=True))
        previous_version = payload["provenance"]["tasks"][request.task_id]
        if current.version == previous_version:
            runs.check(tx, request, payload, allow_wait=True)
            return current, payload, False
        # Manual corrections, invalidated sources and external changes must use
        # the existing interruption/recovery path, not be silently adopted.
        decisions = tx.all("""SELECT d.decision_id,e.payload FROM association_decisions d
            JOIN support_events e ON e.session_id=d.session_id AND e.sequence=d.event_sequence
            WHERE d.user_id=? AND d.task_id=? ORDER BY e.sequence""", (runs.user, request.task_id))
        versions = {json.loads(d["payload"]).get("task_version") for d in decisions
                    if not invalid(tx, runs.user, "decision", d["decision_id"])}
        if (current.version <= previous_version or current.version - previous_version > 100
                or not all(v in versions for v in range(previous_version + 1, current.version + 1))):
            raise VersionConflict()
        if current.status not in {"in_progress", "waiting_external"}:
            raise SupportError("事项已停止或等待用户")
        if payload.get("schedule"):
            from app.tasks.scheduling import dependencies_ready
            dependencies_ready(tx, runs, payload["schedule"])
        ids = [d["decision_id"] for d in decisions if not invalid(tx, runs.user, "decision", d["decision_id"])]
        source = tx.one("""SELECT e.session_id,e.sequence FROM support_task_messages m
            JOIN support_events e ON e.session_id=m.session_id AND e.sequence=m.sequence
            WHERE m.task_id=? AND e.kind='message' AND e.actor=?
            ORDER BY e.created_at DESC,e.sequence DESC LIMIT 1""", (current.task_id, runs.user))
        updated = {**stored, "provenance": runs.guard.capture([current.task_id], ids, tx=tx),
                   "controls": runs.controls(tx, request),
                   "execution_source": dict(kind="message", **source)}
        if updated.get("schedule"):
            from app.tasks.execution_store import object_key
            from app.tasks.scheduling import resources
            from app.runtime.budgets import tenant_for
            updated["schedule"] = {**updated["schedule"], "object": object_key(current),
                                   "resources": resources(current, tenant_for(runs.user))}
        tx.execute("UPDATE runtime_jobs SET payload=? WHERE job_id=?", (json.dumps(updated), request.job_id))
        # Reconcile may already have queued this version before the running job
        # reached its checkpoint. Retire dependency-free copies in the same scope.
        field = "payload::jsonb->>'task_id'" if tx.postgres else "json_extract(payload,'$.task_id')"
        queued = tx.all(f"""SELECT job_id,payload FROM runtime_jobs WHERE user_id=?
            AND kind='task_execution' AND status IN ('pending','dispatched') AND {field}=?""",
            (runs.user, current.task_id))
        for item in queued:
            candidate = json.loads(item["payload"])
            if (candidate.get("schedule") and not candidate["schedule"]["dependencies"]
                    and candidate.get("scope") == updated.get("scope")
                    and candidate["request"]["expected_version"] <= current.version):
                tx.execute("UPDATE runtime_jobs SET status='superseded',finished_at=? WHERE job_id=?",
                           (clock(tx), item["job_id"]))
        from app.observability.interactions import record
        record("execution.updated", tx=tx, session_id=request.session_id, job_id=request.job_id,
               task_id=current.task_id, input={"task_version": previous_version},
               output={"task_version": current.version, "decision_ids": ids})
        return current, updated, True


def save_usage(runs, request, payload, usage):
    """Persist budget consumption before dispatch; a restart cannot forget it."""
    with runs.store.transaction() as tx:
        runs.store._control(tx, request.session_id, runs.user)
        revision(tx, runs.user, lock=True)
        row = runs._row(tx, request.job_id)
        current = json.loads(row["payload"])
        if (row["status"] != "running" or current.get("owner") != payload["owner"]
                or row["run_deadline"] <= clock(tx)):
            raise SupportError("执行租约已失效")
        current["usage"] = usage
        tx.execute("UPDATE runtime_jobs SET payload=? WHERE job_id=?", (json.dumps(current), request.job_id))
        payload["usage"] = usage
