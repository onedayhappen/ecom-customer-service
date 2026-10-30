# 文件说明：以追加记录方式纠正错误关联，并补偿受影响字段和来源关系。
"""Append-only correction of an association, with field-level compensation."""
from copy import deepcopy
import json
import re
from typing import Literal

from pydantic import Field

from app.runtime.outbox import clock
from app.support.store import SupportError, now
from app.tasks.association import _id
from app.tasks.candidates import CandidateService
from app.tasks.models import Source, StrictModel
from app.tasks.provenance import changed_fields, event_ref, invalid, revision
from app.tasks.repository import TaskRepository, VersionConflict


class CorrectionRequest(StrictModel):
    correction_id: str = Field(min_length=1, max_length=200)
    decision_id: str = Field(min_length=1, max_length=200)
    source: Source
    reason: Literal["user_correction", "tool_conflict", "validation_failure"]
    target_task_id: str | None = None
    expected_versions: dict[str, int]


def _value(record, path):
    if path.startswith("details."):
        field = path[8:]
        values = (record or {}).get("details", {})
        return field in values, values.get(field)
    return path in (record or {}), (record or {}).get(path)


def _set(record, path, value):
    present, content = value
    container, field = (record["details"], path[8:]) if path.startswith("details.") else (record, path)
    if present:
        container[field] = deepcopy(content)
    else:
        container.pop(field, None)


class CorrectionService:
    def __init__(self, store, authenticated_user_id, *, scope=None):
        self.store, self.user = store, authenticated_user_id
        self.repo = TaskRepository(store, authenticated_user_id)
        self.candidates = CandidateService(store, authenticated_user_id, scope=scope)

    def _decision(self, tx, did):
        row = tx.one("""SELECT d.*,e.payload FROM association_decisions d JOIN support_events e
            ON e.session_id=d.session_id AND e.sequence=d.event_sequence
            WHERE d.decision_id=? AND d.user_id=?""", (did, self.user))
        if not row or not row["task_id"] or not row["change_id"]:
            raise SupportError("可纠正的关联不存在", 404)
        change = tx.one("""SELECT e.payload FROM task_changes c JOIN support_events e
            ON e.session_id=c.session_id AND e.sequence=c.sequence
            WHERE c.user_id=? AND c.change_id=?""", (self.user, row["change_id"]))
        if not change:
            raise SupportError("关联变更记录缺失")
        return row, json.loads(row["payload"]), json.loads(change["payload"])

    def _history(self, tx, task_id, after_version):
        return [json.loads(r["payload"]) for r in tx.all("""SELECT e.payload FROM task_changes c
            JOIN support_events e ON e.session_id=c.session_id AND e.sequence=c.sequence
            WHERE c.user_id=? AND c.task_id=? AND c.version>? ORDER BY c.version""",
            (self.user, task_id, after_version))]

    def _invalidate(self, tx, kind, source_id, correction_id):
        tx.execute("""INSERT INTO task_invalid_sources VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
                   (self.user, kind, source_id, correction_id))

    def _operations(self, tx, task_ids):
        records = {}
        for tid in task_ids:
            rows = tx.all("""SELECT o.* FROM support_operations o JOIN task_sessions s ON s.session_id=o.session_id
                WHERE s.task_id=? AND o.status IN ('running','unknown','succeeded')
                AND (EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id AND m.task_id=?)
                    OR NOT EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id))""", (tid, tid))
            records.update((r["operation_id"], {"operation_id": r["operation_id"], "status": r["status"]}) for r in rows)
        return list(records.values())

    def _target_evidence(self, tx, request, target):
        if request.source.kind != "message" or request.reason != "user_correction":
            return
        row = tx.one("SELECT payload FROM support_events WHERE session_id=? AND sequence=?",
                     (request.source.session_id, request.source.sequence))
        content = json.loads(row["payload"])["content"]
        order = str(target.details.get("order_id") or "")
        label = order or target.goal
        named = order.upper() in self.candidates._order_ids(content) if order else label in content
        if not named or re.search(r"(?:不是|并非)\s*" + re.escape(label), content, re.I):
            raise SupportError("正确事项尚未明确，请先澄清", 422)
        # An order can contain several goals. An order reference alone does not
        # select one of those goals.
        if order:
            from app.tasks.candidates import _entity_field
            options = self.candidates._rows(tx, [f"{_entity_field(tx, 'order_id')}=?"], [order])
            if len(options) > 1 and target.goal not in content and target.task_id not in content:
                raise SupportError("同一订单有多个事项，请明确要纠正到哪件事", 422)

    def _relink(self, tx, old, target, sources, change_id):
        for source in sources:
            sid, seq = source["session_id"], source["sequence"]
            other = tx.one("""SELECT d.decision_id FROM association_sources s JOIN association_decisions d
                ON d.decision_id=s.decision_id WHERE d.user_id=? AND d.task_id=? AND s.session_id=? AND s.sequence=?
                AND NOT EXISTS(SELECT 1 FROM task_invalid_sources i WHERE i.user_id=d.user_id
                    AND i.kind='decision' AND i.source_id=d.decision_id) LIMIT 1""", (self.user, old, sid, seq))
            # Standalone repository changes may share this source too.
            surviving = any(e["change_id"] != change_id and
                            not invalid(tx, self.user, "change", e["change_id"]) and
                            e.get("source") == {"kind": "message", "session_id": sid, "sequence": seq}
                            for e in self._history(tx, old, 0))
            if not other and not surviving:
                tx.execute("DELETE FROM support_task_messages WHERE task_id=? AND session_id=? AND sequence=?", (old, sid, seq))
            if target:
                tx.execute("INSERT INTO support_task_messages VALUES (?,?,?) ON CONFLICT DO NOTHING", (target, sid, seq))
                tx.execute("INSERT INTO task_sessions VALUES (?,?,?) ON CONFLICT DO NOTHING", (target, sid, now()))

    def _notice_and_caches(self, tx, control, request, task_ids, versions, operations):
        sessions = {control["session_id"]}
        published = False
        for tid in task_ids:
            sessions.update(r["session_id"] for r in tx.all("SELECT session_id FROM task_sessions WHERE task_id=?", (tid,)))
        for sid in sorted(sessions):
            # Message text remains in the transcript for audit. Context reads
            # exclude invalidated responses even while rebuilding is pending.
            rows = tx.all("""SELECT sequence,payload FROM support_events WHERE session_id=?
                AND kind='message' AND visibility='public'""", (sid,))
            for row in rows:
                p = json.loads(row["payload"])
                if p.get("role") not in {"assistant", "agent"}:
                    continue
                if (p.get("decision_id") == request.decision_id or request.decision_id in p.get("decision_ids", [])
                        or any(tid in versions and version >= versions[tid]
                               for tid, version in p.get("task_versions", {}).items())
                        or (p.get("task_id") in versions and
                            p.get("task_version", 0) >= versions[p["task_id"]])):
                    self._invalidate(tx, "event", event_ref(sid, row["sequence"]), request.correction_id)
                    published = True
            tx.execute("DELETE FROM support_context_snapshots WHERE session_id=?", (sid,))
            from app.session.short_term import enqueue_context_job
            current = tx.one("SELECT * FROM support_conversations WHERE session_id=?", (sid,))
            enqueue_context_job(tx, current)
            for tid in task_ids:
                enqueue_context_job(tx, current, tid)
        if published and control["mode"] == "bot":
            content = ("之前把信息关联到了错误事项，现已更正。已发起的业务操作需要核实，不能确认已撤回。"
                       if operations else "之前把信息关联到了错误事项，已修正相关记录。后续将按更正后的事项处理。")
            self.store._event(tx, control, "message", "bot", {"role": "assistant", "content": content,
                             "correction_id": request.correction_id}, key="correction-notice:" + request.correction_id)
        return published and control["mode"] == "bot"

    def correct(self, session_id, request):
        request = CorrectionRequest.model_validate(request)
        if request.reason == "user_correction" and request.source.kind != "message":
            raise SupportError("用户纠正必须引用用户原话", 422)
        fingerprint = self.repo._fingerprint({"session_id": session_id, **request.model_dump()})
        with self.store.transaction() as tx:
            from app.tasks.provenance import lock_input_user
            lock_input_user(tx, self.user)
            control = self.store._control(tx, session_id, self.user)
            previous = tx.one("SELECT request_hash,result FROM task_corrections WHERE user_id=? AND correction_id=?",
                              (self.user, request.correction_id))
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise SupportError("纠错编号已用于不同内容")
                return json.loads(previous["result"])
            revision(tx, self.user, lock=True)
            self.repo._source(tx, request.source)
            row, event, change = self._decision(tx, request.decision_id)
            if invalid(tx, self.user, "decision", request.decision_id):
                earlier = tx.all("""SELECT result FROM task_corrections WHERE user_id=? AND decision_id=?
                    ORDER BY created_at DESC""", (self.user, request.decision_id))
                last = json.loads(earlier[0]["result"]) if earlier else {}
                if not (last.get("needs_clarification") and last.get("target_task_id") is None and request.target_task_id):
                    raise SupportError("该关联已被纠正，请引用新的关联记录")
            old_id, target_id = row["task_id"], request.target_task_id
            if old_id == target_id:
                raise SupportError("正确事项与错误事项相同；字段补充应通过正常事项更新", 422)
            ids = sorted({old_id} | ({target_id} if target_id else set()))
            if set(request.expected_versions) != set(ids):
                raise SupportError("缺少受影响事项版本", 422)
            records = {}
            for tid in ids:
                if not self.candidates._rows(tx, ["t.task_id=?"], [tid]):
                    raise SupportError("事项不存在", 404)
                record = self.repo._record(self.repo._row(tx, tid, lock=True))
                if record.storage_mode != "durable" or record.version != request.expected_versions[tid]:
                    raise VersionConflict()
                records[tid] = record
            if target_id:
                self._target_evidence(tx, request, records[target_id])
            operations = self._operations(tx, ids)
            source_rows = tx.all("SELECT session_id,sequence FROM association_sources WHERE decision_id=?", (request.decision_id,))
            self._invalidate(tx, "decision", request.decision_id, request.correction_id)
            tx.execute("""UPDATE task_attachments SET status='invalidated',version=version+1
                WHERE decision_id=? AND status='active'""", (request.decision_id,))
            self._invalidate(tx, "change", row["change_id"], request.correction_id)
            tx.execute("UPDATE task_provenance_users SET revision=revision+1 WHERE user_id=?", (self.user,))
            original = records[old_id]
            bad_fields = set(change.get("written_fields", changed_fields(change["before"], change["after"])))
            later = self._history(tx, old_id, change["after"]["version"])
            later_fields = set().union(*(set(e.get("written_fields", changed_fields(e["before"], e["after"]))) for e in later))
            corrected = original.model_dump()
            restored, preserved, conflicts = [], [], []
            for path in sorted(bad_fields):
                if path in later_fields:
                    preserved.append(path)
                elif _value(corrected, path) != _value(change["after"], path):
                    conflicts.append(path)
                elif change["before"] is not None or path.startswith("details."):
                    _set(corrected, path, _value(change["before"], path))
                    restored.append(path)
            # Empty erroneously created matters are closed, never erased.
            empty_wrong_task = change["before"] is None and not later and not operations
            if empty_wrong_task:
                corrected.update(status="cancelled", waiting_for=None)
            needs_clarification = target_id is None or bool(conflicts)
            if operations:
                corrected.update(status="paused", waiting_for={"kind": "correction_review",
                                 "correction_id": request.correction_id, "operations": operations})
            elif needs_clarification and not empty_wrong_task:
                corrected.update(status="paused", waiting_for={"kind": "correction_selection",
                                 "correction_id": request.correction_id})
            after_old = self.repo.update(old_id, session_id, expected_version=original.version,
                change_id=_id(request.correction_id, "restore"), source=request.source.model_dump(),
                patch={key: corrected.get(key) for key in ("goal", "details", "status", "waiting_for")}, tx=tx)
            target_decision = None
            target_conflicts = []
            if target_id:
                target = records[target_id]
                details = deepcopy(target.details)
                moved_fields = []
                for detail in event["decision"].get("details", []):
                    key, value = detail["field"], detail["value"]
                    if key in {"order_id", "item_id", "product_id", "sku_id", "platform_code", "store_id"} and key in details:
                        continue
                    if key in details and details[key] != value:
                        target_conflicts.append("details." + key)
                    else:
                        details[key] = value
                        moved_fields.append("details." + key)
                needs_clarification |= bool(target_conflicts)
                patch = {"details": details}
                if operations or target_conflicts:
                    patch.update(status="paused", waiting_for={"kind": "correction_review" if operations else "correction_selection",
                                 "correction_id": request.correction_id, "operations": operations})
                target_decision = _id(request.correction_id, "reassociate")
                target_record = self.repo.update(target_id, session_id, expected_version=target.version,
                    change_id=target_decision, source=request.source.model_dump(), patch=patch,
                    written_fields=moved_fields, tx=tx)
                control = self.store._control(tx, session_id, self.user)
                transferred = deepcopy(event["decision"])
                transferred.update(task_id=target_id, task_version=target.version, action="update", effect="supplement",
                                   details=[d for d in transferred.get("details", []) if "details." + d["field"] in moved_fields])
                sequence = self.store._event(tx, control, "association_decision", "system",
                    {"decision_id": target_decision, "decision": transferred, "task_id": target_id,
                     "task_version": target_record.version, "change_id": target_decision,
                     "corrects": request.decision_id}, private=True, key="association:" + target_decision)
                tx.execute("INSERT INTO association_decisions VALUES (?,?,?,?,?,?,?,?)",
                           (target_decision, "correction:" + request.correction_id, self.user, session_id, target_id,
                            sequence, target_decision, "update"))
                tx.execute('''INSERT INTO task_attachments
                    (task_id,attachment_id,source_session_id,source_sequence,decision_id)
                    SELECT ?,l.attachment_id,l.source_session_id,l.source_sequence,?
                    FROM task_attachments l JOIN media_assets a ON a.attachment_id=l.attachment_id
                    JOIN message_attachments m ON m.attachment_id=l.attachment_id
                        AND m.session_id=l.source_session_id AND m.sequence=l.source_sequence
                    WHERE l.decision_id=? AND a.user_id=? AND a.status='ready' AND a.expires_at>?
                        AND m.status='active' ON CONFLICT DO NOTHING''',
                    (target_id, target_decision, request.decision_id, self.user, clock(tx)))
                for source in source_rows:
                    tx.execute("INSERT INTO association_sources VALUES (?,?,?) ON CONFLICT DO NOTHING",
                               (target_decision, source["session_id"], source["sequence"]))
                if request.source.kind == "message":
                    tx.execute("INSERT INTO association_sources VALUES (?,?,?) ON CONFLICT DO NOTHING",
                               (target_decision, request.source.session_id, request.source.sequence))
            self._relink(tx, old_id, target_id, source_rows, row["change_id"])
            # Old questions and any in-flight consumers fail their version
            # checks after this transaction; wakeups are durable, not required
            # for the correctness of invalidation.
            control = self.store._control(tx, session_id, self.user)
            published = self._notice_and_caches(tx, control, request, ids,
                {old_id: change["after"]["version"]}, operations)
            result = {"correction_id": request.correction_id, "decision_id": request.decision_id,
                      "old_task_id": old_id, "target_task_id": target_id, "new_decision_id": target_decision,
                      "restored_fields": restored, "preserved_fields": preserved,
                      "conflicts": conflicts + target_conflicts, "needs_clarification": needs_clarification,
                      "operations_to_verify": operations, "corrected_public_reply": published,
                      "task_versions": {old_id: after_old.version, **({target_id: target_record.version} if target_id else {})}}
            if needs_clarification:
                result["prompt"] = "请说明正确的订单和事项，以及应保留的内容。"
            if operations:
                ticket = self.store._handoff(tx, control, "association_correction_requires_business_review", self.user)
                note = "\n事项关联纠错：\n" + json.dumps({
                    "correction_id": request.correction_id, "old_task_id": old_id,
                    "target_task_id": target_id, "operations_to_verify": operations,
                    "instruction": "查询这些操作的真实状态；不要重提或宣称已撤回。",
                }, ensure_ascii=False)
                tx.execute("UPDATE support_tickets SET summary=summary || ?,version=version+1 WHERE ticket_id=?",
                           (note, ticket["ticket_id"]))
            self.store._event(tx, control, "association_correction", "system",
                {**result, "reason": request.reason, "source": request.source.model_dump()}, private=True,
                key="correction:" + request.correction_id)
            timestamp = clock(tx)
            tx.execute("""INSERT INTO runtime_jobs
                (job_id,session_id,user_id,control_version,payload,status,created_at,queue_deadline,run_timeout,next_delivery,kind)
                VALUES (?,?,?,?,?,'pending',?,?,?,?, 'task_invalidation')""",
                (_id(request.correction_id, "invalidate"), session_id, self.user, control["control_version"],
                 json.dumps(result, ensure_ascii=False), timestamp, timestamp + 86400, 60, timestamp))
            tx.execute("INSERT INTO task_corrections VALUES (?,?,?,?,?,?,?)",
                (self.user, request.correction_id, fingerprint, request.decision_id, session_id,
                 json.dumps(result, ensure_ascii=False), now()))
            return result
