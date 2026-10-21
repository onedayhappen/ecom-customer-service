"""Durable read-only scheduling. Redis carries hints; the database grants work."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from typing import Literal

from pydantic import Field, model_validator

from app.runtime.budgets import tenant_for
from app.runtime.outbox import clock, TERMINAL
from app.support.store import SupportError
from app.tasks.association import _id
from app.tasks.candidates import BusinessScope
from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns, object_key
from app.tasks.models import StrictModel
from app.tasks.provenance import revision
from app.tasks.repository import VersionConflict


class Dependency(StrictModel):
    """A trusted server contract, never executable expressions from the model."""
    job_id: str = Field(min_length=1, max_length=200)
    kind: Literal["query", "operation"]
    evidence_id: str | None = None
    tool: Literal["query_order", "query_logistics", "query_product", "search_knowledge"] | None = None
    field: str | None = Field(default=None, pattern=r"^[a-zA-Z_][a-zA-Z_0-9]*(\.[a-zA-Z_][a-zA-Z_0-9]*){0,4}$")
    compare: Literal["eq", "gt"] = "eq"
    value: bool | str | int | float | None = None
    operation_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def complete(self):
        if self.kind == "query":
            if not self.tool or not self.field or self.value is None or self.operation_id:
                raise ValueError("Query dependency requires a tool, field and value")
        elif not self.operation_id or self.tool or self.field or self.evidence_id or self.value is not None:
            raise ValueError("Operation dependency requires only an operation receipt")
        return self


class ScheduleBlocked(SupportError):
    pass


def resources(record, tenant):
    """Shared objects are recorded now; mutation conflict locking belongs to 09."""
    scope = [tenant, record.details.get("platform_code"), record.details.get("store_id")]
    keys = ("order_id", "item_id", "sku_id", "product_id", "entitlement_id")
    return [json.dumps([*scope, key, record.details[key]], ensure_ascii=False)
            for key in keys if record.details.get(key)]


def _task_field(tx):
    return "payload::jsonb->>'task_id'" if tx.postgres else "json_extract(payload,'$.task_id')"


def dependencies_ready(tx, runs, schedule):
    """Recheck durable receipts, ownership and provenance immediately before start."""
    for raw in schedule["dependencies"]:
        dep = Dependency.model_validate(raw)
        parent = runs._row(tx, dep.job_id)
        result = json.loads(parent["response"] or "{}")
        payload = json.loads(parent["payload"])
        runs._scope(tx, payload["task_id"])
        if not runs.control_versions_valid(tx, payload.get("controls")):
            raise ScheduleBlocked("dependency_control_changed")
        try:
            runs.require_controls(tx, TaskExecutionInput.model_validate(payload["request"]), payload)
        except SupportError:
            raise ScheduleBlocked("dependency_control_changed") from None
        if tx.one("""SELECT c.session_id FROM task_sessions t JOIN support_conversations c
            ON t.session_id=c.session_id WHERE t.task_id=? AND c.mode<>'bot' LIMIT 1""", (payload["task_id"],)):
            raise ScheduleBlocked("dependency_under_human_control")
        if parent["status"] != "completed" or result.get("state") not in {"ready", "waiting_external"}:
            raise ScheduleBlocked("dependency_pending")
        if not runs.guard.valid(result.get("provenance"), tx=tx):
            raise ScheduleBlocked("dependency_changed")
        runs.guard.require_current(result["provenance"], tx=tx, lock=not tx.read_only)
        if dep.kind == "operation":
            # A claim in dialogue or a successful query is never a write receipt.
            operation = tx.one("""SELECT o.status,o.result FROM support_operations o
                JOIN support_operation_tasks m ON m.operation_id=o.operation_id
                JOIN support_conversations c ON c.session_id=o.session_id
                WHERE o.operation_id=? AND m.task_id=? AND c.user_id=?""",
                (dep.operation_id, payload["task_id"], runs.user))
            if (not operation or operation["status"] != "succeeded"
                    or not json.loads(operation["result"] or "{}").get("success")):
                raise ScheduleBlocked("operation_unverified")
            continue
        found = False
        for eid in result.get("evidence_ids", []):
            if dep.evidence_id and dep.evidence_id != eid:
                continue
            event = tx.one("""SELECT e.payload FROM support_events e
                JOIN support_conversations c ON c.session_id=e.session_id
                WHERE c.user_id=? AND e.kind='task_tool_result' AND e.event_key=?""",
                (runs.user, "task-tool:" + eid))
            evidence = json.loads(event["payload"]) if event else {}
            if (evidence.get("task_id") != payload["task_id"] or evidence.get("tool") != dep.tool
                    or not evidence.get("result", {}).get("success")
                    or evidence.get("observed_epoch", 0) < clock(tx) - 60
                    or not runs.control_versions_valid(tx, evidence.get("controls"))
                    or not runs.guard.valid(evidence.get("provenance"), tx=tx)):
                continue
            value = evidence["result"]
            for part in dep.field.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            numeric = type(value) in (int, float) and type(dep.value) in (int, float)
            found = ((numeric and value > dep.value) if dep.compare == "gt"
                     else (type(value) is type(dep.value) and value == dep.value))
            if found:
                break
        if not found:
            raise ScheduleBlocked("condition_not_verified")


def admit_execution(tx, runs, previous):
    """Caller holds its conversation/user/task locks; serialize only quota checks."""
    from app.config.settings import settings
    schedule = json.loads(previous["payload"]).get("schedule") if previous else None
    if schedule:
        if previous["queue_deadline"] <= clock(tx):
            raise ScheduleBlocked("queue_expired")
        dependencies_ready(tx, runs, schedule)
    if tx.postgres:
        tx.execute("SELECT pg_advisory_xact_lock(82344712)")
    active = tx.all("""SELECT user_id,payload FROM runtime_jobs
        WHERE kind='task_execution' AND status='running' AND run_deadline>?""", (clock(tx),))
    tenant = tenant_for(runs.user)
    if (len(active) >= settings.task_global_concurrency
            or sum(r["user_id"] == runs.user for r in active) >= settings.task_user_concurrency
            or sum(tenant_for(r["user_id"]) == tenant for r in active) >= settings.api_tenant_agent_concurrency):
        raise ScheduleBlocked("execution_capacity")


class TaskSchedule:
    def __init__(self, store, user, *, scope=None, queue=None):
        self.store, self.user, self.queue = store, user, queue
        self.runs = TaskRuns(store, user, scope=scope)

    def enqueue(self, request, *, dependencies=(), queue_seconds=300, tx=None, recovery=0):
        own_transaction = tx is None
        request = TaskExecutionInput.model_validate(request)
        deps = [Dependency.model_validate(d).model_dump() for d in dependencies]
        if len(deps) > 8 or not 1 <= queue_seconds <= 3600:
            raise SupportError("调度参数超出范围", 422)
        signature = self.runs.repo._fingerprint({"request": request.model_dump(), "dependencies": deps,
                                                "scope": vars(self.runs.candidates.scope)})
        with (nullcontext(tx) if tx is not None else self.store.transaction()) as tx:
            control = self.store._control(tx, request.session_id, self.user)
            revision(tx, self.user, lock=True)
            self.runs._scope(tx, request.task_id)
            existing = tx.one("SELECT * FROM runtime_jobs WHERE job_id=?", (request.job_id,))
            if existing:
                payload = json.loads(existing["payload"])
                if existing["user_id"] != self.user or payload.get("schedule", {}).get("signature") != signature:
                    raise SupportError("执行编号已用于不同请求")
                return existing["job_id"]
            record = self.runs.repo._record(self.runs.repo._row(tx, request.task_id, lock=True))
            if record.storage_mode != "durable" or record.version != request.expected_version:
                raise VersionConflict()
            self.runs._control(tx, request)
            self.runs.repo._source(tx, request.source)
            if not tx.one("SELECT task_id FROM support_task_messages WHERE task_id=? AND session_id=? AND sequence=?",
                          (record.task_id, request.source.session_id, request.source.sequence)):
                raise SupportError("执行来源未关联到本事项", 422)
            # Every predecessor already exists and is immutable: no forward edge,
            # self-reference or cycle can be introduced through this interface.
            for dep in deps:
                parent = self.runs._row(tx, dep["job_id"])
                self.runs._scope(tx, json.loads(parent["payload"])["task_id"])
            active = tx.all(f"""SELECT * FROM runtime_jobs WHERE kind='task_execution'
                AND {_task_field(tx)}=? AND status IN ('pending','dispatched','running','completed')""", (record.task_id,))
            for row in active:
                old = json.loads(row["payload"])
                effective_version = old.get("provenance", {}).get("tasks", {}).get(record.task_id)
                if (record.version in {old["request"]["expected_version"], effective_version} and old.get("schedule")
                        and self.runs.control_versions_valid(tx, old.get('controls'))):
                    if old["schedule"]["dependencies"] != deps or old["scope"] != vars(self.runs.candidates.scope):
                        raise SupportError("同一事项版本已有不同的执行条件")
                    return row["job_id"]
                if row["status"] in {"pending", "dispatched"}:
                    tx.execute("UPDATE runtime_jobs SET status='superseded',finished_at=? WHERE job_id=?",
                               (clock(tx), row["job_id"]))
            from app.config.settings import settings
            if tx.postgres:
                tx.execute("SELECT pg_advisory_xact_lock(82344712)")
            count = tx.one("""SELECT COUNT(*) AS n FROM runtime_jobs
                WHERE kind='task_execution' AND status IN ('pending','dispatched','running')""")["n"]
            if count >= settings.api_chat_queue_max_depth:
                raise SupportError("事项队列容量已满", 429)
            timestamp = clock(tx)
            schedule = dict(signature=signature, dependencies=deps, recovery=recovery,
                            resources=resources(record, tenant_for(self.user)), object=object_key(record))
            payload = dict(request=request.model_dump(), request_hash=self.runs.repo._fingerprint(request.model_dump()),
                           task_id=record.task_id, scope=vars(self.runs.candidates.scope), schedule=schedule,
                           controls=self.runs.controls(tx, request))
            waiting = record.status != "in_progress"
            receipt = dict(job_id=request.job_id, task_id=record.task_id, task_version=record.version,
                           state=record.status, reply="", waiting_for=record.waiting_for,
                           provenance=self.runs.guard.capture([record.task_id], tx=tx)) if waiting else None
            tx.execute("""INSERT INTO runtime_jobs
                (job_id,session_id,user_id,control_version,payload,status,created_at,queue_deadline,
                 run_timeout,next_delivery,kind,response,finished_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'task_execution',?,?)""",
                (request.job_id, request.session_id, self.user, control["control_version"],
                 json.dumps(payload), "completed" if waiting else "pending", timestamp,
                 timestamp + queue_seconds, request.budget.seconds, timestamp,
                 json.dumps(receipt) if receipt else None, timestamp if waiting else None))
        # A hint is optional. Never send before the caller's outer transaction commits.
        if self.queue and not waiting and own_transaction:
            self.queue.notify(request.job_id)
        return request.job_id

    def get(self, job_id):
        with self.store.transaction(read_only=True) as tx:
            row = self.runs._row(tx, job_id)
            payload = json.loads(row["payload"])
            self.runs._scope(tx, payload["task_id"])
            for key, value in vars(self.runs.candidates.scope).items():
                if value is not None and payload.get("schedule", {}).get("object", payload.get("scope", {})).get(key) != value:
                    raise SupportError("执行回执不在当前业务访问范围", 404)
            return dict(job_id=job_id, status=row["status"], reason=row["error"],
                        result=json.loads(row["response"]) if row["response"] else None)


class TaskWorker:
    """One bounded pool per process. No session lock spans model/tool execution."""
    def __init__(self, store, engine, *, tools=None, concurrency=4, queue=None):
        if not 1 <= concurrency <= 64:
            raise ValueError("concurrency must be between 1 and 64")
        self.store, self.engine, self.tools, self.queue = store, engine, tools, queue
        self.pool = ThreadPoolExecutor(concurrency, thread_name_prefix="task-worker")
        self.concurrency, self.futures = concurrency, {}

    def tick(self):
        """Recover first, then reserve work without waiting on occupied slots."""
        self.recover()
        completed = {}
        for jid, future in list(self.futures.items()):
            if future.done():
                # Exceptions are observed, never mistaken for successful completion.
                try:
                    completed[jid] = future.result()
                except Exception:
                    completed[jid] = {"state": "interrupted", "reason": "worker_failure"}
                del self.futures[jid]
        if self.queue:
            self.queue.take()  # Wake-up hint only; DB polling also finds lost hints.
        with self.store.transaction(read_only=True) as tx:
            rows = tx.all("""SELECT * FROM runtime_jobs WHERE kind='task_execution'
                AND status IN ('pending','dispatched') AND next_delivery<=?
                ORDER BY next_delivery,created_at LIMIT 128""", (clock(tx),))
        for row in rows:
            if len(self.futures) >= self.concurrency:
                break
            payload = json.loads(row["payload"])
            if not payload.get("schedule"):
                continue
            request = TaskExecutionInput.model_validate(payload["request"])
            from app.tasks.execution import TaskExecutor
            executor = TaskExecutor(self.store, row["user_id"], tools=self.tools,
                                    scope=BusinessScope(**payload["scope"]))
            try:
                reservation, replay = executor.runs.begin(request)
            except ScheduleBlocked as exc:
                self._defer(row, str(exc))
                continue
            except SupportError:
                # A concurrent worker may already own this row. Only pending
                # stale/invalid requests can be retired here.
                with self.store.transaction() as tx:
                    self.store._control(tx, row["session_id"], row["user_id"])
                    revision(tx, row["user_id"], lock=True)
                    current = tx.one("SELECT status FROM runtime_jobs WHERE job_id=?", (row["job_id"],))
                    if current["status"] in {"pending", "dispatched"}:
                        record = executor.runs.repo._record(executor.runs.repo._row(tx, request.task_id))
                        if record.version != request.expected_version:
                            tx.execute("UPDATE runtime_jobs SET status='superseded',finished_at=? WHERE job_id=?",
                                       (clock(tx), row["job_id"]))
                        else:
                            self._defer(row, "task_busy_or_controlled", tx=tx)
                continue
            if replay is None:
                self.futures[row["job_id"]] = self.pool.submit(executor.run_reserved, request, self.engine, reservation)
            else:
                completed[row["job_id"]] = replay
        return completed

    def _defer(self, row, reason, *, tx=None):
        with (nullcontext(tx) if tx is not None else self.store.transaction()) as tx:
            tx.execute("""UPDATE runtime_jobs SET error=?,next_delivery=? WHERE job_id=?
                AND status IN ('pending','dispatched')""", (reason, clock(tx) + .2, row["job_id"]))

    def recover(self):
        with self.store.transaction(read_only=True) as tx:
            timestamp = clock(tx)
            rows = tx.all("""SELECT * FROM runtime_jobs WHERE kind='task_execution' AND (
                (status IN ('pending','dispatched') AND queue_deadline<=?) OR
                (status='running' AND run_deadline<=?)) ORDER BY created_at LIMIT 128""", (timestamp, timestamp))
        recovered = 0
        for stale in rows:
            with self.store.transaction() as tx:
                control = self.store._control(tx, stale["session_id"], stale["user_id"])
                revision(tx, stale["user_id"], lock=True)
                row = tx.one("SELECT * FROM runtime_jobs WHERE job_id=?", (stale["job_id"],))
                timestamp = clock(tx)
                if row["status"] in TERMINAL:
                    continue
                if (row["run_deadline"] if row["status"] == "running" else row["queue_deadline"]) > timestamp:
                    continue
                payload = json.loads(row["payload"])
                if not payload.get("schedule"):
                    continue
                running = row["status"] == "running"
                receipt = dict(job_id=row["job_id"], task_id=payload["task_id"], state="interrupted" if running else "expired",
                               reply="", reason="worker_expired" if running else "queue_expired")
                tx.execute("UPDATE runtime_jobs SET status=?,response=?,finished_at=? WHERE job_id=?",
                           (receipt["state"], json.dumps(receipt), timestamp, row["job_id"]))
                recovered += 1
                service = TaskSchedule(self.store, row["user_id"], scope=BusinessScope(**payload["scope"]))
                request = TaskExecutionInput.model_validate(payload["request"])
                if row["control_version"] != control["control_version"]:
                    continue
                try:
                    service.runs.require_controls(tx, request, payload)
                except SupportError:
                    continue
                # No write is replayed. Unknown operations stop the whole affected
                # conversation for verification, even if the worker died mid-call.
                try:
                    service.runs._control(tx, request)
                except SupportError:
                    if control["mode"] == "bot":
                        self.store._handoff(tx, control, "task_recovery_requires_verification", "system")
                    continue
                record = service.runs.repo._record(service.runs.repo._row(tx, request.task_id))
                attempts = payload["schedule"]["recovery"]
                if running and attempts < 2 and record.status == "in_progress" and record.version == request.expected_version:
                    request = request.model_copy(update={"job_id": _id(row["job_id"], "readonly-recovery")})
                    service.enqueue(request, dependencies=payload["schedule"]["dependencies"], tx=tx, recovery=attempts + 1)
                    # Executor loads only fresh, object-matching successful task
                    # evidence. Missing/failed reads are safe to repeat; no model
                    # continuation, question or business action is blindly replayed.
        return recovered

    def close(self):
        self.pool.shutdown(wait=True)
