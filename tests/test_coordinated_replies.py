"""Publication boundaries on SQLite and isolated PostgreSQL, without a live LLM."""
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from multiprocessing import get_context
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.support.store import SupportError, SupportStore
from app.tasks.association import AssociationService
from app.tasks.conversation_worker import ConversationWorker
from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.intake import MessageIntake
from app.tasks.replies import ReplyCoordinator
from app.tasks.repository import TaskRepository
from app.tasks.scheduling import TaskSchedule, TaskWorker
from tests.test_task_candidates import store, task, ask
from tests.test_task_storage import empty_store, message
from tests.test_task_execution import request, engine, local_budgets, Client, response
from tests.test_task_scheduling import QueryClient, drain, result as query_result
from tests.test_message_association import batch_for, service, proposal, new, update, quote
from tests.test_association_correction import mistaken, request as correction_request
from app.tasks.correction import CorrectionService
from app.tasks.provenance import context_event_valid


def ready(store, record, source, *, total=12.50, reply="已经为您退款999元", result=None):
    runs = TaskRuns(store, record.user_id)
    req = TaskExecutionInput.model_validate(request(record, source))
    reservation, _ = runs.begin(req)
    payload = reservation["payload"]
    receipt = runs.save_tool(req, payload, 0, "query_order", {"order_id": record.details["order_id"]},
                             result or {"success": True, "order": {
                                 "order_id": record.details["order_id"], "status": "已发货", "total": total}})
    runs.finish(req, payload, dict(state="ready", task_version=record.version, reply=reply,
                                  evidence_ids=[receipt["event_id"]]))
    return req.job_id


def replies(store, sid="s"):
    return [p for p in store.public_history(sid, "alice") if p["role"] == "assistant"]


def test_merges_ready_results_and_uses_each_tasks_structured_facts(store):
    a, sa = task(store, "s")
    b, sb = task(store, "s", details={"order_id": "ORD-B"})
    ids = [ready(store, a, sa, total=12.5), ready(store, b, sb, total=37)]
    reply = ReplyCoordinator(store, "alice").publish("s")
    assert reply["result_ids"] == ["job:" + x for x in ids]
    assert "ORD-A；当前状态：已发货；订单金额：¥12.50" in reply["content"]
    assert "ORD-B；当前状态：已发货；订单金额：¥37.00" in reply["content"]
    assert "退款999" not in reply["content"]
    assert reply["task_versions"] == {a.task_id: 1, b.task_id: 1}
    assert ReplyCoordinator(store, "alice").publish("s") is None
    assert len(replies(store)) == 1
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM task_reply_items WHERE disposition='published'")["n"] == 2
        assert tx.one("SELECT sequence FROM runtime_notifications WHERE session_id='s'")["sequence"] == reply["sequence"]


def test_fast_result_published_while_other_task_still_running(store):
    a, sa = task(store, "s")
    b, sb = task(store, "s", details={"order_id": "ORD-B"})
    release, started = threading.Event(), threading.Event()
    def tool(name, args):
        if args["order_id"] == "ORD-A":
            started.set()
            assert release.wait(10)
        return query_result(name, args)
    schedule = TaskSchedule(store, "alice")
    ids = [schedule.enqueue(request(t, s)) for t, s in [(a, sa), (b, sb)]]
    worker = TaskWorker(store, engine(QueryClient()), tools=tool, concurrency=2)
    try:
        worker.tick()
        assert started.wait(5)
        drain(worker, [ids[1]])
        fast = ReplyCoordinator(store, "alice").publish("s")
        assert "ORD-B" in fast["content"] and "ORD-A" not in fast["content"]
        assert ReplyCoordinator(store, "alice").publish("s") is None
        release.set()
        drain(worker, [ids[0]])
        slow = ReplyCoordinator(store, "alice").publish("s")
        assert "ORD-A" in slow["content"] and "ORD-B" not in slow["content"]
    finally:
        release.set()
        worker.close()
    assert len(replies(store)) == 2


def test_unassociated_new_input_in_any_session_blocks_facts_then_unrelated_releases(store):
    a, sa = task(store, "s")
    ready(store, a, sa)
    batch = batch_for(store, "鞋子价格", sid="old")
    assert ReplyCoordinator(store, "alice").publish("s") is None
    svc = service(store)
    svc.commit(batch, svc.snapshot(batch), proposal(new(batch, goal="鞋子价格", details={"item_name": "鞋子"})))
    assert "ORD-A" in ReplyCoordinator(store, "alice").publish("s")["content"]


def test_correction_before_publish_discards_old_result(store):
    wrong, target, decision = mistaken(store)
    with store.transaction(read_only=True) as tx:
        sa = tx.one("SELECT session_id,sequence FROM association_sources WHERE decision_id=?", (decision["decision_id"],))
    jid = ready(store, wrong, dict(kind="message", **sa))
    batch = batch_for(store, "不是 ORD-B，是 ORD-A")
    first = ReplyCoordinator(store, "alice").publish("s")
    assert first["result_ids"] == [] and not replies(store)
    source = dict(kind="message", session_id="s", sequence=batch.messages[0]["sequence"])
    correction = correction_request(store, wrong, target, decision)
    correction["source"] = source
    fixed = CorrectionService(store, "alice").correct("s", correction)
    MessageIntake(store).complete(batch, [dict(sequence=source["sequence"], status="associated",
                                             decision_id=fixed["new_decision_id"])])
    assert ReplyCoordinator(store, "alice").publish("s") is None
    current = TaskRepository(store, "alice").get(target.task_id)
    ready(store, current, source)
    public = ReplyCoordinator(store, "alice").publish("s")
    assert "ORD-A" in public["content"] and "ORD-B" not in public["content"]
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT disposition FROM task_reply_items WHERE item_key=?", ("job:" + jid,))["disposition"] == "stale"


def test_correction_after_combined_reply_invalidates_context_and_sends_one_notice(store):
    wrong, target, decision = mistaken(store)
    with store.transaction(read_only=True) as tx:
        source = tx.one("SELECT session_id,sequence FROM association_sources WHERE decision_id=?", (decision["decision_id"],))
    ready(store, wrong, dict(kind="message", **source))
    other, source2 = task(store, "s", details={"order_id": "ORD-C"})
    ready(store, other, source2)
    published = ReplyCoordinator(store, "alice").publish("s")
    assert len(published["task_versions"]) == 2 and "task_id" not in published
    req = correction_request(store, wrong, target, decision)
    fixed = CorrectionService(store, "alice").correct("s", req)
    assert fixed["corrected_public_reply"]
    assert CorrectionService(store, "alice").correct("s", req) == fixed
    with store.transaction(read_only=True) as tx:
        assert not context_event_valid(tx, "s", published["sequence"])
    assert len(replies(store)) == 2


def test_continuous_input_emits_one_processing_event_without_consuming_ready_result(store):
    a, sa = task(store, "s")
    jid = ready(store, a, sa)
    intake = MessageIntake(store)
    published = []
    for i in range(10):
        intake.receive("s", "alice", request_id=str(uuid4()), content=f"还要补充 {i}")
        published.append(ReplyCoordinator(store, "alice").publish("s"))
    assert sum(p is not None for p in published) == 1
    assert not replies(store)
    with store.transaction(read_only=True) as tx:
        assert not tx.one("SELECT item_key FROM task_reply_items WHERE item_key=?", ("job:" + jid,))
    assert intake.progress("s", "alice")["processing"] == "processing"


def test_only_one_valid_question_and_answer_prevents_repeat(store):
    a, sa = task(store, "s")
    b, sb = task(store, "s", details={"order_id": "ORD-B"})
    a, b = ask(store, a, sa, publish=False), ask(store, b, sb, publish=False)
    for t, s in [(a, sa), (b, sb)]:
        TaskSchedule(store, "alice").enqueue(request(t, s))
    first = ReplyCoordinator(store, "alice").publish("s")
    assert first["question_id"] in {a.waiting_for["question_id"], b.waiting_for["question_id"]}
    assert len(first["result_ids"]) == 1
    assert ReplyCoordinator(store, "alice").publish("s") is None
    answered = a if first["task_id"] == a.task_id else b
    batch = batch_for(store, "左耳没声音")
    svc = service(store)
    svc.commit(batch, svc.snapshot(batch), proposal(update(batch, answered, effect="answer",
        details={"reason": "左耳没声音"}, question_id=answered.waiting_for["question_id"])))
    second = ReplyCoordinator(store, "alice").publish("s")
    assert second["question_id"] != first["question_id"]
    assert len(replies(store)) == 2


def test_does_not_ask_for_reason_already_supplied(store):
    a, sa = task(store, "s", details={"order_id": "ORD-A", "reason": "损坏"})
    a = ask(store, a, sa, publish=False)
    TaskSchedule(store, "alice").enqueue(request(a, sa))
    reply = ReplyCoordinator(store, "alice").publish("s")
    assert "请补充" not in reply["content"] and "question_id" not in reply


@pytest.mark.parametrize("change", ["wrong_order", "stale", "failed", "other_task", "control", "malformed", "false_string"])
def test_unusable_evidence_cannot_become_facts(store, change):
    a, sa = task(store, "s")
    ready(store, a, sa)
    with store.transaction() as tx:
        row = tx.one("SELECT * FROM support_events WHERE kind='task_tool_result'")
        p = json.loads(row["payload"])
        if change == "wrong_order":
            p["result"]["order"]["order_id"] = "SECRET-OTHER"
        elif change == "stale":
            p["observed_epoch"] = 0
        elif change == "failed":
            p["result"]["success"] = False
        elif change == "other_task":
            p["task_id"] = "other-task"
        elif change == "malformed":
            p["result"]["order"] = ["invalid-shape"]
        elif change == "false_string":
            p["result"]["success"] = "false"
        else:
            p["controls"] = {"s": 999}
        tx.execute("UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?",
                   (json.dumps(p), row["session_id"], row["sequence"]))
    content = ReplyCoordinator(store, "alice").publish("s")["content"]
    assert "¥" not in content and "已发货" not in content and "SECRET" not in content


def test_atomic_rollback_includes_reply_coverage_and_notification(store, monkeypatch):
    a, sa = task(store, "s")
    ready(store, a, sa)
    coordinator = ReplyCoordinator(store, "alice")
    real = coordinator._mark
    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("injected crash before commit")
    monkeypatch.setattr(coordinator, "_mark", fail)
    with pytest.raises(RuntimeError):
        coordinator.publish("s")
    assert not replies(store)
    assert ReplyCoordinator(store, "alice").publish("s")
    assert len(replies(store)) == 1


def _publish_process(dsn):
    return ReplyCoordinator(SupportStore(dsn), "alice").publish("s")


def test_multiple_process_publishers_deliver_once(store):
    a, sa = task(store, "s")
    ready(store, a, sa)
    with ProcessPoolExecutor(2, mp_context=get_context("spawn")) as pool:
        results = list(pool.map(_publish_process, [store.dsn, store.dsn]))
    assert sum(r is not None for r in results) == 1 and len(replies(store)) == 1


def test_ownership_and_handoff_fence_publication(store):
    a, sa = task(store, "s")
    ready(store, a, sa)
    with pytest.raises(SupportError):
        ReplyCoordinator(store, "bob").publish("s")
    store.handoff("s", "alice")
    assert ReplyCoordinator(store, "alice").publish("s") is None and not replies(store)


def test_reconcile_recovers_association_commit_gap_and_reuses_progress_version(store):
    batch = batch_for(store, "ORD-A 查物流")
    svc = service(store)
    svc.commit(batch, svc.snapshot(batch), proposal(new(batch, goal="查物流", details={"order_id": "ORD-A"})))
    worker = ConversationWorker(store, engine(QueryClient()), tools=query_result)
    try:
        worker.reconcile()
        worker.reconcile()
        with store.transaction(read_only=True) as tx:
            jobs = tx.all("SELECT job_id FROM runtime_jobs WHERE kind='task_execution'")
        assert len(jobs) == 1
        drain(worker.executions, [jobs[0]["job_id"]])
        published = worker.tick()
        assert any("ORD-A" in p["content"] for p in published)
        assert worker.tick() == []
    finally:
        worker.close()


def test_agent_asks_once_despite_its_own_progress_version_change(store):
    a, sa = task(store, "s")
    client = Client(response(calls=[("ask_user", {"field": "reason", "prompt": "原因是什么？", "options": []})]))
    worker = ConversationWorker(store, engine(client))
    try:
        worker.reconcile()
        with store.transaction(read_only=True) as tx:
            jid = tx.one("SELECT job_id FROM runtime_jobs WHERE kind='task_execution'")["job_id"]
        drain(worker.executions, [jid])
        worker.reconcile()
        with store.transaction(read_only=True) as tx:
            assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE kind='task_execution'")["n"] == 1
    finally:
        worker.close()


def test_publisher_locks_input_position_until_message_and_coverage_commit(store, monkeypatch):
    a, sa = task(store, "s")
    ready(store, a, sa)
    coordinator = ReplyCoordinator(store, "alice")
    reached, release, sending = threading.Event(), threading.Event(), threading.Event()
    original = coordinator._publish
    def delayed(*args):
        reached.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(coordinator, "_publish", delayed)
    def incoming():
        sending.set()
        return MessageIntake(SupportStore(store.dsn)).receive("s", "alice", request_id=str(uuid4()), content="不是这笔")
    with ThreadPoolExecutor(2) as pool:
        publication = pool.submit(coordinator.publish, "s")
        assert reached.wait(5)
        incoming_message = pool.submit(incoming)
        assert sending.wait(5)
        try:
            assert not incoming_message.done()
        finally:
            release.set()
        sent = publication.result(timeout=5)
        received = incoming_message.result(timeout=5)
    assert received["sequence"] > sent["sequence"]
    assert len(replies(store)) == 1


def test_clarification_takes_priority_over_previously_sent_task_question(store):
    a, sa = task(store, "s")
    a = ask(store, a, sa)
    b, _ = task(store, "s", details={"order_id": "ORD-B"})
    batch = batch_for(store, "那个有问题")
    svc = service(store)
    svc.commit(batch, svc.snapshot(batch), proposal(dict(
        action="clarify", sequences=[batch.messages[0]["sequence"]], evidence=[quote(batch)],
        clarification="是哪笔？", candidate_task_ids=[a.task_id, b.task_id])))
    reply = ReplyCoordinator(store, "alice").publish("s")
    assert "clarification_id" in reply and "1. ORD-A；2. ORD-B" in reply["content"]
    assert ReplyCoordinator(store, "alice").publish("s") is None


def test_complete_worker_from_multiple_intake_messages_to_replies(store):
    agent = engine(QueryClient())
    def associate(context):
        batch = SimpleNamespace(session_id=context.session_id, messages=context.current_messages)
        return proposal(*(new(batch, index=i, goal="查询订单", details={"order_id": m["content"].split()[0]})
                          for i, m in enumerate(batch.messages)))
    agent.associate = associate
    intake = MessageIntake(store)
    for oid in ["ORD-A", "ORD-B"]:
        intake.receive("s", "alice", request_id=str(uuid4()), content=oid + " 查询订单")
    worker = ConversationWorker(store, agent, tools=query_result, concurrency=2)
    try:
        # This is eventual publication, not a 10-second latency benchmark.
        # Allow the normal execution window on loaded integration hosts.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            worker.tick()
            content = "\n".join(p["content"] for p in replies(store))
            if "ORD-A" in content and "ORD-B" in content:
                break
            time.sleep(.02)
        assert intake.progress("s", "alice")["associated_sequence"] == intake.progress("s", "alice")["received_sequence"]
        content = "\n".join(p["content"] for p in replies(store))
        with store.transaction(read_only=True) as tx:
            jobs = tx.all("SELECT kind,status,error,response FROM runtime_jobs")
        assert content.count("ORD-A") == content.count("ORD-B") == 1, jobs
        assert worker.tick() == []
    finally:
        worker.close()


def test_v15_upgrade_preserves_pending_work_and_rolls_back_as_one_transaction(store, monkeypatch):
    from app.tasks import migrations
    a, sa = task(store, "s")
    jid = ready(store, a, sa)
    with store.transaction() as tx:
        for table in ("task_operation_resources", "task_operation_proposals",
                      "task_reply_items", "task_reply_sessions", "task_execution_wakes"):
            tx.execute("DROP TABLE " + table)
        tx.execute("DROP INDEX runtime_task_reply_scan")
        tx.execute("DELETE FROM support_schema_versions WHERE version>15")
        tx.execute("INSERT INTO support_schema_versions VALUES (15,'before-upgrade') ON CONFLICT DO NOTHING")
    upgraded = SupportStore(store.dsn)
    original = migrations.migrate_coordinated_replies
    def fail(tx):
        original(tx)
        raise RuntimeError("simulated migration failure")
    monkeypatch.setattr(migrations, "migrate_coordinated_replies", fail)
    with pytest.raises(RuntimeError):
        upgraded.initialize()
    with upgraded._connection() as tx:
        assert tx.one("SELECT MAX(version) AS v FROM support_schema_versions")["v"] == 15
        assert tx.one("SELECT job_id FROM runtime_jobs WHERE job_id=?", (jid,))
    monkeypatch.setattr(migrations, "migrate_coordinated_replies", original)
    upgraded.initialize()
    SupportStore(store.dsn).initialize()
    with upgraded.transaction(read_only=True) as tx:
        assert tx.one("SELECT task_id FROM task_execution_wakes WHERE task_id=?", (a.task_id,))
    assert "ORD-A" in ReplyCoordinator(upgraded, "alice").publish("s")["content"]


def test_archived_old_job_does_not_crash_publisher_or_keep_processing_forever(store):
    from app.runtime.maintenance import archive_jobs
    a, sa = task(store, "s")
    jid = ready(store, a, sa)
    batch = batch_for(store, "收到")
    MessageIntake(store).complete(batch, [dict(sequence=batch.messages[0]["sequence"],
                                             status="associated", decision_id="ignored")])
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET finished_at=0 WHERE job_id=?", (jid,))
    archive_jobs(store, retention_days=1)
    assert ReplyCoordinator(store, "alice").publish("s") is None
    assert MessageIntake(store).progress("s", "alice")["processing"] == "idle"


def test_deferred_questions_do_not_starve_later_ready_result(store):
    # Force more waiting jobs than one publisher scan can hold.
    for i in range(51):
        a, sa = task(store, "s", details={"order_id": f"ORD-WAIT-{i}"})
        a = ask(store, a, sa, publish=False)
        TaskSchedule(store, "alice").enqueue(request(a, sa))
    fast, fs = task(store, "s", details={"order_id": "ORD-FAST"})
    ready(store, fast, fs)
    coordinator = ReplyCoordinator(store, "alice")
    coordinator.publish("s")
    second = coordinator.publish("s")
    assert "ORD-FAST" in second["content"] and "question_id" not in second
    assert coordinator.publish("s") is None


def test_other_human_session_does_not_block_an_unrelated_ready_task(store):
    a, sa = task(store, "s")
    ready(store, a, sa)
    store.handoff("old", "alice")
    MessageIntake(store).receive("old", "alice", request_id=str(uuid4()), content="给人工客服的补充")
    assert "ORD-A" in ReplyCoordinator(store, "alice").publish("s")["content"]


def test_expired_queue_reports_failure_once_without_claiming_business_success(store):
    a, sa = task(store, "s")
    jid = TaskSchedule(store, "alice").enqueue(request(a, sa))
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET queue_deadline=0 WHERE job_id=?", (jid,))
    worker = TaskWorker(store, engine(QueryClient()))
    try:
        worker.recover()
        reply = ReplyCoordinator(store, "alice").publish("s")
        assert "可核实" in reply["content"] and "已发货" not in reply["content"]
        assert ReplyCoordinator(store, "alice").publish("s") is None
    finally:
        worker.close()
