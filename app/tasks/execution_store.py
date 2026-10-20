"""Execution receipts in the existing Outbox; Case retains tool evidence."""
import json
from uuid import uuid4

from app.runtime.outbox import clock
from app.support.store import SupportError, now
from app.tasks.association import _id
from app.tasks.candidates import CandidateService
from app.tasks.provenance import ProvenanceGuard, invalid, revision
from app.tasks.repository import TaskRepository, VersionConflict

OBJECT_FIELDS = ("order_id", "item_id", "sku_id", "product_id", "platform_code", "store_id")


def object_key(record):
    return {key: record.details[key] for key in OBJECT_FIELDS if key in record.details}


class TaskRuns:
    def __init__(self, store, user, *, scope=None):
        self.store, self.user = store, user
        self.repo, self.guard = TaskRepository(store, user), ProvenanceGuard(store, user)
        self.candidates = CandidateService(store, user, scope=scope)

    def _scope(self, tx, task_id):
        if not self.candidates._rows(tx, ["t.task_id=?"], [task_id]):
            raise SupportError("事项不在当前业务访问范围", 404)

    def _row(self, tx, job_id):
        row = tx.one("SELECT * FROM runtime_jobs WHERE job_id=? AND user_id=? AND kind='task_execution'",
                     (job_id, self.user))
        if not row:
            raise SupportError("事项执行不存在", 404)
        return row

    def _control(self, tx, request):
        control = self.store._control(tx, request.session_id, self.user)
        if control["mode"] != "bot" or tx.one("""SELECT c.session_id FROM task_sessions t
            JOIN support_conversations c ON c.session_id=t.session_id
            WHERE t.task_id=? AND c.mode<>'bot' LIMIT 1""", (request.task_id,)):
            raise SupportError("事项由人工处理，自动执行已停止")
        if tx.one("""SELECT o.operation_id FROM support_operations o
            JOIN task_sessions t ON t.session_id=o.session_id WHERE t.task_id=? AND o.status IN ('running','unknown')
            AND (EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id AND m.task_id=?)
                OR NOT EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id)) LIMIT 1""",
            (request.task_id, request.task_id)):
            raise SupportError("相关业务操作需先核实状态")
        return control

    def begin(self, request):
        fingerprint = self.repo._fingerprint(request.model_dump())
        with self.store.transaction() as tx:
            self.store._control(tx, request.session_id, self.user)
            revision(tx, self.user, lock=True)
            self._scope(tx, request.task_id)
            previous = tx.one("SELECT * FROM runtime_jobs WHERE job_id=?", (request.job_id,))
            if previous:
                payload = json.loads(previous["payload"])
                if (previous["user_id"] != self.user or previous["kind"] != "task_execution"
                        or payload.get("request_hash") != fingerprint):
                    raise SupportError("执行编号已用于不同请求")
                if any(value is not None and payload.get("scope", {}).get(key) != value
                       for key, value in vars(self.candidates.scope).items()):
                    raise SupportError("执行回执不在当前业务访问范围", 404)
                if previous["response"]:
                    return None, json.loads(previous["response"])
                if previous["status"] == "running":
                    if previous["run_deadline"] > clock(tx):
                        raise SupportError("该事项执行正在进行")
                    receipt = {"job_id": request.job_id, "task_id": request.task_id, "state": "interrupted",
                               "reply": "", "reason": "expired_execution", "needs_human": False}
                    tx.execute("UPDATE runtime_jobs SET status='interrupted',response=?,finished_at=? WHERE job_id=?",
                               (json.dumps(receipt), clock(tx), request.job_id))
                    return None, receipt
                if previous["status"] not in {"pending", "dispatched"}:
                    raise SupportError("该执行已停止")
            record = self.repo._record(self.repo._row(tx, request.task_id, lock=True))
            if record.storage_mode != "durable" or record.version != request.expected_version:
                raise VersionConflict()
            control = self._control(tx, request)
            self.repo._source(tx, request.source)
            if not tx.one("SELECT task_id FROM support_task_messages WHERE task_id=? AND session_id=? AND sequence=?",
                          (record.task_id, request.source.session_id, request.source.sequence)):
                raise SupportError("执行来源未关联到本事项", 422)
            if record.status in {"waiting_user", "paused", "completed", "cancelled"}:
                receipt = {"job_id": request.job_id, "task_id": record.task_id, "task_version": record.version,
                              "state": record.status, "reply": "", "waiting_for": record.waiting_for,
                              "provenance": self.guard.capture([record.task_id], tx=tx)}
                if previous:
                    tx.execute("UPDATE runtime_jobs SET status='completed',response=?,finished_at=? WHERE job_id=?",
                               (json.dumps(receipt), clock(tx), request.job_id))
                return None, receipt
            timestamp = clock(tx)
            task_field = "payload::jsonb->>'task_id'" if tx.postgres else "json_extract(payload,'$.task_id')"
            active = tx.one(f"""SELECT job_id,run_deadline FROM runtime_jobs WHERE kind='task_execution'
                AND status='running' AND {task_field}=?""", (request.task_id,))
            if active and active["run_deadline"] > timestamp:
                raise SupportError("该事项已有有效执行")
            if active:
                tx.execute("UPDATE runtime_jobs SET status='interrupted',finished_at=?,error='expired_execution' WHERE job_id=?",
                           (timestamp, active["job_id"]))
            decisions = [r["decision_id"] for r in tx.all("""SELECT decision_id FROM association_decisions
                WHERE user_id=? AND task_id=?""", (self.user, request.task_id))
                         if not invalid(tx, self.user, "decision", r["decision_id"])]
            stamp = self.guard.capture([record.task_id], decisions, tx=tx)
            payload = {"request": request.model_dump(), "request_hash": fingerprint, "task_id": record.task_id,
                       "provenance": stamp, "owner": str(uuid4()),
                       "scope": {key: record.details.get(key) for key in vars(self.candidates.scope)}}
            if previous:
                tx.execute("""UPDATE runtime_jobs SET payload=?,status='running',started_at=?,run_deadline=?
                    WHERE job_id=?""", (json.dumps(payload), timestamp, timestamp + request.budget.seconds, request.job_id))
            else:
                tx.execute("""INSERT INTO runtime_jobs
                    (job_id,session_id,user_id,control_version,payload,status,created_at,queue_deadline,
                     run_timeout,started_at,run_deadline,next_delivery,kind)
                    VALUES (?,?,?,?,?,'running',?,?,?,?,?,0,'task_execution')""",
                    (request.job_id, request.session_id, self.user, control["control_version"], json.dumps(payload),
                     timestamp, timestamp, request.budget.seconds, timestamp, timestamp + request.budget.seconds))
            return {"payload": payload, "record": record}, None

    def check(self, tx, request, payload, *, lock=False, allow_wait=False):
        if lock:
            revision(tx, self.user, lock=True)
        row = self._row(tx, request.job_id)
        current = json.loads(row["payload"])
        if (row["status"] != "running" or current.get("owner") != payload["owner"]
                or row["run_deadline"] <= clock(tx)):
            raise SupportError("执行租约已失效")
        self.guard.require_current(payload["provenance"], tx=tx, lock=lock)
        self._scope(tx, request.task_id)
        self._control(tx, request)
        record = self.repo._record(self.repo._row(tx, request.task_id))
        if not allow_wait and record.status not in {"in_progress", "waiting_external"}:
            raise SupportError("事项已停止或等待用户")
        return record

    def save_tool(self, request, payload, call_index, name, arguments, result):
        with self.store.transaction() as tx:
            control = self.store._control(tx, request.session_id, self.user)
            record = self.check(tx, request, payload, lock=True)
            eid = _id(request.job_id, "tool:" + str(call_index))
            evidence = {"event_id": eid, "tool": name, "arguments": arguments, "result": result,
                        "task_id": record.task_id, "task_version": record.version, "object": object_key(record),
                        "provenance": payload["provenance"], "observed_at": now(), "observed_epoch": clock(tx)}
            self.store._event(tx, control, "task_tool_result", "system", evidence, private=True, key="task-tool:" + eid)
            # Same transaction and same Case evidence format; do not write a
            # parallel conversation draft or reinterpret a query as an action.
            tx.execute("INSERT INTO case_snapshots VALUES (?,?) ON CONFLICT DO NOTHING",
                       (self.user, json.dumps({"user_id": self.user, "cases": [], "events": []})))
            snapshot = tx.one("SELECT payload FROM case_snapshots WHERE user_id=?" +
                              (" FOR UPDATE" if tx.postgres else ""), (self.user,))
            case = json.loads(snapshot["payload"])
            case.setdefault("events", []).append({
                "event_id": eid, "event_type": "tool_result", "user_id": self.user,
                "session_id": request.session_id, "turn_id": request.job_id, "case_id": None,
                "payload": {"tool": name, "evidence": result, "success": bool(result.get("success")),
                            "task_id": record.task_id, "task_version": record.version, "object": object_key(record)},
                "created_at": evidence["observed_at"],
            })
            tx.execute("UPDATE case_snapshots SET payload=? WHERE user_id=?", (json.dumps(case, ensure_ascii=False), self.user))
            return evidence

    def evidence(self, record, *, seconds=60):
        with self.store.transaction(read_only=True) as tx:
            task_field = "e.payload::jsonb->>'task_id'" if tx.postgres else "json_extract(e.payload,'$.task_id')"
            rows = tx.all(f"""SELECT e.payload FROM support_events e JOIN support_conversations c ON c.session_id=e.session_id
                WHERE c.user_id=? AND e.kind='task_tool_result' AND {task_field}=? ORDER BY e.created_at DESC LIMIT 30""",
                (self.user, record.task_id))
            cutoff = clock(tx) - seconds
            return [e for r in reversed(rows) if (e := json.loads(r["payload"]))["object"] == object_key(record)
                    and e["observed_epoch"] >= cutoff and e["result"].get("success")
                    and not any(invalid(tx, self.user, "decision", did) for did in e["provenance"]["decisions"])]

    def save_progress(self, request, payload, progress, index):
        with self.store.transaction() as tx:
            self.store._control(tx, request.session_id, self.user)
            record = self.check(tx, request, payload, lock=True)
            if record.version != progress.expected_version:
                raise VersionConflict()
            change = dict(expected_version=record.version, change_id=_id(request.job_id, "progress:" + str(index)),
                          source=request.source.model_dump(), tx=tx)
            if progress.state == "waiting_user":
                record = self.repo.ask(record.task_id, request.session_id, question=progress.question.model_dump(), **change)
            else:
                waiting = {"kind": "external", "reason": progress.wait_reason, "evidence_id": progress.evidence_id} if progress.state == "waiting_external" else None
                record = self.repo.update(record.task_id, request.session_id, patch={"status": progress.state, "waiting_for": waiting}, **change)
            payload = {**payload, "provenance": self.guard.capture([record.task_id], payload["provenance"]["decisions"], tx=tx)}
            tx.execute("UPDATE runtime_jobs SET payload=? WHERE job_id=?", (json.dumps(payload), request.job_id))
            return record, payload

    def finish(self, request, payload, result, *, interrupted=False):
        with self.store.transaction() as tx:
            self.store._control(tx, request.session_id, self.user)
            row = self._row(tx, request.job_id)
            if row["response"]:
                return json.loads(row["response"])
            if json.loads(row["payload"]).get("owner") != payload["owner"]:
                raise SupportError("执行租约已失效")
            if not interrupted:
                self.check(tx, request, payload, lock=True, allow_wait=True)
            result = {**result, "job_id": request.job_id, "task_id": request.task_id,
                      "provenance": payload["provenance"]}
            tx.execute("""UPDATE runtime_jobs SET status=?,response=?,finished_at=? WHERE job_id=?""",
                       ("interrupted" if interrupted else "completed", json.dumps(result, ensure_ascii=False), clock(tx), request.job_id))
            return result
