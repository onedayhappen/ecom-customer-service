# 文件说明：测试连续消息幂等接收、固定批次确认、并发领取和崩溃恢复。
"""Continuous intake, exact batch acknowledgements and crash recovery."""
import os
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context

import pytest

from app.support.store import SupportError, SupportStore
from app.tasks.intake import MessageIntake
from app.tasks.repository import TaskRepository
from tests.test_task_storage import empty_store  # Shared isolated SQLite/PostgreSQL fixture.


@pytest.fixture
def store(empty_store):
    empty_store.ensure("s", "alice")
    empty_store.ensure("s2", "alice")
    empty_store.ensure("other", "bob")
    return empty_store


def send(store, request_id="m1", content="耳机退款", **kwargs):
    return MessageIntake(store).receive("s", "alice", request_id=request_id, content=content, **kwargs)


def outcomes(batch, status="associated"):
    return [dict(sequence=m["sequence"], status=status, decision_id=f"fixed:{m['request_id']}") for m in batch.messages]


def test_arrivals_before_during_and_after_processing_are_preserved(store):
    intake = MessageIntake(store)
    first = send(store)
    batch = intake.claim_next()
    assert [m["sequence"] for m in batch.messages] == [first["sequence"]]
    assert intake.progress("s", "alice")["associated_sequence"] == 0
    with store.transaction() as tx:
        c = store._control(tx, "s", "alice")
        store._event(tx, c, "audit", "system", {"data": "an interspersed event"}, private=True)
    second = send(store, "m2", "左耳没声音")
    assert second["sequence"] > first["sequence"] + 1
    assert intake.complete(batch, outcomes(batch)) == first["sequence"]
    third = send(store, "m3", "只退其中一件")
    next_batch = MessageIntake(SupportStore(store.dsn)).claim_next()
    assert [m["content"] for m in next_batch.messages] == ["左耳没声音", "只退其中一件"]
    assert intake.complete(next_batch, outcomes(next_batch)) == third["sequence"]
    assert intake.claim_next() is None
    assert store.public_history("s", "alice") == [
        {"role": "user", "content": "耳机退款"},
        {"role": "user", "content": "左耳没声音"},
        {"role": "user", "content": "只退其中一件"},
    ]


def test_request_retry_and_identical_text_with_new_id(store):
    original = send(store)
    assert send(store) == original
    second = send(store, "m2")
    assert second["sequence"] > original["sequence"]
    for action in (lambda: send(store, content="换了内容"),
                   lambda: MessageIntake(store).receive("s2", "alice", request_id="m1", content="耳机退款")):
        with pytest.raises(SupportError):
            action()
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE kind='association'")["n"] == 1
        assert tx.one("SELECT COUNT(*) AS n FROM message_intake_items")["n"] == 2


def test_only_one_worker_claims_same_session(store):
    send(store)
    with ThreadPoolExecutor(4) as workers:
        results = list(workers.map(lambda _: MessageIntake(SupportStore(store.dsn)).claim_next(), range(4)))
    assert sum(b is not None for b in results) == 1


def test_expired_association_lease_recovers_and_rejects_old_completion(store):
    send(store)
    intake = MessageIntake(store)
    first = intake.claim_next()
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (first.job_id,))
    second = MessageIntake(SupportStore(store.dsn)).claim_next()
    assert second.owner != first.owner and second.job_id == first.job_id
    with pytest.raises(SupportError):
        intake.complete(first, outcomes(first))
    assert intake.complete(second, outcomes(second)) == second.messages[-1]["sequence"]
    assert intake.complete(second, outcomes(second)) == second.messages[-1]["sequence"]
    with pytest.raises(SupportError):
        intake.complete(second, outcomes(second, status="clarification"))


def test_lease_cannot_ack_unread_or_partial_range(store):
    send(store)
    send(store, "m2")
    intake = MessageIntake(store)
    batch = intake.claim_next()
    for decisions in (outcomes(batch)[:1], outcomes(batch) * 2,
                      outcomes(batch) + [dict(sequence=999, status="associated", decision_id="forged")]):
        with pytest.raises(SupportError):
            intake.complete(batch, decisions)
    assert intake.progress("s", "alice")["associated_sequence"] == 0
    intake.complete(batch, outcomes(batch, status="clarification"))
    progress = intake.progress("s", "alice")
    assert progress["associated_sequence"] == progress["received_sequence"]
    assert all(item["association_status"] == "clarification" for item in progress["messages"])
    assert all(m["role"] == "user" for m in store.public_history("s", "alice"))


def test_archived_batch_keeps_idempotent_completion_receipt(store):
    from app.runtime.maintenance import archive_jobs
    send(store)
    intake = MessageIntake(store)
    batch = intake.claim_next()
    result = intake.complete(batch, outcomes(batch))
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET finished_at=1 WHERE job_id=?", (batch.job_id,))
    assert archive_jobs(store, retention_days=1) == 1
    assert intake.complete(batch, outcomes(batch)) == result
    with pytest.raises(SupportError):
        intake.complete(batch, outcomes(batch, status="clarification"))


def test_failed_admission_and_completion_commit_nothing(store, monkeypatch):
    original = store.jobs.admit
    def failing(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("database disconnected before commit")
    monkeypatch.setattr(store.jobs, "admit", failing)
    with pytest.raises(RuntimeError):
        send(store)
    assert store.public_history("s", "alice") == []
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs")["n"] == 0
        assert tx.one("SELECT COUNT(*) AS n FROM message_intake_items")["n"] == 0
    monkeypatch.setattr(store.jobs, "admit", original)
    send(store)
    intake = MessageIntake(store)
    batch = intake.claim_next()
    original_event = store._event
    def failing_event(*args, **kwargs):
        original_event(*args, **kwargs)
        raise RuntimeError("event persistence failure")
    monkeypatch.setattr(store, "_event", failing_event)
    with pytest.raises(RuntimeError):
        intake.complete(batch, outcomes(batch))
    assert intake.progress("s", "alice")["associated_sequence"] == 0
    monkeypatch.setattr(store, "_event", original_event)
    intake.complete(batch, outcomes(batch))


def test_message_batch_and_task_mutations_share_one_transaction(store):
    sent = send(store)
    intake = MessageIntake(store)
    batch = intake.claim_next()
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (batch.job_id,))
    repo = TaskRepository(store, "alice")
    with pytest.raises(SupportError):
        with store.transaction() as tx:
            repo.create("s", goal="退货", change_id="association-create",
                        source=dict(kind="message", session_id="s", sequence=sent["sequence"]), tx=tx)
            intake.complete(batch, outcomes(batch), tx=tx)
    assert repo.list() == []
    new_batch = intake.claim_next()
    with store.transaction() as tx:
        repo.create("s", goal="退货", change_id="association-create",
                    source=dict(kind="message", session_id="s", sequence=sent["sequence"]), tx=tx)
        intake.complete(new_batch, outcomes(new_batch), tx=tx)
    assert len(repo.list()) == 1


def test_handoff_fences_inflight_batch_and_preserves_unread_input(store):
    first = send(store)
    intake = MessageIntake(store)
    batch = intake.claim_next()
    send(store, "handoff", "转人工")
    assert store.state("s", "alice")["mode"] == "handoff_pending"
    with pytest.raises(SupportError):
        intake.complete(batch, outcomes(batch))
    assert intake.claim_next() is None
    send(store, "more", "这是订单号")
    progress = intake.progress("s", "alice")
    assert progress["associated_sequence"] == 0
    assert progress["messages"][0]["sequence"] == first["sequence"]
    assert [m["association_status"] for m in progress["messages"]] == ["pending", "control", "pending"]


def test_explicit_cancel_only_changes_selected_task_and_is_atomic(store):
    first = send(store)
    repo = TaskRepository(store, "alice")
    source = dict(kind="message", session_id="s", sequence=first["sequence"])
    first_task = repo.create("s", goal="退货", change_id="refund", source=source)
    other_task = repo.create("s", goal="开票", change_id="invoice", source=source)
    intake = MessageIntake(store)
    batch = intake.claim_next()
    initial_version = store.state("s", "alice")["control_version"]
    send(store, "cancel", "取消退货", action="cancel", task_id=first_task.task_id, task_version=first_task.version)
    assert repo.get(first_task.task_id).status == "cancelled"
    assert repo.get(other_task.task_id) == other_task
    assert store.state("s", "alice")["control_version"] == initial_version
    assert len(store.public_history("s", "alice")) == 2
    with pytest.raises(SupportError):
        send(store, "stale", "取消退货", action="cancel", task_id=first_task.task_id, task_version=first_task.version)
    assert len(store.public_history("s", "alice")) == 2
    intake.complete(batch, outcomes(batch))
    assert intake.progress("s", "alice")["associated_sequence"] == intake.progress("s", "alice")["received_sequence"]


def test_ambiguous_cancel_stays_pending_and_controls_bypass_backlog_limit(store):
    intake = MessageIntake(store, max_unassociated=1)
    intake.receive("s", "alice", request_id="m1", content="那个先不要了")
    assert intake.progress("s", "alice")["messages"][0]["association_status"] == "pending"
    with pytest.raises(SupportError) as error:
        intake.receive("s", "alice", request_id="m2", content="继续")
    assert error.value.status == 429
    intake.receive("s", "alice", request_id="handoff", content="转人工")
    assert store.state("s", "alice")["mode"] == "handoff_pending"


def test_batch_limit_never_skips_messages_and_scope_is_checked(store):
    for i in range(5):
        send(store, f"m{i}")
    intake = MessageIntake(store, batch_size=2)
    counts = []
    while batch := intake.claim_next():
        counts.append(len(batch.messages))
        intake.complete(batch, outcomes(batch))
    assert counts == [2, 2, 1]
    assert len(intake.progress("s", "alice", limit=2)["messages"]) == 2
    for call in (lambda: intake.progress("s", "bob"), lambda: intake.claim("s", "bob"),
                 lambda: intake.receive("s", "bob", request_id="no", content="forbidden")):
        with pytest.raises(SupportError):
            call()


def committed_then_crashed(dsn):
    MessageIntake(SupportStore(dsn)).receive("s", "alice", request_id="crash-message", content="已保存但进程退出")
    os._exit(0)


def test_process_exit_after_admission_leaves_recoverable_outbox(store):
    process = get_context("spawn").Process(target=committed_then_crashed, args=(store.dsn,))
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("child intake process did not finish")
    assert process.exitcode == 0
    batch = MessageIntake(SupportStore(store.dsn)).claim_next()
    assert [m["content"] for m in batch.messages] == ["已保存但进程退出"]


def test_concurrent_request_id_reuse_across_sessions_is_clean_conflict(store):
    def receive(sid):
        try:
            return MessageIntake(SupportStore(store.dsn)).receive(sid, "alice", request_id="same", content="消息")
        except SupportError as error:
            return error.status
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(receive, ["s", "s2"]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count(409) == 1
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM support_events WHERE kind='message'")["n"] == 1


def test_v10_migration_preserves_legacy_outbox_and_rolls_back_on_failure(empty_store, monkeypatch):
    from app.support.store import SCHEMA
    from app.tasks import migrations
    import json
    with empty_store._connection() as tx:
        for ddl in SCHEMA:
            tx.execute(ddl)
        migrations.migrate_task_storage(tx)
        tx.execute("CREATE TABLE support_schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT)")
        tx.execute("INSERT INTO support_schema_versions VALUES (10,'old')")
        tx.execute("INSERT INTO support_conversations(session_id,user_id,created_at) VALUES ('s','alice','old')")
        payload = json.dumps(dict(job_id="old-job", session_id="s", user_id="alice", message="旧请求", control_version=0))
        tx.execute("""INSERT INTO runtime_jobs(job_id,session_id,user_id,control_version,payload,status,
            created_at,queue_deadline,run_timeout,next_delivery)
            VALUES ('old-job','s','alice',0,?,'pending',0,9999999999,600,0)""", (payload,))
    original = migrations.migrate_message_intake
    def fail(tx):
        original(tx)
        raise RuntimeError("migration aborted")
    monkeypatch.setattr(migrations, "migrate_message_intake", fail)
    with pytest.raises(RuntimeError):
        empty_store.initialize()
    with empty_store._connection() as tx:
        assert tx.one("SELECT MAX(version) AS v FROM support_schema_versions")["v"] == 10
        assert tx.one("SELECT payload FROM runtime_jobs WHERE job_id='old-job'")["payload"] == payload
    monkeypatch.setattr(migrations, "migrate_message_intake", original)
    empty_store.initialize()
    SupportStore(empty_store.dsn).initialize()
    with empty_store.transaction(read_only=True) as tx:
        row = tx.one("SELECT * FROM runtime_jobs WHERE job_id='old-job'")
    assert row["kind"] == "conversation" and row["status"] == "superseded"
