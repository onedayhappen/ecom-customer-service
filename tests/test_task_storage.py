"""Storage contracts against SQLite and opt-in, isolated real PostgreSQL."""
import json
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from uuid import uuid4

import pytest

from app.support.store import SCHEMA, SCHEMA_VERSION, SupportError, SupportStore
from app.tasks.repository import TaskRepository, VersionConflict


DSN = "postgresql://test_runner:isolated_test_only@127.0.0.1:25432/ecom_concurrency_test"
SOURCE = dict(kind="message", session_id="s1", sequence=1)


@pytest.fixture(params=["sqlite", "postgres"])
def empty_store(request, tmp_path, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "short_term_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_enabled", False)
    if request.param == "sqlite":
        yield SupportStore(f"sqlite:///{tmp_path / 'tasks.db'}")
        return
    if os.environ.get("ECOM_INTEGRATION") != "1":
        pytest.skip("dedicated PostgreSQL environment only")
    import psycopg
    from psycopg import sql
    from app.db import get_connection_pool
    schema = "task_storage_" + uuid4().hex
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == "ecom_concurrency_test"
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = SupportStore(DSN + "?options=-csearch_path%3D" + schema)
    try:
        yield store
    finally:
        get_connection_pool(store.dsn).close()
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def message(store, session_id, user, text="给这件耳机退货", key=None):
    with store.transaction() as tx:
        c = store._control(tx, session_id, user)
        seq = store._event(tx, c, "message", user, {"role": "user", "content": text}, key=key)
    return dict(kind="message", session_id=session_id, sequence=seq)


@pytest.fixture
def store(empty_store):
    for sid, user in [("s1", "alice"), ("s2", "alice"), ("other", "bob")]:
        empty_store.ensure(sid, user)
        message(empty_store, sid, user)
    return empty_store


def create(store, **kwargs):
    args = dict(change_id="create", source=SOURCE, goal="退耳机", details={"order_id": "ORD-A", "quantity": 1})
    args.update(kwargs)
    return TaskRepository(store, "alice").create("s1", **args)


def update(store, record, **kwargs):
    args = dict(expected_version=record.version, change_id="update", source=SOURCE,
                patch={"details": {"order_id": "ORD-A", "quantity": 2}, "waiting_for": "确认数量"})
    args.update(kwargs)
    return TaskRepository(store, "alice").update(record.task_id, "s1", **args)


def test_multiple_matters_and_cross_session_restart(store):
    refund = create(store)
    invoice = create(store, change_id="invoice", goal="开发票")
    assert refund.task_id != invoice.task_id
    repo = TaskRepository(SupportStore(store.dsn), "alice")
    assert repo.get(refund.task_id, session_id="s2") == refund
    result = repo.update(refund.task_id, "s2", expected_version=1, change_id="continue",
                         source={"kind": "message", "session_id": "s2", "sequence": 1}, patch={"status": "waiting_user"})
    assert result.version == 2 and result.status == "waiting_user"
    assert {r.task_id for r in repo.list(session_id="s1")} == {refund.task_id, invoice.task_id}
    assert [r.task_id for r in repo.list(session_id="s2")] == [refund.task_id]
    assert len(repo.history(refund.task_id)) == 2
    with store.transaction(read_only=True) as tx:
        links = tx.all("SELECT * FROM support_task_messages WHERE task_id=?", (refund.task_id,))
    assert len(links) == 2  # Both sessions used sequence=1.
    # Default ingress cannot pause or rewrite a durable matter.
    store.receive("s1", "alice", "你好", "new-old-route")
    assert repo.get(refund.task_id) == result
    assert store.tasks.list("s1", "alice")["tasks"] == []


def test_permission_enforced_for_every_access(store):
    record = create(store)
    bob = TaskRepository(store, "bob")
    assert bob.list() == []
    for action in (
        lambda: bob.get(record.task_id),
        lambda: bob.history(record.task_id),
        lambda: bob.list(session_id="s1"),
        lambda: bob.get(record.task_id, session_id="other"),
        lambda: bob.update(record.task_id, "other", expected_version=1, change_id="steal",
                           source={"kind": "message", "session_id": "other", "sequence": 1}, patch={"goal": "偷看"}),
        lambda: bob.create("s1", change_id="steal", source=SOURCE, goal="偷看"),
    ):
        with pytest.raises(SupportError) as error:
            action()
        assert error.value.status == 404
    with pytest.raises(SupportError):
        create(store, change_id="forged", source={"kind": "message", "session_id": "other", "sequence": 1})
    assert TaskRepository(store, "alice").get(record.task_id).version == 1


def test_retries_are_exact_and_do_not_overwrite_newer_progress(store):
    first = create(store)
    assert create(store) == first
    changed = update(store, first)
    assert create(store) == first
    assert update(store, first) == changed
    assert TaskRepository(store, "alice").get(first.task_id) == changed
    assert len(TaskRepository(store, "alice").history(first.task_id)) == 2
    for call in (
        lambda: create(store, goal="换了内容"),
        lambda: update(store, first, patch={"goal": "换了内容"}),
        lambda: update(store, first, change_id="create"),
    ):
        with pytest.raises(SupportError) as error:
            call()
        assert error.value.status == 409


def test_history_is_private_and_preserves_before_after_and_source(store):
    first = create(store)
    changed = update(store, first)
    events = TaskRepository(store, "alice").history(first.task_id)
    assert events[0]["before"] is None
    assert events[1]["before"] == first.model_dump()
    assert events[1]["after"] == changed.model_dump()
    assert events[1]["source"] == SOURCE
    assert events[1]["change_id"] == "update"
    assert TaskRepository(store, "alice").history(first.task_id, after_version=1) == events[1:]
    with store.transaction(read_only=True) as tx:
        assert all(r["visibility"] == "internal" for r in tx.all("SELECT visibility FROM support_events WHERE kind='matter_change'"))


@pytest.mark.parametrize("creation", [False, True])
def test_event_failure_rolls_back_state_links_and_receipt(store, monkeypatch, creation):
    record = create(store)
    original = store._event
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash before commit")
    monkeypatch.setattr(store, "_event", broken)
    with pytest.raises(RuntimeError):
        create(store, change_id="failed") if creation else update(store, record, change_id="failed")
    monkeypatch.setattr(store, "_event", original)
    repo = TaskRepository(SupportStore(store.dsn), "alice")
    assert repo.get(record.task_id) == record
    assert repo.list() == [record]
    assert len(repo.history(record.task_id)) == 1
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM task_changes")["n"] == 1
        assert tx.one("SELECT COUNT(*) AS n FROM support_task_messages")["n"] == 1
    retried = create(store, change_id="failed") if creation else update(store, record, change_id="failed")
    assert retried.version == (1 if creation else 2)


def test_real_database_version_race_and_failed_change_can_retry(store):
    original = create(store)
    def attempt(index):
        local = SupportStore(store.dsn)
        try:
            return TaskRepository(local, "alice").update(
                original.task_id, "s1" if index % 2 else "s2",
                expected_version=1, change_id=f"race-{index}", source=SOURCE,
                patch={"details": {"quantity": index + 1}})
        except VersionConflict:
            return None
    with ThreadPoolExecutor(4) as workers:
        results = list(workers.map(attempt, range(4)))
    assert sum(r is not None for r in results) == 1
    repo = TaskRepository(store, "alice")
    assert repo.get(original.task_id).version == 2
    assert len(repo.history(original.task_id)) == 2
    loser = next(i for i, r in enumerate(results) if r is None)
    current = repo.get(original.task_id)
    assert update(store, current, change_id=f"race-{loser}").version == 3


def test_concurrent_create_same_request_is_one_matter(store):
    with ThreadPoolExecutor(4) as workers:
        results = list(workers.map(lambda _: create(SupportStore(store.dsn)), range(4)))
    assert len({r.task_id for r in results}) == 1
    assert len(TaskRepository(store, "alice").list()) == 1
    assert len(TaskRepository(store, "alice").history(results[0].task_id)) == 1


def test_pagination_closed_records_and_explicit_null(store):
    records = [create(store, change_id=f"create-{i}", goal=f"事项 {i}") for i in range(5)]
    repo = TaskRepository(store, "alice")
    page = repo.list(limit=2)
    tail = page[-1]
    next_page = repo.list(limit=3, before=(tail.updated_at, tail.task_id))
    assert {t.task_id for t in page + next_page} == {t.task_id for t in records}
    assert len(page + next_page) == 5
    waiting = update(store, records[0], patch={"waiting_for": {"question_id": "q1", "prompt": "原因？"}})
    closed = update(store, waiting, change_id="close", patch={"status": "completed", "waiting_for": None})
    assert repo.get(closed.task_id).waiting_for is None
    assert len(repo.list()) == 4
    assert len(repo.list(include_closed=True)) == 5


def test_invalid_sources_and_payloads_never_change_progress(store):
    first = create(store)
    with store.transaction() as tx:
        c = store._control(tx, "s1", "alice")
        private = store._event(tx, c, "message", "alice", {"role": "user", "content": "私有备注"}, private=True)
        assistant = store._event(tx, c, "message", "bot", {"role": "assistant", "content": "猜测"})
    for sequence in (private, assistant, 9999):
        with pytest.raises(SupportError):
            update(store, first, change_id=f"invalid-{sequence}", source={**SOURCE, "sequence": sequence})
    for patch in ({"user_id": "bob"}, {"status": "refunded"}, {"goal": None},
                  {"details": None}, {"details": {"amount": float("nan")}}, {"goal": "x" * 2001}):
        with pytest.raises(SupportError) as error:
            update(store, first, patch=patch)
        assert error.value.status == 422
    assert TaskRepository(store, "alice").get(first.task_id) == first
    assert len(TaskRepository(store, "alice").history(first.task_id)) == 1


def test_tool_evidence_must_exist_and_belong_to_user_and_session(store):
    source = dict(kind="tool_result", session_id="s1", event_id="tool-1")
    with pytest.raises(SupportError):
        create(store, source=source)
    event = dict(event_id="tool-1", event_type="tool_result", user_id="alice", session_id="s1",
                 payload={"tool": "query_order", "success": True, "evidence": {"order_id": "ORD-A"}})
    with store.transaction() as tx:
        tx.execute("INSERT INTO case_snapshots VALUES (?,?)", ("alice", json.dumps({"events": [event]})))
    record = create(store, source=source)
    assert TaskRepository(store, "alice").history(record.task_id)[0]["source"] == source
    with pytest.raises(SupportError):
        update(store, record, source={**source, "session_id": "s2"})


def seed_v9(store):
    with store._connection() as tx:
        for ddl in SCHEMA:
            tx.execute(ddl)
        # Reproduce the real legacy constraints.
        tx.execute("DROP TABLE support_task_messages")
        tx.execute("""CREATE TABLE support_task_messages
            (task_id TEXT,session_id TEXT,sequence INTEGER,PRIMARY KEY(task_id,sequence))""")
        tx.execute("""CREATE UNIQUE INDEX conversation_one_active_task
            ON conversation_tasks(session_id) WHERE status IN ('ready','running','waiting_input','waiting_confirmation')""")
        tx.execute("CREATE TABLE support_schema_versions (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        tx.execute("INSERT INTO support_schema_versions VALUES (9,'old')")
        tx.execute("INSERT INTO support_conversations(session_id,user_id,created_at) VALUES ('s1','alice','old')")
        for task_id, session_id in (("old", "s1"), ("orphan", "missing")):
            tx.execute("""INSERT INTO conversation_tasks VALUES (?,?,? ,?,?,? ,?,?,? ,?,?,?)""",
                       (task_id, session_id, "refund_service", "退款", "旧退款请求", '{"order_id":"ORD-A"}',
                        "waiting_input", 7, '{"field":"reason","prompt":"退款原因？"}', "old", "old", 1))
        tx.execute("INSERT INTO support_task_messages VALUES ('old','s1',1)")


def test_v9_migration_is_repeatable_and_old_flows_remain_readable(empty_store):
    seed_v9(empty_store)
    empty_store.initialize()
    for _ in range(2):
        SupportStore(empty_store.dsn).initialize()
    repo = TaskRepository(empty_store, "alice")
    old = repo.get("old")
    assert old.goal == "旧退款请求" and old.details == {"order_id": "ORD-A"}
    assert old.version == 7 and old.status == "waiting_user" and old.storage_mode == "legacy"
    assert empty_store.tasks.get("s1", "alice", "old")["intent"] == "refund_service"
    with pytest.raises(SupportError):
        repo.get("orphan")
    with pytest.raises(SupportError):
        update(empty_store, old)
    with empty_store.transaction() as tx:
        assert tx.one("SELECT MAX(version) AS v FROM support_schema_versions")["v"] == SCHEMA_VERSION
        assert tx.one("SELECT COUNT(*) AS n FROM support_task_messages")["n"] == 1
        assert tx.one("SELECT user_id FROM conversation_tasks WHERE task_id='orphan'")["user_id"] is None
        if tx.postgres:
            types = tx.all("SELECT column_name,data_type FROM information_schema.columns "
                           "WHERE table_schema=current_schema() AND table_name='conversation_tasks'")
            assert {r["column_name"] for r in types if r["data_type"] == "jsonb"} == {"details", "waiting_for"}
        c = empty_store._control(tx, "s1", "alice")
        for _ in range(2):
            legacy = empty_store.tasks.create(tx, c, {"intent": "logistics_inquiry", "text": "查物流"}, "alice")
            legacy["status"] = "ready"
            empty_store.tasks.save(tx, c, legacy, "alice")
    assert len(repo.list()) == 3


def test_migration_failure_rolls_back_and_can_be_retried(empty_store, monkeypatch):
    from app.tasks import migrations
    seed_v9(empty_store)
    original = migrations.migrate_task_storage
    def fail(tx):
        original(tx)
        raise RuntimeError("migration interrupted")
    monkeypatch.setattr(migrations, "migrate_task_storage", fail)
    with pytest.raises(RuntimeError):
        empty_store.initialize()
    with empty_store._connection() as tx:
        assert tx.one("SELECT MAX(version) AS v FROM support_schema_versions")["v"] == 9
        assert tx.one("SELECT COUNT(*) AS n FROM conversation_tasks")["n"] == 2
        assert tx.one("SELECT COUNT(*) AS n FROM support_task_messages")["n"] == 1
    monkeypatch.setattr(migrations, "migrate_task_storage", original)
    empty_store.initialize()
    assert TaskRepository(empty_store, "alice").get("old").version == 7


def process_update(args):
    dsn, task_id, version, index = args
    repo = TaskRepository(SupportStore(dsn), "alice")
    try:
        repo.update(task_id, "s2", expected_version=version, change_id=f"process-{index}",
                    source=SOURCE, patch={"details": {"quantity": index + 1}})
        return True
    except VersionConflict:
        return False


def test_concurrent_processes_use_database_versions(store):
    record = create(store)
    with ProcessPoolExecutor(3, mp_context=get_context("spawn")) as workers:
        results = list(workers.map(process_update, [(store.dsn, record.task_id, 1, i) for i in range(3)]))
    assert results.count(True) == 1
    assert TaskRepository(store, "alice").get(record.task_id).version == 2
