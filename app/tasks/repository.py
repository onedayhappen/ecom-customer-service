"""User-owned matters and their provenance, independent of a chat session.

Construct with the authenticated identity, never a user_id from model output or
request JSON. This internal API does not enable the new conversation pipeline.
All mutations commit the task, source links, event and retry receipt together.
"""
import hashlib
import json
from contextlib import nullcontext
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import ValidationError

from app.support.store import SupportError, now
from app.tasks.migrations import encoded, json_value
from app.tasks.models import MessageSource, QuestionSpec, TaskChange, TaskContent, TaskPatch, TaskRecord


class VersionConflict(SupportError):
    def __init__(self):
        super().__init__("事项已更新，请读取最新记录后重新判断")


class TaskRepository:
    def __init__(self, store, authenticated_user_id):
        if not isinstance(authenticated_user_id, str) or not authenticated_user_id.strip():
            raise SupportError("缺少已认证用户", 401)
        self.store, self.user_id = store, authenticated_user_id

    @staticmethod
    def _validate(model, value):
        try:
            return model.model_validate(value)
        except ValidationError:
            # Do not echo source/customer data in errors or logs.
            raise SupportError("事项字段或来源格式不正确", 422) from None

    def _record(self, row):
        if row["storage_mode"] == "legacy":
            # Old flows still write their old columns. Expose a read-only view
            # until rollout, not a second writable copy of their progress.
            status = {
                "queued": "in_progress", "ready": "in_progress", "running": "in_progress",
                "waiting_input": "waiting_user", "waiting_confirmation": "waiting_user",
                "needs_human": "paused",
            }.get(row["status"], row["status"])
            content = dict(goal=row["request_text"] or row["title"],
                           details=json_value(row["slots"], {}),
                           waiting_for=json_value(row["question"]), status=status)
        else:
            content = dict(goal=row["goal"],
                           details=row["details"] if self.store.postgres else json_value(row["details"], {}),
                           waiting_for=row["waiting_for"] if self.store.postgres else json_value(row["waiting_for"]),
                           status=row["status"])
        return TaskRecord(**content, **{key: row[key] for key in (
            "task_id", "user_id", "version", "created_at", "updated_at", "storage_mode")})

    def _row(self, tx, task_id, *, lock=False):
        row = tx.one("SELECT * FROM conversation_tasks WHERE task_id=? AND user_id=?" +
                     (" FOR UPDATE" if lock and tx.postgres else ""), (task_id, self.user_id))
        if row is None:
            raise SupportError("事项不存在", 404)
        return row

    def get(self, task_id, *, session_id=None):
        with self.store.transaction(read_only=True) as tx:
            if session_id is not None:
                self.store._control(tx, session_id, self.user_id)
            return self._record(self._row(tx, task_id))

    def list(self, *, session_id=None, include_closed=False, limit=50, before=None):
        """Stable keyset pagination. before is the last (updated_at, task_id)."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SupportError("分页参数不合法", 422)
        with self.store.transaction(read_only=True) as tx:
            conditions, args = ["t.user_id=?"], [self.user_id]
            if session_id is not None:
                self.store._control(tx, session_id, self.user_id)
                conditions.append("EXISTS (SELECT 1 FROM task_sessions s "
                                  "WHERE s.task_id=t.task_id AND s.session_id=?)")
                args.append(session_id)
            if not include_closed:
                conditions.append("t.status NOT IN ('completed','cancelled')")
            if before is not None:
                if not isinstance(before, (tuple, list)) or len(before) != 2 or not all(isinstance(x, str) for x in before):
                    raise SupportError("分页参数不合法", 422)
                conditions.append("(t.updated_at<? OR (t.updated_at=? AND t.task_id<?))")
                args.extend([before[0], before[0], before[1]])
            return [self._record(r) for r in tx.all(
                "SELECT t.* FROM conversation_tasks t WHERE " + " AND ".join(conditions) +
                " ORDER BY t.updated_at DESC,t.task_id DESC LIMIT ?", (*args, limit))]

    def history(self, task_id, *, after_version=0, limit=100):
        if type(after_version) is not int or after_version < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise SupportError("历史分页参数不合法", 422)
        with self.store.transaction(read_only=True) as tx:
            self._row(tx, task_id)
            return [json.loads(row["payload"]) for row in tx.all(
                """SELECT e.payload FROM task_changes c JOIN support_events e
                ON e.session_id=c.session_id AND e.sequence=c.sequence
                WHERE c.user_id=? AND c.task_id=? AND c.version>?
                ORDER BY c.version LIMIT ?""", (self.user_id, task_id, after_version, limit))]

    def _source(self, tx, source):
        # A session owned by the same user is necessary but not sufficient: the
        # exact immutable public user message or server-recorded tool event must exist.
        self._check_source_owner(tx, source.session_id)
        if isinstance(source, MessageSource):
            row = tx.one("SELECT * FROM support_events WHERE session_id=? AND sequence=?",
                         (source.session_id, source.sequence))
            if not row or row["kind"] != "message" or row["visibility"] != "public" or row["actor"] != self.user_id:
                raise SupportError("来源消息不存在或不可用", 422)
            if json.loads(row["payload"]).get("role") != "user":
                raise SupportError("事项补充必须引用用户原话", 422)
        else:
            row = tx.one("SELECT payload FROM case_snapshots WHERE user_id=?", (self.user_id,))
            events = json.loads(row["payload"]).get("events", []) if row else []
            if not any(e.get("event_id") == source.event_id and e.get("event_type") == "tool_result"
                       and e.get("user_id") == self.user_id and e.get("session_id") == source.session_id for e in events):
                raise SupportError("来源工具结果不存在或不可用", 422)

    def _check_source_owner(self, tx, session_id):
        # Reading another session must not lock it after locking the task/session:
        # two cross-session continuations must not deadlock in opposite order.
        row = tx.one("SELECT user_id FROM support_conversations WHERE session_id=?", (session_id,))
        if not row or row["user_id"] != self.user_id:
            raise SupportError("会话不存在", 404)

    @staticmethod
    def _fingerprint(payload):
        try:
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise SupportError("事项内容必须是有效 JSON", 422) from None
        if len(raw.encode("utf-8")) > 65536:
            raise SupportError("事项内容过长", 422)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _reserve(self, tx, change, fingerprint, task_id, session_id, version):
        inserted = tx.execute("""INSERT INTO task_changes
            (user_id,change_id,request_hash,task_id,session_id,version)
            VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,change_id) DO NOTHING""",
            (self.user_id, change.change_id, fingerprint, task_id, session_id, version)).rowcount
        if inserted:
            return None
        row = tx.one("""SELECT c.request_hash,e.payload FROM task_changes c
            JOIN support_events e ON e.session_id=c.session_id AND e.sequence=c.sequence
            WHERE c.user_id=? AND c.change_id=?""", (self.user_id, change.change_id))
        if not row or row["request_hash"] != fingerprint:
            raise SupportError("变更编号已用于不同内容")
        return TaskRecord.model_validate(json.loads(row["payload"])["after"])

    def _save_event(self, tx, control, change, before, after):
        source = change.source
        for sid in {control["session_id"], source.session_id}:
            tx.execute("INSERT INTO task_sessions VALUES (?,?,?) ON CONFLICT DO NOTHING",
                       (after.task_id, sid, after.updated_at))
        if isinstance(source, MessageSource):
            tx.execute("INSERT INTO support_task_messages VALUES (?,?,?) ON CONFLICT DO NOTHING",
                       (after.task_id, source.session_id, source.sequence))
        sequence = self.store._event(tx, control, "matter_change", self.user_id, {
            "change_id": change.change_id, "task_id": after.task_id, "version": after.version,
            "source": source.model_dump(), "before": before.model_dump() if before else None,
            "after": after.model_dump(),
        }, private=True, key="matter:" + change.change_id)
        tx.execute("UPDATE task_changes SET sequence=? WHERE user_id=? AND change_id=?",
                   (sequence, self.user_id, change.change_id))

    def create(self, session_id, *, change_id, source, goal, details=None, waiting_for=None, status="in_progress", tx=None):
        change = self._validate(TaskChange, dict(change_id=change_id, source=source))
        content = self._validate(TaskContent, dict(goal=goal, details=details if details is not None else {},
                                                 waiting_for=waiting_for, status=status))
        fingerprint = self._fingerprint(dict(action="create", session_id=session_id,
                                            change=change.model_dump(), content=content.model_dump()))
        with (nullcontext(tx) if tx is not None else self.store.transaction()) as tx:
            control = self.store._control(tx, session_id, self.user_id)
            task_id, timestamp = str(uuid4()), now()
            replay = self._reserve(tx, change, fingerprint, task_id, session_id, 1)
            if replay is not None:
                return replay
            self._source(tx, change.source)
            record = TaskRecord(**content.model_dump(), task_id=task_id, user_id=self.user_id,
                                version=1, created_at=timestamp, updated_at=timestamp, storage_mode="durable")
            tx.execute("""INSERT INTO conversation_tasks
                (task_id,session_id,intent,title,request_text,slots,status,version,question,
                 created_at,updated_at,created_sequence,user_id,goal,details,waiting_for,storage_mode)
                VALUES (?,?,?,'','','{}',?,?,NULL,?,?,?,?,?,?,?,'durable')""",
                (task_id, session_id, "matter", record.status, 1, timestamp, timestamp,
                 control["sequence"] + 1, self.user_id, record.goal,
                 encoded(tx, record.details), encoded(tx, record.waiting_for)))
            self._save_event(tx, control, change, None, record)
            return record

    def update(self, task_id, session_id, *, expected_version, change_id, source, patch, tx=None):
        if type(expected_version) is not int or expected_version < 1:
            raise SupportError("事项版本不合法", 422)
        change = self._validate(TaskChange, dict(change_id=change_id, source=source))
        patch = self._validate(TaskPatch, patch).model_dump(exclude_unset=True)
        fingerprint = self._fingerprint(dict(action="update", task_id=task_id, session_id=session_id,
                                            expected_version=expected_version, change=change.model_dump(), patch=patch))
        with (nullcontext(tx) if tx is not None else self.store.transaction()) as tx:
            control = self.store._control(tx, session_id, self.user_id)
            replay = self._reserve(tx, change, fingerprint, task_id, session_id, expected_version + 1)
            if replay is not None:
                self._row(tx, task_id)
                return replay
            row = self._row(tx, task_id, lock=True)
            if row["storage_mode"] != "durable":
                raise SupportError("历史事项尚未迁移到新执行入口")
            if row["version"] != expected_version:
                raise VersionConflict()
            self._source(tx, change.source)
            before = self._record(row)
            content = self._validate(TaskContent, {
                **before.model_dump(include={"goal", "details", "waiting_for", "status"}), **patch})
            after = before.model_copy(update={**content.model_dump(), "version": expected_version + 1, "updated_at": now()})
            updated = tx.execute("""UPDATE conversation_tasks
                SET goal=?,details=?,waiting_for=?,status=?,version=?,updated_at=?
                WHERE task_id=? AND user_id=? AND version=?""",
                (after.goal, encoded(tx, after.details), encoded(tx, after.waiting_for),
                 after.status, after.version, after.updated_at, task_id, self.user_id, expected_version)).rowcount
            if updated != 1:
                raise VersionConflict()
            self._save_event(tx, control, change, before, after)
            return after

    def ask(self, task_id, session_id, *, expected_version, change_id, source, question, tx=None):
        """Save a version-bound question; publishing it is a separate step."""
        if type(expected_version) is not int or expected_version < 1:
            raise SupportError("事项版本不合法", 422)
        question = self._validate(QuestionSpec, question)
        question_id = str(uuid5(NAMESPACE_URL, json.dumps([self.user_id, task_id, change_id])))
        return self.update(task_id, session_id, expected_version=expected_version,
                           change_id=change_id, source=source, tx=tx, patch={
                               "status": "waiting_user",
                               "waiting_for": {**question.model_dump(), "kind": "question",
                                               "question_id": question_id, "task_version": expected_version + 1},
                           })
