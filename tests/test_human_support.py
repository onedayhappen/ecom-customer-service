"""Human support invariants against a real transactional database, no LLM."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import json
import threading

import pytest

from app.support.store import SupportStore, SupportError, BotInterrupted
from app.support.runtime import bot_scope, call_tool, explicit_handoff


@pytest.fixture
def store(tmp_path):
    db = SupportStore(f"sqlite:///{tmp_path / 'support.db'}")
    db.ensure("session", "customer")
    for user, role in (("alice", "agent"), ("bob", "agent"), ("manager", "admin")):
        db.seed_staff(user, role)
    return db


def ticket(store):
    return store.handoff("session", "customer")["ticket"]


def claim(store, actor="alice"):
    t = ticket(store)
    store.change(t["ticket_id"], actor, t["version"], "claim")
    return t["ticket_id"]


def test_duplicate_handoff_and_cross_instance_claim(store):
    def create(_):
        other = SupportStore(store.dsn)
        return other.handoff("session", "customer")["ticket"]["ticket_id"]
    with ThreadPoolExecutor(6) as pool:
        ids = list(pool.map(create, range(12)))
    assert len(set(ids)) == 1
    def attempt(actor):
        try:
            SupportStore(store.dsn).change(ids[0], actor, 1, "claim")
            return True
        except SupportError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt, ["alice", "bob"])) == 1


def test_private_notes_never_leak_or_enter_model_context(store):
    tid = claim(store)
    store.reply(tid, "alice", "internal-only", "one", True)
    store.reply(tid, "alice", "public reply", "two")
    public = store.events("session", "customer")
    assert "internal-only" not in json.dumps(public)
    assert "internal-only" not in json.dumps(store.context("session", "customer"))
    assert "internal-only" in json.dumps(store.events("session", "alice", staff=True))
    assert public["cursor"] == store.state("session", "customer")["cursor"]


def test_role_owner_and_reassignment_checks(store):
    tid = claim(store)
    for fn in [lambda: store.state("session", "intruder"),
               lambda: store.list_tickets("customer"),
               lambda: store.detail(tid, "bob"),
               lambda: store.reply(tid, "bob", "hi", "msg")]:
        with pytest.raises(SupportError): fn()
    current = store.detail(tid, "manager")["ticket"]
    store.change(tid, "manager", current["version"], "assign", "bob")
    with pytest.raises(SupportError): store.reply(tid, "alice", "stale", "msg")
    store.reply(tid, "bob", "new owner", "msg")


def test_replies_are_idempotent_and_conflicts_rejected(store):
    tid = claim(store)
    first = store.reply(tid, "alice", "hello", "id")
    assert store.reply(tid, "alice", "hello", "id") == first
    with pytest.raises(SupportError): store.reply(tid, "alice", "changed", "id")
    with pytest.raises(SupportError): store.reply(tid, "alice", "hello", "id", True)


def test_waiting_resolved_reopen_and_explicit_resume(store):
    tid = claim(store)
    store.change(tid, "alice", 2, "waiting_customer")
    store.receive("session", "customer", "details", "m1")
    assert store.state("session", "customer")["ticket"]["status"] == "in_progress"
    version = store.detail(tid, "alice")["ticket"]["version"]
    store.change(tid, "alice", version, "resolved")
    assert store.state("session", "customer")["mode"] == "human"
    store.receive("session", "customer", "still broken", "m2")
    assert store.state("session", "customer")["ticket"]["ticket_id"] == tid
    assert store.resume("session", "customer")["mode"] == "bot"
    with pytest.raises(SupportError): store.reply(tid, "alice", "late", "m3")


def test_disabled_agent_cannot_read_or_reply_and_reopen_queues(store):
    tid = claim(store)
    store.change(tid, "alice", 2, "resolved")
    store.update_staff("manager", "alice", "agent", False)
    with pytest.raises(SupportError): store.events("session", "alice", staff=True)
    store.receive("session", "customer", "again", "m1")
    assert store.state("session", "customer")["mode"] == "handoff_pending"


def test_bot_inflight_reply_and_tools_are_fenced(store):
    with bot_scope(store, "session", "customer", 0):
        ticket(store)
        calls = []
        with pytest.raises(BotInterrupted):
            call_tool("apply_refund", {}, lambda: calls.append(True))
        with pytest.raises(BotInterrupted): store.publish_bot("session", "customer", 0, "job", "late reply")
        assert calls == []
    store.resume("session", "customer")
    with pytest.raises(BotInterrupted): store.check_bot("session", "customer", 0)


def test_inflight_effect_records_result_after_handoff(store):
    entered, finish = threading.Event(), threading.Event()
    def work():
        with bot_scope(store, "session", "customer", 0):
            def effect():
                entered.set(); finish.wait(3)
                return {"success": True}
            return call_tool("apply_refund", {}, effect)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(work)
        assert entered.wait(3)
        tid = claim(store)
        with pytest.raises(SupportError): store.resume("session", "customer")
        finish.set()
        assert future.result()["success"]
    assert store.detail(tid, "alice")["operations"][0]["status"] == "succeeded"
    assert store.resume("session", "customer")["mode"] == "bot"


def test_unknown_effect_requires_evidence_and_admin(store):
    with bot_scope(store, "session", "customer", 0):
        call_tool("apply_refund", {}, lambda: {"success": False, "error_code": "transient_failure"})
    tid = claim(store)
    with pytest.raises(SupportError): store.resume("session", "customer")
    op = store.detail(tid, "alice")["operations"][0]
    with pytest.raises(SupportError): store.reconcile(op["operation_id"], "alice", "failed", "receipt")
    store.reconcile(op["operation_id"], "manager", "failed", "下游查询确认未发生退款，工单回执123")
    assert store.resume("session", "customer")["mode"] == "bot"


def test_message_catchup_cursor_is_lossless(store):
    claim(store)
    for i in range(25): store.receive("session", "customer", str(i), str(i))
    cursor, seen = 0, []
    while True:
        data = SupportStore(store.dsn).events("session", "customer", cursor, limit=7)
        seen.extend(e["sequence"] for e in data["events"])
        if cursor == data["cursor"]: break
        cursor = data["cursor"]
    assert len(seen) == len(set(seen)) == 27


@pytest.mark.parametrize("text", ["转人工", "我要人工客服", "请帮我转人工", "人工"])
def test_explicit_handoff(text):
    assert explicit_handoff(text)


@pytest.mark.parametrize("text", ["不要转人工", "先别转人工", "人工客服几点上班", "不想找人工了"])
def test_not_handoff(text):
    assert not explicit_handoff(text)


def test_commit_failure_propagates():
    from app.db import _PooledConnection
    class Conn:
        closed = False
        def commit(self): raise RuntimeError("commit failed")
    class Pool:
        def _release(self, conn, healthy): assert healthy is False
    with pytest.raises(RuntimeError, match="commit failed"):
        with _PooledConnection(Pool(), Conn()): pass


def test_mysql_refund_commits_before_pool_return():
    from app.data.order_dao import MySqlOrderDAO
    events = []
    class Cursor:
        rowcount = 1
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, *a): pass
        def fetchone(self): return {"status": "pending", "refund_status": None}
    class Conn:
        def cursor(self): return Cursor()
        def commit(self): events.append("commit")
        def rollback(self): events.append("rollback")
    dao = MySqlOrderDAO("unused")
    dao._acquire = Conn
    assert dao.request_refund("u", "o", "pdd", "reason") == "success"
    assert events == ["commit", "rollback"]


def test_historical_ticket_stops_at_resume_boundary(store):
    tid = claim(store)
    store.reply(tid, "alice", "original reply", "reply")
    store.resume("session", "customer")
    store.receive("session", "customer", "new private topic", "later")
    new_ticket = claim(store, "bob")
    store.reply(new_ticket, "bob", "new internal note", "note", True)
    history = store.events("session", "alice", staff=True, ticket_id=tid)
    assert "original reply" in json.dumps(history)
    assert "new private topic" not in json.dumps(history)
    assert "new internal note" not in json.dumps(history)
    assert history["state"]["mode"] == "archived"
    assert store.detail(tid, "alice")["state"] == history["state"]
    with pytest.raises(SupportError):
        store.events("session", "alice", staff=True, ticket_id=new_ticket)


def test_crashed_operation_reconciliation_and_late_result(store):
    oid = store.begin_operation("session", "customer", 0, "apply_refund")
    with pytest.raises(SupportError): store.begin_operation("session", "customer", 0, "apply_refund")
    tid = claim(store)
    with pytest.raises(SupportError): store.mark_unknown(oid, "alice", "stopped")
    store.mark_unknown(oid, "manager", "worker was terminated and downstream inspected")
    with pytest.raises(SupportError): store.resume("session", "customer")
    store.reconcile(oid, "manager", "succeeded", "business receipt verified")
    store.finish_operation(oid, {"success": False, "error_code": "transient_failure"})
    assert store.detail(tid, "manager")["operations"][0]["status"] == "succeeded"
    assert store.resume("session", "customer")["mode"] == "bot"


def test_cached_replay_does_not_release_newer_job(store):
    store.receive("session", "customer", "first", "first")
    store.publish_bot("session", "customer", 0, "first", "done", {"reply": "done"})
    store.receive("session", "customer", "second", "second")
    assert store.receive("session", "customer", "first", "first")["duplicate"]
    store.release_bot("session", "customer", "first")
    with pytest.raises(SupportError): store.receive("session", "customer", "third", "third")


def test_unknown_operation_automatically_queues_human(store):
    oid = store.begin_operation("session", "customer", 0, "apply_refund")
    store.finish_operation(oid, {"success": False, "error_code": "transient_failure"})
    assert store.state("session", "customer")["mode"] == "handoff_pending"
    with pytest.raises(BotInterrupted): store.begin_operation("session", "customer", 0, "apply_refund")


def test_failed_submission_does_not_unlock_running_duplicate(store):
    store.receive("session", "customer", "confirm", "j", request_fingerprint="original")
    store.start_bot("session", "customer", 0, "j")
    with pytest.raises(SupportError):
        store.receive("session", "customer", "confirm", "j", request_fingerprint="changed")
    store.release_admission("session", "customer", "j")
    with pytest.raises(SupportError): store.receive("session", "customer", "next", "new")
    store.release_bot("session", "customer", "j")
    with pytest.raises(SupportError):
        store.receive("session", "customer", "confirm", "j", request_fingerprint="original")
    # A failed replay did not leave an orphan reservation.
    store.receive("session", "customer", "next", "new")


def test_full_history_is_independent_of_model_window(store):
    tid = claim(store)
    for i in range(45): store.receive("session", "customer", str(i), str(i))
    store.reply(tid, "alice", "internal only", "private", True)
    assert len(store.context("session", "customer")) == 40
    assert len(store.public_history("session", "customer")) == 45
    assert store.public_history("session", "customer")[0]["content"] == "0"
