# 文件说明：统一检查原话及派生证据的版本和有效性，保护执行、确认与发布。
"""Freshness checks shared by execution, confirmation, replies and derived data.

These server-generated stamps are dependency metadata, never authorization.
For a side effect, require_current must run in the transaction that reserves it.
"""
from contextlib import nullcontext
import json

from app.support.store import SupportError
from app.tasks.repository import TaskRepository, VersionConflict


def lock_input_user(tx, user):
    """Serialize intake/publication across sessions, before conversation locks."""
    if tx.postgres:
        tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,82344716))", ("task-input:" + user,))


def event_ref(session_id, sequence):
    return json.dumps([session_id, sequence], ensure_ascii=False, separators=(",", ":"))


def revision(tx, user, *, lock=False):
    if lock:
        tx.execute("INSERT INTO task_provenance_users VALUES (?,0) ON CONFLICT DO NOTHING", (user,))
    row = tx.one("SELECT revision FROM task_provenance_users WHERE user_id=?" +
                 (" FOR UPDATE" if lock and tx.postgres else ""), (user,))
    return row["revision"] if row else 0


def invalid(tx, user, kind, source_id):
    return bool(tx.one("SELECT source_id FROM task_invalid_sources WHERE user_id=? AND kind=? AND source_id=?",
                       (user, kind, source_id)))


def context_event_valid(tx, session_id, sequence):
    owner = tx.one("SELECT user_id FROM support_conversations WHERE session_id=?", (session_id,))
    return bool(owner) and not invalid(tx, owner["user_id"], "event", event_ref(session_id, sequence))


def changed_fields(before, after):
    """Only business content; identity, timestamps and versions are not fields."""
    before = before or {}
    paths = {key for key in ("goal", "status", "waiting_for") if before.get(key) != after.get(key)}
    old, new = before.get("details", {}), after.get("details", {})
    paths.update("details." + key for key in old.keys() | new.keys()
                 if (key in old) != (key in new) or old.get(key) != new.get(key))
    return paths


class ProvenanceGuard:
    def __init__(self, store, user):
        self.store, self.user = store, user
        self.repo = TaskRepository(store, user)

    def capture(self, task_ids=(), decision_ids=(), *, tx=None, memory=None):
        with (nullcontext(tx) if tx is not None else self.store.transaction(read_only=True)) as tx:
            tasks = {tid: self.repo._record(self.repo._row(tx, tid)).version for tid in sorted(set(task_ids))}
            decisions = sorted(set(decision_ids))
            for did in decisions:
                if not tx.one("SELECT decision_id FROM association_decisions WHERE user_id=? AND decision_id=?",
                              (self.user, did)) or invalid(tx, self.user, "decision", did):
                    raise SupportError("来源已失效或不存在", 409)
            from app.media.context import dependency_stamp
            stamp = {"user_id": self.user, "tasks": tasks, "decisions": decisions,
                    'media': dependency_stamp(tx, self.user, tasks),
                    "revision": revision(tx, self.user)}
            if memory:
                from copy import deepcopy
                stamp['memory'] = deepcopy(memory)
            return stamp

    def require_current(self, stamp, *, tx=None, lock=False):
        if lock and tx is None:
            raise ValueError("A locked freshness check must share the reservation transaction")
        with (nullcontext(tx) if tx is not None else self.store.transaction(read_only=not lock)) as tx:
            if not isinstance(stamp, dict) or stamp.get("user_id") != self.user:
                raise SupportError("来源凭据不可用", 409)
            # Lock ordering: provenance row, then sorted tasks. All users of
            # lock=True must follow this order, before reserving external work.
            current_revision = revision(tx, self.user, lock=lock)
            tasks, decisions = stamp.get("tasks"), stamp.get("decisions")
            if not isinstance(tasks, dict) or not isinstance(decisions, list) or "revision" not in stamp:
                raise SupportError("来源凭据不完整", 409)
            if not tasks and stamp["revision"] != current_revision:
                raise VersionConflict()
            for tid in sorted(tasks):
                if self.repo._record(self.repo._row(tx, tid, lock=lock)).version != tasks[tid]:
                    raise VersionConflict()
            for did in decisions:
                if not tx.one("SELECT decision_id FROM association_decisions WHERE user_id=? AND decision_id=?",
                              (self.user, did)) or invalid(tx, self.user, "decision", did):
                    raise SupportError("关联来源已失效", 409)
            from app.media.context import dependency_stamp
            if stamp.get('media', []) != dependency_stamp(tx, self.user, tasks):
                raise VersionConflict()
            if 'memory' in stamp:
                from app.memory.task_recall import dependencies_current
                if not dependencies_current(tx, self.store, self.user, stamp['memory']):
                    raise SupportError('历史偏好已变化，请重新查询', 409)

    def valid(self, stamp, *, tx=None):
        try:
            self.require_current(stamp, tx=tx)
            return True
        except (SupportError, TypeError, ValueError):
            return False
