"""Execution receipts in the existing Outbox; Tool evidence lives in the task event log."""
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
        from app.tasks.rollout import require_running
        require_running(tx, self.user)
        control = self.store._control(tx, request.session_id, self.user)
        if tx.one('SELECT ticket_id FROM task_handoffs WHERE user_id=? AND task_id=? AND released_at IS NULL LIMIT 1',
                  (self.user, request.task_id)):
            raise SupportError('事项仍由人工处理，自动执行已停止')
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

    def controls(self, tx, request):
        return {r["session_id"]: r["control_version"] for r in tx.all("""SELECT c.session_id,c.control_version
            FROM support_conversations c WHERE c.user_id=? AND (c.session_id=? OR EXISTS (
                SELECT 1 FROM task_sessions t WHERE t.session_id=c.session_id AND t.task_id=?))""",
            (self.user, request.session_id, request.task_id))}

    def require_controls(self, tx, request, payload):
        current = self.controls(tx, request)
        if any(current.get(sid) != version for sid, version in payload.get("controls", {}).items()):
            raise SupportError("相关会话控制已改变")

    def control_versions_valid(self, tx, stamp):
        if not isinstance(stamp, dict) or not stamp:
            return False
        return all(tx.one("""SELECT session_id FROM support_conversations
            WHERE user_id=? AND session_id=? AND control_version=?""", (self.user, sid, version))
            for sid, version in stamp.items())

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
            if previous and previous["control_version"] != control["control_version"]:
                raise SupportError("会话控制已改变")
            if previous:
                self.require_controls(tx, request, json.loads(previous["payload"]))
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
            from app.tasks.scheduling import admit_execution
            admit_execution(tx, self, previous)
            decisions = [r["decision_id"] for r in tx.all("""SELECT decision_id FROM association_decisions
                WHERE user_id=? AND task_id=?""", (self.user, request.task_id))
                         if not invalid(tx, self.user, "decision", r["decision_id"])]
            stamp = self.guard.capture([record.task_id], decisions, tx=tx)
            payload = {"request": request.model_dump(), "request_hash": fingerprint, "task_id": record.task_id,
                       "provenance": stamp, "owner": str(uuid4()),
                       "controls": self.controls(tx, request),
                       "scope": {key: record.details.get(key) for key in vars(self.candidates.scope)}}
            if previous:
                queued = json.loads(previous["payload"])
                if queued.get("usage"):
                    payload["usage"] = queued["usage"]
                if queued.get("schedule"):
                    payload["schedule"] = queued["schedule"]
                    payload["scope"] = queued["scope"]
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
        control = self._control(tx, request)
        if row["control_version"] != control["control_version"]:
            raise SupportError("会话控制已改变")
        self.require_controls(tx, request, current)
        if current.get("schedule"):
            from app.tasks.scheduling import dependencies_ready
            dependencies_ready(tx, self, current["schedule"])
        record = self.repo._record(self.repo._row(tx, request.task_id))
        if not allow_wait and record.status not in {"in_progress", "waiting_external"}:
            raise SupportError("事项已停止或等待用户")
        return record

    def save_tool(self, request, payload, call_index, name, arguments, result, *, original=None):
        with self.store.transaction() as tx:
            control = self.store._control(tx, request.session_id, self.user)
            if original is None:
                record = self.check(tx, request, payload, lock=True)
            else:
                # A query already in flight may still be useful after an ordinary
                # detail edit. Keep its OLD provenance; never finish the old model
                # run or stamp that answer as having seen the new message.
                revision(tx, self.user, lock=True)
                row = self._row(tx, request.job_id)
                self._control(tx, request)
                self.require_controls(tx, request, payload)
                self._scope(tx, request.task_id)
                record = self.repo._record(self.repo._row(tx, request.task_id, lock=True))
                if (name not in {"query_order", "query_logistics"} or not result.get("success")
                        or record.version <= original.version
                        or row["status"] != "running" or row["run_deadline"] <= clock(tx)
                        or json.loads(row["payload"]).get("owner") != payload["owner"]
                        or row["control_version"] != control["control_version"]
                        or record.status != "in_progress" or record.goal != original.goal
                        or object_key(record) != object_key(original)
                        or record.details.get("tracking_number") != original.details.get("tracking_number")
                        or any(invalid(tx, self.user, "decision", did) for did in payload["provenance"]["decisions"])):
                    raise VersionConflict()
                if payload.get("schedule"):
                    from app.tasks.scheduling import dependencies_ready
                    dependencies_ready(tx, self, payload["schedule"])
                record = original
            eid = _id(request.job_id, "tool:" + str(call_index))
            evidence = {"event_id": eid, "tool": name, "arguments": arguments, "result": result,
                        "task_id": record.task_id, "task_version": record.version, "object": object_key(record),
                        "provenance": payload["provenance"], "controls": payload.get("controls"),
                        "observed_at": now(), "observed_epoch": clock(tx)}
            self.store._event(tx, control, "task_tool_result", "system", evidence, private=True, key="task-tool:" + eid)
            return evidence

    def save_late_tool(self, request, payload, original, index, name, arguments, result):
        """Audit an admitted call after its version/lease changed; never authorize reuse."""
        with self.store.transaction() as tx:
            control = self.store._control(tx, request.session_id, self.user)
            row = self._row(tx, request.job_id)
            if json.loads(row["payload"]).get("owner") != payload["owner"]:
                raise SupportError("工具结果不属于原执行")
            self._scope(tx, request.task_id)
            eid = _id(request.job_id, "tool:" + str(index))
            evidence = dict(event_id=eid, tool=name, arguments=arguments, result=result,
                            task_id=request.task_id, task_version=original.version, object=object_key(original),
                            provenance=payload["provenance"], controls=payload.get("controls"), late=True,
                            observed_at=now(), observed_epoch=clock(tx))
            self.store._event(tx, control, "task_tool_result", "system", evidence,
                              private=True, key="task-tool:" + eid)
            return evidence

    def evidence(self, record, *, seconds=60):
        with self.store.transaction(read_only=True) as tx:
            from app.media.context import dependency_stamp
            media = dependency_stamp(tx, self.user, [record.task_id])
            task_field = "e.payload::jsonb->>'task_id'" if tx.postgres else "json_extract(e.payload,'$.task_id')"
            rows = tx.all(f"""SELECT e.payload FROM support_events e JOIN support_conversations c ON c.session_id=e.session_id
                WHERE c.user_id=? AND e.kind='task_tool_result' AND {task_field}=? ORDER BY e.created_at DESC LIMIT 30""",
                (self.user, record.task_id))
            cutoff = clock(tx) - seconds
            return [e for r in reversed(rows) if (e := json.loads(r["payload"]))["object"] == object_key(record)
                    and not e.get("late")
                    and e.get('tool') not in {'search_knowledge','search_policies'}
                    and e["observed_epoch"] >= cutoff and e["result"].get("success")
                    and self.control_versions_valid(tx, e.get("controls"))
                    and e.get('provenance', {}).get('media', []) == media
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
                      "provenance": payload["provenance"], "controls": payload.get("controls", {})}
            from app.observability.interactions import record as trace_record
            trace_record("execution.saved", tx=tx, session_id=request.session_id,
                         job_id=request.job_id, task_id=request.task_id,
                         status="error" if result.get("state") in {"interrupted", "deferred", "expired"} else "ok",
                         output=result)
            tx.execute("""UPDATE runtime_jobs SET status=?,response=?,finished_at=? WHERE job_id=?""",
                       ("interrupted" if interrupted else "completed", json.dumps(result, ensure_ascii=False), clock(tx), request.job_id))
            if not interrupted and result.get('needs_human') is True:
                control = self.store._control(tx, request.session_id, self.user)
                reason = 'operation_preparation_requires_human' if result.get('reason') == 'operation_preparation_requires_human' else 'agent_escalation'
                ticket = self.store._handoff(tx, control, reason, 'system')
                result.update(ticket_id=ticket['ticket_id'], reply='已进入人工队列，客服将继续核实处理。')
                tx.execute('UPDATE runtime_jobs SET response=? WHERE job_id=?',
                    (json.dumps(result, ensure_ascii=False), request.job_id))
            return result
