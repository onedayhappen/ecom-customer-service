"""Publish ready pieces once, in the transaction that checks their freshness.

No model or network call occurs here. Bounded selection and typed rendering
remove the need to repeatedly regenerate prose while new messages arrive.
"""
import json

from app.runtime.outbox import clock
from app.support.store import SupportError, now
from app.tasks.association import _id
from app.tasks.execution_store import TaskRuns, object_key
from app.tasks.provenance import invalid, lock_input_user, revision
from app.tasks.reply_facts import facts, label, question_text


class ReplyCoordinator:
    def __init__(self, store, user, *, scope=None):
        self.store, self.user = store, user
        self.runs = TaskRuns(store, user, scope=scope)

    def _covered(self, tx, key):
        return tx.one("""SELECT item_key FROM task_reply_items
            WHERE user_id=? AND item_key=? AND disposition<>'deferred'""", (self.user, key))

    def _mark(self, tx, sid, key, sequence=None, disposition="published"):
        tx.execute("""INSERT INTO task_reply_items VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id,item_key) DO UPDATE SET sequence=excluded.sequence,
                disposition=excluded.disposition,checked_at=excluded.checked_at
            WHERE task_reply_items.disposition='deferred'""",
            (self.user, key, sid, sequence, disposition, now(), clock(tx)))

    def _receipts(self, tx, record, result):
        receipts = []
        for eid in result.get("evidence_ids", [])[:30]:
            row = tx.one("""SELECT e.payload FROM support_events e JOIN support_conversations c
                ON c.session_id=e.session_id WHERE c.user_id=? AND e.event_key=?
                AND e.kind='task_tool_result'""", (self.user, "task-tool:" + eid))
            evidence = json.loads(row["payload"]) if row else {}
            if (evidence.get("task_id") != record.task_id or evidence.get("object") != object_key(record)
                    or not isinstance(evidence.get("result"), dict)
                    or evidence["result"].get("success") is not True
                    or evidence.get("observed_epoch", 0) < clock(tx) - 60
                    or not self.runs.control_versions_valid(tx, evidence.get("controls"))
                    or any(invalid(tx, self.user, "decision", did)
                           for did in evidence.get("provenance", {}).get("decisions", []))):
                continue
            receipts.append(evidence)
        return receipts

    def _open_question(self, tx, sid):
        # Only questions actually sent and still at their original version count.
        q = "payload::jsonb->>'question_id'" if tx.postgres else "json_extract(payload,'$.question_id')"
        c = "payload::jsonb->>'clarification_id'" if tx.postgres else "json_extract(payload,'$.clarification_id')"
        rows = tx.all(f"""SELECT payload FROM support_events WHERE session_id=? AND kind='message'
            AND visibility='public' AND ({q} IS NOT NULL OR {c} IS NOT NULL)
            ORDER BY sequence DESC LIMIT 100""", (sid,))
        has_task_question = False
        for row in rows:
            p = json.loads(row["payload"])
            if p.get("clarification_id"):
                if tx.one("""SELECT clarification_id FROM association_clarifications
                    WHERE clarification_id=? AND status='open'""", (p["clarification_id"],)):
                    return "clarification"
            if p.get("question_id") and p.get("task_id"):
                record = self.runs.repo._record(self.runs.repo._row(tx, p["task_id"]))
                if (record.version == p.get("task_version") and record.status == "waiting_user"
                        and isinstance(record.waiting_for, dict)
                        and record.waiting_for.get("question_id") == p["question_id"]):
                    has_task_question = True
        return "task" if has_task_question else None

    def publish(self, sid):
        with self.store.transaction() as tx:
            # Also fences an unassociated correction arriving in another session.
            lock_input_user(tx, self.user)
            control = self.store._control(tx, sid, self.user)
            revision(tx, self.user, lock=True)
            tx.execute("INSERT INTO task_reply_sessions(session_id) VALUES (?) ON CONFLICT DO NOTHING", (sid,))
            tx.execute("UPDATE task_reply_sessions SET checked_at=? WHERE session_id=?", (clock(tx), sid))
            if control["mode"] != "bot":
                return None
            position = tx.one("SELECT received_sequence FROM message_intake_sessions WHERE session_id=?", (sid,))
            received = position["received_sequence"] if position else 0
            pending = tx.one("""SELECT i.session_id FROM message_intake_items i
                JOIN support_conversations c ON c.session_id=i.session_id
                WHERE i.user_id=? AND i.association_status='pending' AND c.mode='bot' LIMIT 1""", (self.user,))
            if pending:
                # One receipt per uninterrupted pending episode, never one per message.
                noticed = tx.one("SELECT pending_notice FROM task_reply_sessions WHERE session_id=?", (sid,))
                if noticed["pending_notice"] or not received:
                    return None
                tx.execute("UPDATE task_reply_sessions SET pending_notice=1 WHERE session_id=?", (sid,))
                event_id = _id(sid, "receiving:" + str(received))
                return self._publish(tx, control, [], "消息已收到，正在核对补充内容。", received, event_id)
            tx.execute("UPDATE task_reply_sessions SET pending_notice=0 WHERE session_id=?", (sid,))
            pieces, blocked, question = [], set(), self._open_question(tx, sid)
            clarifications = tx.all("""SELECT * FROM association_clarifications
                WHERE user_id=? AND status='open' ORDER BY created_at LIMIT 100""", (self.user,))
            block_all = False
            for row in clarifications:
                options = json.loads(row["candidates"])
                blocked.update(o["task_id"] for o in options)
                block_all |= not options
                key = "clarification:" + row["clarification_id"]
                if row["session_id"] != sid or question == "clarification" or self._covered(tx, key):
                    continue
                labels, versions = [], {}
                for option in options:
                    self.runs._scope(tx, option["task_id"])
                    record = self.runs.repo._record(self.runs.repo._row(tx, option["task_id"], lock=True))
                    if record.version == option["version"]:
                        versions[record.task_id] = record.version
                        labels.append(label(record.details.get("order_id") or record.goal))
                text = "请说明这次要处理的订单号，以及希望如何处理。"
                # Keep option numbering identical to the validated association contract.
                if labels and len(labels) == len(options):
                    text = "您说的是哪一件：" + "；".join(f"{i + 1}. {v}" for i, v in enumerate(labels)) + "？"
                pieces.append(dict(key=key, content=text, task_versions=versions, clarification_id=row["clarification_id"]))
                question = "clarification"
            jobs = tx.all("""SELECT j.* FROM runtime_jobs j LEFT JOIN task_reply_items r
                ON r.user_id=j.user_id AND r.item_key='job:' || j.job_id
                WHERE j.user_id=? AND j.session_id=? AND j.kind='task_execution'
                AND j.status IN ('completed','interrupted','expired') AND j.payload<>'{}'
                AND (r.disposition IS NULL OR r.disposition='deferred')
                ORDER BY COALESCE(r.checked_at,0),j.created_at,j.job_id LIMIT 50""", (self.user, sid))
            for job in jobs:
                if len(pieces) >= 8:
                    break
                key = "job:" + job["job_id"]
                payload, result = json.loads(job["payload"]), json.loads(job["response"] or "{}")
                tid = payload["task_id"]
                self.runs._scope(tx, tid)
                record = self.runs.repo._record(self.runs.repo._row(tx, tid, lock=True))
                if not result.get("provenance") and job["status"] in {"expired", "interrupted"}:
                    # An expired queued job never acquired an execution stamp.
                    # Report only a failure, and only if no later retry supersedes it.
                    task_field = "payload::jsonb->>'task_id'" if tx.postgres else "json_extract(payload,'$.task_id')"
                    later = tx.one(f"""SELECT job_id FROM runtime_jobs WHERE user_id=? AND kind='task_execution'
                        AND {task_field}=? AND created_at>? LIMIT 1""", (self.user, tid, job["created_at"]))
                    if not later and record.status == "in_progress" and payload["request"]["expected_version"] == record.version:
                        result = dict(state="deferred", provenance=self.runs.guard.capture([tid], tx=tx))
                stamp = result.get("provenance")
                if (not self.runs.guard.valid(stamp, tx=tx)
                        or not self.runs.control_versions_valid(tx, payload.get("controls"))
                        or tx.one("""SELECT c.session_id FROM task_sessions t JOIN support_conversations c
                            ON t.session_id=c.session_id WHERE t.task_id=? AND c.mode<>'bot' LIMIT 1""", (tid,))):
                    self._mark(tx, sid, key, disposition="stale")
                    continue
                if block_all or tid in blocked:
                    self._mark(tx, sid, key, disposition="deferred")
                    continue
                if result.get("state") == "interrupted":
                    self._mark(tx, sid, key, disposition="interrupted")
                    continue
                metadata = dict(key=key, task_versions={tid: record.version},
                                decision_ids=stamp["decisions"], evidence_ids=[])
                if record.status == "waiting_user":
                    if question:
                        self._mark(tx, sid, key, disposition="deferred")
                        continue
                    text = question_text(record)
                    if text:
                        metadata.update(task_id=tid, task_version=record.version,
                                        question_id=record.waiting_for["question_id"])
                        question = "task"
                    else:
                        text = "补充信息已记录；目前还无法继续核实，可转人工客服协助。"
                elif record.status in {"paused", "cancelled"}:
                    text = f"订单 {label(record.details.get('order_id'))}的当前事项已" + (
                        "暂停。" if record.status == "paused" else "停止自动处理。")
                elif result.get("state") == "needs_confirmation":
                    from app.tasks.operations import TaskOperations
                    operations = TaskOperations(self.store, self.user, scope=self.runs.candidates.scope)
                    try:
                        row = operations._row(tx, result.get("operation_id"))
                        view = operations._view(tx, row, sid)
                        if row["task_id"] != tid or not view["can_confirm"]:
                            raise SupportError("操作已变化")
                    except SupportError:
                        self._mark(tx, sid, key, disposition="stale")
                        continue
                    text = "订单 " + label(view["review"]["order_id"]) + "：预检已完成，请核对确认卡片后决定是否提交。"
                    metadata["operation_id"] = row["operation_id"]
                else:
                    receipts = self._receipts(tx, record, result)
                    text = facts(record, receipts)
                    metadata["evidence_ids"] = [r["event_id"] for r in receipts]
                    if not text:
                        subject = "订单 " + label(record.details["order_id"]) if record.details.get("order_id") else label(record.goal)
                        text = subject + "：暂时没有可核实的查询结果，可补充信息后重试，或转人工客服核实。"
                    if record.status == "waiting_external":
                        text += "\n后续结果仍需核实，有新进展可以继续咨询。"
                metadata["content"] = text
                pieces.append(metadata)
            if not pieces:
                return None
            event_id = _id(sid, json.dumps(sorted(p["key"] for p in pieces)))
            content = "\n\n".join(p["content"] for p in pieces)
            return self._publish(tx, control, pieces, content, received, event_id)

    def _publish(self, tx, control, pieces, content, received, event_id):
        # Locks remain held through the event, coverage ledger and notification.
        payload = dict(role="assistant", content=content, event_id=event_id,
                       received_sequence=received, result_ids=[p["key"] for p in pieces],
                       task_versions={k: v for p in pieces for k, v in p.get("task_versions", {}).items()},
                       decision_ids=sorted({d for p in pieces for d in p.get("decision_ids", [])}),
                       evidence_ids=sorted({e for p in pieces for e in p.get("evidence_ids", [])}))
        operations = [p["operation_id"] for p in pieces if p.get("operation_id")]
        if operations:
            payload["operation_ids"] = operations
        for p in pieces:
            for key in ("question_id", "task_id", "task_version", "clarification_id"):
                if key in p:
                    payload[key] = p[key]
        seq = self.store._event(tx, control, "message" if pieces else "task_processing", "bot",
                               payload, key="coordinated-reply:" + event_id)
        for p in pieces:
            self._mark(tx, control["session_id"], p["key"], seq)
        for tid in payload["task_versions"]:
            tx.execute("INSERT INTO support_task_messages VALUES (?,?,?) ON CONFLICT DO NOTHING",
                       (tid, control["session_id"], seq))
        return dict(sequence=seq, **payload)
