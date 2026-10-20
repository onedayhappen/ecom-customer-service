"""Transactional support store. PostgreSQL in deployment, SQLite for local tests.

Every conversation mutation locks its control row. Messages and lifecycle events
share a durable, per-conversation sequence, allocated under that same lock.
No network/LLM call is performed while a transaction is open.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from app.tasks.service import SCHEMA as TASK_SCHEMA, TaskService
from app.runtime.outbox import SCHEMA as JOB_SCHEMA, DurableJobs
from app.runtime.notifications import SCHEMA as NOTIFICATION_SCHEMA, enqueue as enqueue_notification
from app.memory.case_postgres import SCHEMA as CASE_SCHEMA
from app.runtime.maintenance import SCHEMA as MAINTENANCE_SCHEMA
from app.session.short_term import SCHEMA as CONTEXT_SCHEMA
from app.memory.runtime import SCHEMA as MEMORY_RUNTIME_SCHEMA


class SupportError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class BotInterrupted(SupportError):
    def __init__(self):
        super().__init__("会话控制状态已变化，旧 AI 任务已停止")


def now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS support_staff (
        user_id TEXT PRIMARY KEY, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS support_staff_audit (
        audit_id TEXT PRIMARY KEY, actor TEXT NOT NULL, user_id TEXT NOT NULL,
        role TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS support_conversations (
        session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'bot', control_version INTEGER NOT NULL DEFAULT 0,
        sequence INTEGER NOT NULL DEFAULT 0, ticket_id TEXT, bot_job_id TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS support_tickets (
        ticket_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        status TEXT NOT NULL, assignee TEXT, reason TEXT NOT NULL,
        summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, assigned_at TEXT, first_reply_at TEXT, resolved_at TEXT)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS support_one_active_ticket
        ON support_tickets(session_id) WHERE status IN ('queued','in_progress','waiting_customer')""",
    """CREATE INDEX IF NOT EXISTS support_ticket_queue
        ON support_tickets(status, created_at)""",
    """CREATE TABLE IF NOT EXISTS support_events (
        session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        event_key TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
        visibility TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(session_id, sequence), UNIQUE(session_id, event_key))""",
    """CREATE TABLE IF NOT EXISTS support_operations (
        operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        control_version INTEGER NOT NULL, tool TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, result TEXT)""",
    """CREATE INDEX IF NOT EXISTS support_message_history ON support_events(session_id,sequence DESC)
        WHERE kind='message'""",
    """CREATE INDEX IF NOT EXISTS support_operation_session ON support_operations(session_id)""",
    """CREATE TABLE IF NOT EXISTS support_bot_results (
        job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, response TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS support_bot_runs (
        job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
) + TASK_SCHEMA + JOB_SCHEMA + NOTIFICATION_SCHEMA + CASE_SCHEMA + MAINTENANCE_SCHEMA + CONTEXT_SCHEMA + MEMORY_RUNTIME_SCHEMA
SCHEMA_VERSION = 11


class Tx:
    def __init__(self, connection, postgres, read_only=False):
        self.connection, self.postgres, self.read_only = connection, postgres, read_only

    def execute(self, sql, args=()):
        return self.connection.execute(sql.replace("?", "%s") if self.postgres else sql, args)

    def one(self, sql, args=()):
        row = self.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.execute(sql, args).fetchall()]


class SupportStore:
    def __init__(self, dsn):
        self.dsn = dsn
        self.postgres = not dsn.startswith("sqlite:///")
        self._initialized = False
        self._schema_lock = threading.Lock()
        self.tasks = TaskService(self)
        self.jobs = DurableJobs(self)

    @contextmanager
    def _connection(self, read_only=False):
        if self.postgres:
            from app.db import get_connection_pool
            from psycopg.rows import dict_row
            with get_connection_pool(self.dsn).connection() as conn:
                previous = conn.row_factory
                conn.row_factory = dict_row
                try:
                    if read_only:
                        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    yield Tx(conn, True, read_only)
                finally:
                    conn.row_factory = previous
        else:
            path = self.dsn[len("sqlite:///"):]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute('BEGIN' if read_only else 'BEGIN IMMEDIATE')
                yield Tx(conn, False, read_only)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self):
        with self._schema_lock:
            if not self._initialized:
                with self._connection() as tx:
                    if self.postgres:
                        tx.execute("SELECT pg_advisory_xact_lock(82344710)")
                        exists = tx.one("SELECT to_regclass('support_schema_versions') AS name")['name']
                    else:
                        exists = tx.one("SELECT name FROM sqlite_master WHERE type='table' AND name='support_schema_versions'")
                    applied = tx.one('SELECT MAX(version) AS version FROM support_schema_versions')['version'] if exists else 0
                    if not applied or applied < SCHEMA_VERSION:
                        for sql in SCHEMA:
                            tx.execute(sql)
                        if applied and applied < 4:
                            field = "payload::jsonb->>'task_id'" if tx.postgres else "json_extract(payload,'$.task_id')"
                            tx.execute(f"""INSERT INTO support_task_messages(task_id,session_id,sequence)
                                SELECT {field},session_id,sequence FROM support_events
                                WHERE kind='message' AND {field} IS NOT NULL ON CONFLICT DO NOTHING""")
                        from app.tasks.migrations import migrate_task_storage
                        if (applied or 0) < 10:
                            migrate_task_storage(tx)
                        if (applied or 0) < 11:
                            from app.tasks.migrations import migrate_message_intake
                            migrate_message_intake(tx)
                        tx.execute('CREATE TABLE IF NOT EXISTS support_schema_versions (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
                        tx.execute('INSERT INTO support_schema_versions VALUES (?,?) ON CONFLICT(version) DO NOTHING', (SCHEMA_VERSION, now()))
                self._initialized = True

    @contextmanager
    def transaction(self, read_only=False):
        self.initialize()
        with self._connection(read_only=read_only) as tx:
            yield tx

    def _control(self, tx, sid, user=None):
        row = tx.one("SELECT * FROM support_conversations WHERE session_id=?" +
                     (" FOR UPDATE" if self.postgres and not tx.read_only else ""), (sid,))
        if not row or (user is not None and row["user_id"] != user):
            raise SupportError("会话不存在", 404)
        return row

    def _event(self, tx, control, kind, actor, payload, *, private=False, key=None):
        key = key or str(uuid4())
        old = tx.one("SELECT * FROM support_events WHERE session_id=? AND event_key=?",
                     (control["session_id"], key))
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if old:
            if old["payload"] != encoded or old["actor"] != actor or old["kind"] != kind:
                raise SupportError("消息 ID 已用于不同内容")
            return old["sequence"]
        control["sequence"] += 1
        tx.execute("UPDATE support_conversations SET sequence=? WHERE session_id=?",
                   (control["sequence"], control["session_id"]))
        tx.execute("INSERT INTO support_events VALUES (?,?,?,?,?,?,?,?)",
                   (control["session_id"], control["sequence"], key, kind, actor,
                    "internal" if private else "public", encoded, now()))
        enqueue_notification(tx, control['session_id'], control['sequence'])
        if kind == 'message' and payload.get('task_id'):
            tx.execute('INSERT INTO support_task_messages VALUES (?,?,?) ON CONFLICT DO NOTHING',
                       (payload['task_id'], control['session_id'], control['sequence']))
        if kind == 'message' and not private:
            from app.config.settings import settings
            if settings.short_term_memory_enabled:
                from app.session.short_term import enqueue_context_job
                enqueue_context_job(tx, control, payload.get('task_id'))
            if settings.memory_enabled and payload.get('role') == 'user':
                from app.memory.runtime import enqueue_memory_job
                enqueue_memory_job(tx, control, payload)
        return control["sequence"]

    def ensure(self, sid, user, history=()):
        with self.transaction() as tx:
            inserted = tx.execute("""INSERT INTO support_conversations
                (session_id,user_id,created_at) VALUES (?,?,?) ON CONFLICT(session_id) DO NOTHING""",
                (sid, user, now())).rowcount
            c = self._control(tx, sid, user)
            if inserted:
                for index, message in enumerate(history):
                    if message.get("role") in {"user", "assistant"}:
                        self._event(tx, c, "message", user if message["role"] == "user" else "bot",
                                    {"role": message["role"], "content": message["content"]},
                                    key=f"legacy:{index}")
            return c

    def role(self, user):
        with self.transaction(read_only=True) as tx:
            return self._role(tx, user)

    def _role(self, tx, user):
        row = tx.one("SELECT role FROM support_staff WHERE user_id=? AND enabled=1", (user,))
        return row["role"] if row else "customer"

    def seed_staff(self, user, role):
        if role not in {"agent", "admin"}:
            raise ValueError("role must be agent or admin")
        with self.transaction() as tx:
            tx.execute("""INSERT INTO support_staff(user_id,role) VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,enabled=1""", (user, role))
            tx.execute("INSERT INTO support_staff_audit VALUES (?,?,?,?,?,?)", (str(uuid4()), "local_cli", user, role, 1, now()))

    def staff(self, actor):
        with self.transaction(read_only=True) as tx:
            if self._role(tx, actor) == "customer":
                raise SupportError("需要客服权限", 403)
            return tx.all("SELECT user_id,role,enabled FROM support_staff ORDER BY user_id")

    def update_staff(self, actor, user, role, enabled):
        with self.transaction() as tx:
            if self._role(tx, actor) != "admin":
                raise SupportError("需要管理员权限", 403)
            if user == actor and (not enabled or role != "admin"):
                raise SupportError("不能移除自己的管理员权限")
            tx.execute("""INSERT INTO support_staff VALUES (?,?,?) ON CONFLICT(user_id)
                DO UPDATE SET role=excluded.role,enabled=excluded.enabled""", (user, role, int(enabled)))
            tx.execute("INSERT INTO support_staff_audit VALUES (?,?,?,?,?,?)", (str(uuid4()), actor, user, role, int(enabled), now()))

    def _ticket(self, tx, c):
        return tx.one("SELECT * FROM support_tickets WHERE ticket_id=?", (c["ticket_id"],)) if c["ticket_id"] else None

    def _handoff(self, tx, c, reason, actor):
        ticket = self._ticket(tx, c)
        if ticket and c["mode"] != "bot":
            return ticket
        tid, timestamp = str(uuid4()), now()
        recent = tx.all("""SELECT payload FROM support_events WHERE session_id=? AND kind='message'
            AND visibility='public' ORDER BY sequence DESC LIMIT 12""", (c["session_id"],))
        summary = "\n".join(json.loads(r["payload"])["content"][:400] for r in reversed(recent))
        tx.execute("""INSERT INTO support_tickets
            (ticket_id,session_id,status,reason,summary,created_at) VALUES (?,?,'queued',?,?,?)""",
            (tid, c["session_id"], reason, summary or "用户请求人工协助", timestamp))
        tx.execute("""UPDATE support_conversations SET mode='handoff_pending',
            control_version=control_version+1,ticket_id=?,bot_job_id=NULL WHERE session_id=?""", (tid, c["session_id"]))
        c.update(mode="handoff_pending", ticket_id=tid, control_version=c["control_version"] + 1)
        self.jobs.supersede(tx, c)
        self.tasks.freeze(tx, c, actor)
        self._event(tx, c, "support_state", actor,
                    {"mode": c["mode"], "ticket_id": tid, "status": "queued", "reason": reason})
        return self._ticket(tx, c)

    def handoff(self, sid, user, reason="customer_requested"):
        with self.transaction() as tx:
            c = self._control(tx, sid, user)
            self._handoff(tx, c, reason, user)
            return self._state(tx, c)

    def _state(self, tx, c):
        ticket = self._ticket(tx, c)
        public_ticket = {k: ticket[k] for k in ("ticket_id", "status", "assignee", "created_at", "version")} if ticket else None
        return {"session_id": c["session_id"], "mode": c["mode"], "control_version": c["control_version"],
                "cursor": c["sequence"], "ticket": public_ticket}

    def state(self, sid, user):
        with self.transaction(read_only=True) as tx:
            return self._state(tx, self._control(tx, sid, user))

    def operation_status(self, sid, user, operation_id):
        """Read a receipt only within the authenticated conversation."""
        with self.transaction(read_only=True) as tx:
            self._control(tx, sid, user)
            op = tx.one('SELECT tool,status,result FROM support_operations '
                        'WHERE session_id=? AND operation_id=?', (sid, operation_id))
            if not op:
                return {'status': 'unknown', 'note': '未找到当前会话的操作记录，不能据此重试'}
            return {'action': op['tool'], 'status': op['status'],
                    'result': json.loads(op['result']) if op['result'] else None}

    def resume(self, sid, user):
        with self.transaction() as tx:
            c = self._control(tx, sid, user)
            if c["mode"] == "bot":
                return self._state(tx, c)
            # Unknown or in-flight effects must be reconciled before enabling new bot mutations.
            if tx.one("SELECT operation_id FROM support_operations WHERE session_id=? AND status IN ('running','unknown') LIMIT 1", (sid,)):
                raise SupportError("仍有执行中或结果待核实的业务操作，请先由客服核实")
            ticket = self._ticket(tx, c)
            target = "cancelled" if ticket["status"] == "queued" else "resolved"
            tx.execute("UPDATE support_tickets SET status=?,resolved_at=?,version=version+1 WHERE ticket_id=?",
                       (target, now(), ticket["ticket_id"]))
            tx.execute("UPDATE support_conversations SET mode='bot',control_version=control_version+1,ticket_id=NULL WHERE session_id=?", (sid,))
            c.update(mode="bot", ticket_id=None, control_version=c["control_version"] + 1)
            self._event(tx, c, "support_state", user, {"mode": "bot", "status": target},
                        key=f"ticket-closed:{ticket['ticket_id']}")
            self.tasks.archive_ticket(tx, c, ticket['ticket_id'], c['sequence'])
            return self._state(tx, c)

    def receive(self, sid, user, content, message_id, *, explicit_handoff=False, request_fingerprint=None, task_input=None, delivery=None):
        """Append once; return routing version. Delivery to the bot queue is separate."""
        with self.transaction() as tx:
            c = self._control(tx, sid, user)
            if tx.one("SELECT session_id FROM message_intake_sessions WHERE session_id=?", (sid,)):
                raise SupportError("该会话使用持续消息入口，请从该入口继续")
            if tx.one("SELECT job_id FROM support_bot_runs WHERE job_id=? AND session_id<>?", (message_id, sid)):
                raise SupportError("消息 ID 已用于其他会话")
            old = tx.one("SELECT payload FROM support_events WHERE session_id=? AND event_key=?", (sid, f"user:{message_id}"))
            if old:
                data = json.loads(old["payload"])
                if data["content"] != content or data.get("request_fingerprint") != request_fingerprint:
                    raise SupportError("消息 ID 已用于不同内容")
                if c["mode"] == "bot" and c["control_version"] == data["route_version"]:
                    cached = tx.one("SELECT job_id FROM support_bot_results WHERE job_id=? AND session_id=?", (message_id, sid))
                    if not cached:
                        if c["bot_job_id"] not in {None, message_id}:
                            raise SupportError("会话已有其他任务，不能重放旧请求")
                        if c["bot_job_id"] is None and tx.one("SELECT job_id FROM support_bot_runs WHERE job_id=?", (message_id,)):
                            raise SupportError("该任务此前已启动但未返回结果，请转人工核实")
                        tx.execute("UPDATE support_conversations SET bot_job_id=? WHERE session_id=?", (message_id, sid))
                return {**self._state(tx, c), "route_version": data["route_version"], "duplicate": True}
            task_route = {}
            if task_input is not None and not explicit_handoff:
                task_route = self.tasks.receive(tx, c, content, user, message_id, task_input)
            if c["mode"] == "bot" and c["bot_job_id"] and not explicit_handoff and not task_route.get('background_control'):
                raise SupportError("该会话已有 AI 任务，请等待完成或转人工")
            self._event(tx, c, "message", user,
                        {"role": "user", "content": content, "route_version": c["control_version"],
                         "request_fingerprint": request_fingerprint, 'task_id': task_route.get('task_id')}, key=f"user:{message_id}")
            if explicit_handoff:
                self._handoff(tx, c, "customer_requested", user)
            elif c["mode"] == "bot" and not task_route.get('background_control'):
                tx.execute("UPDATE support_conversations SET bot_job_id=? WHERE session_id=?", (message_id, sid))
                if delivery and not task_route.get('reply'):
                    self.jobs.admit(tx, c, message_id, content, (task_input or {}).get('confirmation_token'), **delivery)
            if task_route.get('background_control'):
                result = {'session_id': sid, 'reply': task_route['reply'], 'intent': 'answer_question',
                          'confidence': 1, 'requires_human': False, 'follow_up_question': None,
                          'control_version': c['control_version']}
                self._event(tx, c, 'message', 'bot', {'role': 'assistant', 'content': result['reply'],
                            'task_id': task_route.get('control_task_id')}, key=f'bot:{message_id}')
                tx.execute('INSERT INTO support_bot_results VALUES (?,?,?)', (message_id, sid, json.dumps(result, ensure_ascii=False)))
            ticket = self._ticket(tx, c)
            if ticket and c["mode"] == "human" and ticket["status"] in {"waiting_customer", "resolved"}:
                enabled = ticket["assignee"] and self._role(tx, ticket["assignee"]) != "customer"
                target = "in_progress" if enabled else "queued"
                tx.execute("UPDATE support_tickets SET status=?,resolved_at=NULL,version=version+1 WHERE ticket_id=?", (target, ticket["ticket_id"]))
                if not enabled:
                    tx.execute("UPDATE support_tickets SET assignee=NULL WHERE ticket_id=?", (ticket["ticket_id"],))
                    tx.execute("UPDATE support_conversations SET mode='handoff_pending' WHERE session_id=?", (sid,))
                    c["mode"] = "handoff_pending"
                self._event(tx, c, "support_state", user, {"status": target, "mode": c["mode"]})
            return {**self._state(tx, c), "route_version": c["control_version"], "duplicate": False}

    def _ticket_view(self, tx, c, actor, tid):
        ticket = tx.one("SELECT * FROM support_tickets WHERE ticket_id=? AND session_id=?", (tid, c["session_id"]))
        role = self._role(tx, actor)
        if not ticket or role == "customer" or (role != "admin" and ticket["assignee"] != actor):
            raise SupportError("请先领取工单", 403)
        if c["ticket_id"] == tid:
            return ticket, c["sequence"], self._state(tx, c), None
        closed = tx.one("SELECT sequence,created_at FROM support_events WHERE session_id=? AND event_key=?",
                        (c["session_id"], f"ticket-closed:{tid}"))
        if not closed:
            raise SupportError("工单历史边界缺失，请联系管理员", 409)
        state = {"session_id": c["session_id"], "mode": "archived", "cursor": closed["sequence"],
                 "ticket": {k: ticket[k] for k in ("ticket_id", "status", "assignee", "created_at", "version")}}
        return ticket, closed["sequence"], state, closed["created_at"]

    def events(self, sid, user, after=0, limit=100, *, staff=False, ticket_id=None):
        with self.transaction(read_only=True) as tx:
            c = self._control(tx, sid, None if staff else user)
            ceiling, state = c["sequence"], self._state(tx, c)
            if staff:
                if ticket_id:
                    _, ceiling, state, _ = self._ticket_view(tx, c, user, ticket_id)
                else:
                    self._authorize_ticket(tx, c, user)
            rows = tx.all("""SELECT * FROM support_events WHERE session_id=? AND sequence>? AND sequence<=?
                """ + ("" if staff else "AND visibility='public' ") + "ORDER BY sequence LIMIT ?", (sid, after, ceiling, limit))
            for row in rows:
                row["payload"] = json.loads(row["payload"])
            # Advance only through returned events. Internal trailing events can be skipped safely.
            cursor = rows[-1]["sequence"] if len(rows) == limit else ceiling
            return {"events": rows, "cursor": cursor, "state": state}

    def context(self, sid, user, *, query='', exclude_latest_user=False):
        from app.config.settings import settings
        if settings.short_term_memory_enabled:
            from app.session.short_term import ContextService
            return ContextService(self).build(sid, user, query=query, exclude_latest_user=exclude_latest_user)
        with self.transaction(read_only=True) as tx:
            self._control(tx, sid, user)
            rows = tx.all("""SELECT payload FROM support_events WHERE session_id=? AND kind='message'
                AND visibility='public' ORDER BY sequence DESC LIMIT 40""", (sid,))
            messages = [{"role": p["role"] if p["role"] == "user" else "assistant", "content": p["content"]}
                        for p in (json.loads(r["payload"]) for r in reversed(rows))]
            return messages[:-1] if exclude_latest_user and messages and messages[-1]['role'] == 'user' else messages

    def transcript(self, sid, user, before=None, limit=50, *, staff=False, ticket_id=None):
        """Bootstrap from a bounded snapshot, then seek backwards through messages.

        Snapshot state, tasks and the SSE cursor share one read transaction;
        changes committed afterwards are recovered by incremental event delivery.
        """
        if not 1 <= limit <= 100 or before is not None and before < 1:
            raise SupportError('历史分页参数不合法', 422)
        with self.transaction(read_only=True) as tx:
            c = self._control(tx, sid, None if staff else user)
            ceiling, state = c['sequence'], self._state(tx, c)
            if staff:
                if ticket_id:
                    _, ceiling, state, _ = self._ticket_view(tx, c, user, ticket_id)
                else:
                    self._authorize_ticket(tx, c, user)
            upper = min(before, ceiling + 1) if before is not None else ceiling + 1
            rows = tx.all("SELECT * FROM support_events WHERE session_id=? AND sequence<? AND kind='message' " +
                          ('' if staff else "AND visibility='public' ") + 'ORDER BY sequence DESC LIMIT ?',
                          (sid, upper, limit + 1))
            page = rows[:limit]
            next_before = page[-1]['sequence'] if len(rows) > limit else None
            for row in page:
                row['payload'] = json.loads(row['payload'])
            snapshot = {}
            if before is None:
                snapshot = self.tasks.ticket_snapshot(tx, c, ticket_id, ceiling) if state['mode'] == 'archived' else self.tasks.snapshot(tx, c)
            return {'events': list(reversed(page)), 'cursor': ceiling, 'state': state,
                    'next_before': next_before, **snapshot}

    def public_history(self, sid, user):
        """Compatibility endpoint returns full history, not the bounded LLM context."""
        with self.transaction(read_only=True) as tx:
            self._control(tx, sid, user)
            rows = tx.all("SELECT payload FROM support_events WHERE session_id=? AND kind='message' AND visibility='public' ORDER BY sequence", (sid,))
            return [{"role": p["role"], "content": p["content"]} for p in (json.loads(r["payload"]) for r in rows)]

    def _authorize_ticket(self, tx, c, actor, *, queue_ok=False):
        role = self._role(tx, actor)
        ticket = self._ticket(tx, c)
        if not ticket or role == "customer" or (role != "admin" and ticket["assignee"] != actor and not (queue_ok and ticket["status"] == "queued")):
            raise SupportError("无权处理该工单", 403)
        return ticket

    def list_tickets(self, actor, view="queue", offset=0, limit=30):
        with self.transaction(read_only=True) as tx:
            role = self._role(tx, actor)
            if role == "customer" or (view == "all" and role != "admin"):
                raise SupportError("无权查看工单", 403)
            where, args = ("t.status='queued'", []) if view == "queue" else (("t.assignee=?", [actor]) if view == "mine" else ("1=1", []))
            # Queue preview deliberately excludes transcript, summary and user identity.
            return tx.all("SELECT t.ticket_id,t.status,t.reason,t.assignee,t.version,t.created_at,t.assigned_at,t.first_reply_at,t.resolved_at FROM support_tickets t WHERE " + where +
                          " ORDER BY t.created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset))

    def detail(self, tid, actor):
        with self.transaction(read_only=True) as tx:
            t = tx.one("SELECT session_id FROM support_tickets WHERE ticket_id=?", (tid,))
            if not t:
                raise SupportError("工单不存在", 404)
            c = self._control(tx, t["session_id"])
            ticket, ceiling, state, closed_at = self._ticket_view(tx, c, actor, tid)
            operations = tx.all("SELECT * FROM support_operations WHERE session_id=? AND created_at<=? ORDER BY created_at",
                                (c["session_id"], closed_at or now()))
            evidence = tx.all("SELECT payload,created_at FROM support_events WHERE session_id=? AND sequence<=? AND kind='tool_evidence' ORDER BY sequence DESC LIMIT 20", (c["session_id"], ceiling))
            return {"ticket": ticket, "state": state, "operations": operations,
                    "tasks": self.tasks.ticket_snapshot(tx, c, tid, ceiling)["tasks"],
                    "evidence": [{**json.loads(e["payload"]), "created_at": e["created_at"]} for e in evidence]}

    def change(self, tid, actor, version, action, assignee=None):
        with self.transaction() as tx:
            ticket = tx.one("SELECT * FROM support_tickets WHERE ticket_id=?", (tid,))
            if not ticket:
                raise SupportError("工单不存在", 404)
            c = self._control(tx, ticket["session_id"])
            if c["ticket_id"] != tid or c["mode"] == "bot":
                raise SupportError("工单已退出人工处理")
            ticket = self._authorize_ticket(tx, c, actor, queue_ok=action == "claim")
            if ticket["version"] != version:
                raise SupportError("工单已更新，请刷新后重试")
            timestamp = now()
            target, owner = ticket["status"], ticket["assignee"]
            if action == "claim":
                if target != "queued":
                    raise SupportError("工单已被领取")
                target, owner = "in_progress", actor
            elif action == "assign":
                if self._role(tx, actor) != "admin":
                    raise SupportError("需要管理员权限", 403)
                if self._role(tx, assignee) == "customer":
                    raise SupportError("目标客服不存在或已停用", 422)
                target, owner = "in_progress", assignee
            elif action in {"waiting_customer", "resolved"}:
                if target not in {"in_progress", "waiting_customer"}:
                    raise SupportError("当前状态不能执行该操作")
                target = action
            elif action == "requeue":
                if target not in {"in_progress", "waiting_customer"}:
                    raise SupportError("当前状态不能退回队列")
                target, owner = "queued", None
            else:
                raise SupportError("不支持的工单操作", 422)
            tx.execute("""UPDATE support_tickets SET status=?,assignee=?,version=version+1,
                assigned_at=?,resolved_at=? WHERE ticket_id=?""",
                (target, owner, (timestamp if owner else None) if action in {"claim", "assign", "requeue"} else ticket["assigned_at"],
                 timestamp if target == "resolved" else None, tid))
            c["mode"] = "handoff_pending" if target == "queued" else "human"
            tx.execute("UPDATE support_conversations SET mode=? WHERE session_id=?", (c["mode"], c["session_id"]))
            self._event(tx, c, "support_state", actor, {"mode": c["mode"], "status": target, "assignee": owner, "action": action})
            return self._state(tx, c)

    def reply(self, tid, actor, content, message_id, private=False):
        with self.transaction() as tx:
            t = tx.one("SELECT session_id FROM support_tickets WHERE ticket_id=?", (tid,))
            if not t:
                raise SupportError("工单不存在", 404)
            c = self._control(tx, t["session_id"])
            ticket = self._authorize_ticket(tx, c, actor)
            if c["ticket_id"] != tid or c["mode"] != "human" or ticket["status"] not in {"in_progress", "waiting_customer"}:
                raise SupportError("工单当前不能回复")
            seq = self._event(tx, c, "message", actor,
                              {"role": "agent", "content": content, "ticket_id": tid, "internal": private},
                              private=private, key=f"staff:{actor}:{message_id}")
            if not private:
                tx.execute("UPDATE support_tickets SET first_reply_at=COALESCE(first_reply_at,?) WHERE ticket_id=?", (now(), tid))
            return {"sequence": seq}

    def check_bot(self, sid, user, version):
        with self.transaction(read_only=True) as tx:
            self._check(tx, sid, user, version)

    def _check(self, tx, sid, user, version):
        c = self._control(tx, sid, user)
        if c["mode"] != "bot" or c["control_version"] != version:
            raise BotInterrupted()
        return c

    def publish_bot(self, sid, user, version, job_id, content, response=None):
        with self.transaction() as tx:
            c = self._check(tx, sid, user, version)
            if response is not None:
                self.tasks.finish(tx, c, job_id, response)
                self.jobs.completed(tx, job_id, response)
            self._event(tx, c, "message", "bot", {"role": "assistant", "content": content,
                        'task_id': self.tasks.message_task_id(tx, job_id)}, key=f"bot:{job_id}")
            if response is not None:
                tx.execute("INSERT INTO support_bot_results VALUES (?,?,?) ON CONFLICT(job_id) DO NOTHING",
                           (job_id, sid, json.dumps(response, ensure_ascii=False)))
            tx.execute("UPDATE support_conversations SET bot_job_id=NULL WHERE session_id=? AND bot_job_id=?", (sid, job_id))

    def start_bot(self, sid, user, version, job_id):
        with self.transaction() as tx:
            c = self._check(tx, sid, user, version)
            if c["bot_job_id"] not in {None, job_id}:
                raise SupportError("会话已有其他任务")
            inserted = tx.execute("INSERT INTO support_bot_runs VALUES (?,?,?) ON CONFLICT(job_id) DO NOTHING",
                                  (job_id, sid, now())).rowcount
            if not inserted:
                raise SupportError("该任务已启动过，请查看会话结果或转人工核实，不能重复执行")
            tx.execute("UPDATE support_conversations SET bot_job_id=? WHERE session_id=?", (job_id, sid))
            self.jobs.started(tx, c, job_id)
            self.tasks.started(tx, c, job_id)

    def release_bot(self, sid, user, job_id):
        with self.transaction() as tx:
            c = self._control(tx, sid, user)
            if c['bot_job_id'] == job_id:
                self.tasks.failed(tx, c, job_id)
            tx.execute("UPDATE support_conversations SET bot_job_id=NULL WHERE session_id=? AND bot_job_id=?", (sid, job_id))

    def release_admission(self, sid, user, job_id):
        """An uncertain/rejected queue submission must not unlock an active worker."""
        with self.transaction() as tx:
            self._control(tx, sid, user)
            if not tx.one("SELECT job_id FROM support_bot_runs WHERE job_id=?", (job_id,)):
                tx.execute("UPDATE support_conversations SET bot_job_id=NULL WHERE session_id=? AND bot_job_id=?", (sid, job_id))

    def failed_job(self, sid, user, job_id, interrupted=False):
        with self.transaction() as tx:
            c = self._control(tx, sid, user)
            if c["bot_job_id"] != job_id:
                return
            if interrupted:
                self._handoff(tx, c, "business_result_unknown", "system")
            else:
                self.tasks.failed(tx, c, job_id)
            tx.execute("UPDATE support_conversations SET bot_job_id=NULL WHERE session_id=?", (sid,))

    def evidence(self, sid, user, tool, arguments, result):
        from app.observability.audit import mask_sensitive
        with self.transaction() as tx:
            c = self._control(tx, sid, user)
            self._event(tx, c, "tool_evidence", "bot", mask_sensitive({"tool": tool, "arguments": arguments, "result": result}), private=True)

    def bot_result(self, sid, user, job_id):
        with self.transaction(read_only=True) as tx:
            self._control(tx, sid, user)
            row = tx.one("SELECT response FROM support_bot_results WHERE job_id=? AND session_id=?", (job_id, sid))
            return json.loads(row["response"]) if row else None

    def begin_operation(self, sid, user, version, tool):
        with self.transaction() as tx:
            c = self._check(tx, sid, user, version)
            if tx.one("SELECT operation_id FROM support_operations WHERE session_id=? AND status IN ('running','unknown') LIMIT 1", (sid,)):
                raise SupportError("已有业务操作执行中或结果待核实，请转人工处理")
            oid = str(uuid4())
            tx.execute("INSERT INTO support_operations VALUES (?,?,?,?,?,?,?)", (oid, sid, version, tool, "running", now(), None))
            self._event(tx, c, "operation", "bot", {"operation_id": oid, "tool": tool, "status": "running"}, private=True)
            return oid

    def finish_operation(self, oid, result):
        if result.get("success") is True:
            status = "succeeded"
        elif result.get("error_code") in {"business_rejection", "validation_error", "evidence_unavailable", "confirmation_invalid"}:
            status = "failed"
        else:
            status = "unknown"
        with self.transaction() as tx:
            op = tx.one("SELECT * FROM support_operations WHERE operation_id=?", (oid,))
            c = self._control(tx, op["session_id"])
            updated = tx.execute("UPDATE support_operations SET status=?,result=? WHERE operation_id=? AND status IN ('running','unknown')",
                                 (status, json.dumps(result, ensure_ascii=False), oid)).rowcount
            self._event(tx, c, "operation" if updated else "operation_late_result", "bot",
                        {"operation_id": oid, "status": status, "result": result}, private=True)
            if updated and status == "unknown" and c["mode"] == "bot":
                self._handoff(tx, c, "business_result_unknown", "system")

    def mark_unknown(self, oid, actor, evidence):
        """Operator attests the old executor stopped; never automatically retry it."""
        with self.transaction() as tx:
            if self._role(tx, actor) != "admin":
                raise SupportError("需要管理员权限", 403)
            op = tx.one("SELECT * FROM support_operations WHERE operation_id=?", (oid,))
            if not op:
                raise SupportError("操作不存在", 404)
            c = self._control(tx, op["session_id"])
            if c["mode"] == "bot":
                raise SupportError("请先转人工，停止该会话机器人执行")
            changed = tx.execute("UPDATE support_operations SET status='unknown' WHERE operation_id=? AND status='running'", (oid,)).rowcount
            if not changed:
                raise SupportError("操作状态已变化，请刷新")
            self._event(tx, c, "executor_stopped", actor, {"operation_id": oid, "evidence": evidence}, private=True)

    def reconcile(self, oid, actor, status, evidence):
        with self.transaction() as tx:
            if self._role(tx, actor) != "admin":
                raise SupportError("需要管理员核实", 403)
            op = tx.one("SELECT * FROM support_operations WHERE operation_id=?", (oid,))
            if not op or op["status"] != "unknown":
                raise SupportError("只能核实结果未知的操作；执行中的任务须先确认已终止")
            c = self._control(tx, op["session_id"])
            changed = tx.execute("UPDATE support_operations SET status=?,result=? WHERE operation_id=? AND status='unknown'",
                                 (status, json.dumps({"manual_evidence": evidence, "actor": actor}), oid)).rowcount
            if not changed:
                raise SupportError("操作状态已变化，请刷新")
            self._event(tx, c, "operation_reconciled", actor, {"operation_id": oid, "status": status, "evidence": evidence}, private=True)
