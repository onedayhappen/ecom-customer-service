"""Real storage/association boundaries with deterministic model/tool responses."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.query_rewrite import rewrite
from app.tasks.execution_store import TaskRuns
from app.tasks.intake import MessageIntake
from app.tasks.repository import TaskRepository
from app.tasks.scheduling import TaskSchedule, TaskWorker
from app.tasks.execution_models import TaskExecutionInput
from tests.test_message_association import batch_for, new, proposal, quote, service, store, update
from tests.test_task_candidates import task
from tests.test_task_execution import Client, engine, request, response
from tests.test_task_storage import empty_store, message


def test_rewrite_freezes_raw_history_and_feeds_retrieval(store):
    message(store, "s", "alice", "耳机左耳没声音")
    with store.transaction() as tx:
        c = store._control(tx, "s", "alice")
        store._event(tx, c, "message", "bot", {"role": "assistant", "content": "要换货还是退货？"})
    batch = batch_for(store, "换获吧", "运费我不出")
    svc, seen = service(store), []
    def normalize(context):
        seen.append(context.data())
        # A newer message must not silently enter this batch or its history.
        MessageIntake(store).receive("s", "alice", request_id=str(uuid4()), content="另外查键盘物流")
        return "用户要求更换左耳无声的耳机，且不承担运费。"
    def associate(context):
        assert context.rewritten_query == "用户要求更换左耳无声的耳机，且不承担运费。"
        assert [m["content"] for m in context.current_messages] == ["换获吧", "运费我不出"]
        assert context.recent_messages == seen[0]["recent_messages"]
        decision = new(batch, "更换耳机且用户不承担运费")
        decision["sequences"] = [m["sequence"] for m in batch.messages]
        decision["evidence"] = [quote(batch, i) for i in range(2)]
        return proposal(decision)
    svc.process(batch, SimpleNamespace(rewrite=normalize, associate=associate))
    assert len(seen) == 1 and not seen[0]["candidates"]
    with store.transaction(read_only=True) as tx:
        saved = json.loads(tx.one("SELECT payload FROM runtime_jobs WHERE job_id=?", (batch.job_id,))["payload"])
        assert saved["query_rewrite"]["status"] == "ok"
        assert saved["sequences"] == [m["sequence"] for m in batch.messages]
    next_batch = MessageIntake(store).claim("s", "alice")
    assert [m["content"] for m in next_batch.messages] == ["另外查键盘物流"]


@pytest.mark.parametrize("output", ["", '{"query":"退货"}', RuntimeError("provider unavailable")])
def test_bad_rewrite_falls_back_without_losing_raw_messages(store, output):
    batch = batch_for(store, "我要退货")
    def normalize(_):
        if isinstance(output, Exception):
            raise output
        return output
    def associate(context):
        assert context.rewritten_query == "我要退货"
        return proposal(new(batch))
    svc = service(store)
    svc.process(batch, SimpleNamespace(rewrite=normalize, associate=associate))
    with store.transaction(read_only=True) as tx:
        saved = json.loads(tx.one("SELECT payload FROM runtime_jobs WHERE job_id=?", (batch.job_id,))["payload"])
        assert saved["query_rewrite"]["status"] == "fallback"


def test_rewrite_model_has_no_json_output_contract(store):
    batch = batch_for(store, "不要退了")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: (
        pytest.fail("structured output requested") if "response_format" in kw
        else response("用户要求停止退货。")))))
    assert rewrite(client, "test", service(store).candidates.dialogue("s", [batch.messages[0]["sequence"]])) == "用户要求停止退货。"


def test_reclaimed_batch_keeps_its_range_and_rewrite(store):
    batch = batch_for(store, "我要退货")
    svc = service(store)
    dialogue = svc.candidates.dialogue("s", [batch.messages[0]["sequence"]])
    svc._rewrite(batch, SimpleNamespace(rewrite=lambda _: "用户要求退货。"), dialogue)
    MessageIntake(store).receive("s", "alice", request_id=str(uuid4()), content="改成换货")
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (batch.job_id,))
    reclaimed = MessageIntake(store).claim("s", "alice")
    assert reclaimed.messages == batch.messages and reclaimed.owner != batch.owner
    result = svc.process(reclaimed, SimpleNamespace(
        rewrite=lambda _: pytest.fail("rewrite repeated"), associate=lambda _: proposal(new(reclaimed))))
    assert result["decisions"]
    assert MessageIntake(store).claim("s", "alice").messages[0]["content"] == "改成换货"


def test_update_discards_old_model_plan_without_resetting_budget(store):
    original, source = task(store, "s")
    svc = service(store)
    def old_model(_):
        batch = batch_for(store, "改成换货，运费我不出")
        decision = update(batch, original, effect="amend")
        decision["goal"] = "耳机换货，运费用户不承担"
        svc.commit(batch, svc.snapshot(batch), proposal(decision))
        return response(calls=[("get_order", {"order_id": "ORD-A"})])
    def new_model(params):
        assert "换货" in params["messages"][1]["content"]
        return response("请补充换货原因。")
    client = Client(old_model, new_model)
    result = engine(client).execute_task(store, "alice", request(original, source),
        tools=lambda *a: pytest.fail("stale tool plan executed"))
    assert result["state"] == "ready" and result["task_version"] == 2
    assert result["model_calls"] == 2 and result["tool_calls"] == 0


def test_tool_result_is_saved_before_replanning_and_late_evidence_is_not_reused(store):
    original, source = task(store, "s")
    svc, calls = service(store), []
    def tool(name, args):
        calls.append(name)
        batch = batch_for(store, "不要退货了，换货")
        decision = update(batch, original, effect="amend")
        decision["goal"] = "耳机换货"
        svc.commit(batch, svc.snapshot(batch), proposal(decision))
        return {"success": True, "order": {"order_id": "ORD-A", "status": "已发货"}}
    client = Client(response(calls=[("get_order", {"order_id": "ORD-A"}),
                                   ("get_order_items", {"order_id": "ORD-A"})]),
                    response("请补充换货原因。"))
    result = engine(client).execute_task(store, "alice", request(original, source), tools=tool)
    assert result["state"] == "ready" and calls == ["get_order"]
    with store.transaction(read_only=True) as tx:
        evidence = [json.loads(r["payload"]) for r in tx.all("SELECT payload FROM support_events WHERE kind='task_tool_result'")]
    assert len(evidence) == 1 and evidence[0]["late"] and evidence[0]["result"]["success"]
    assert not TaskRuns(store, "alice").evidence(svc.repo.get(original.task_id))


def test_pending_unrelated_message_waits_then_continues_existing_loop(store):
    original, source = task(store, "s")
    arrived, next_model = threading.Event(), threading.Event()
    holder = {}
    def tool(name, args):
        holder["batch"] = batch_for(store, "键盘什么时候到")
        arrived.set()
        return {"success": True, "order": {"order_id": "ORD-A", "status": "已发货"}}
    def answer(_):
        next_model.set()
        return response("请补充退货原因。")
    client = Client(response(calls=[("get_order", {"order_id": "ORD-A"})]), answer)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(engine(client).execute_task, store, "alice", request(original, source), tools=tool)
        assert arrived.wait(5)
        assert not next_model.wait(.15)
        batch, svc = holder["batch"], service(store)
        svc.commit(batch, svc.snapshot(batch), proposal(new(batch, "查询键盘物流")))
        result = running.result(timeout=10)
    assert next_model.is_set() and result["task_version"] == original.version
    assert len(client.requests[1]["messages"]) > 2  # tool conversation was preserved


def test_one_message_updates_two_running_matters_and_creates_a_third(store):
    first, source1 = task(store, "s")
    second, source2 = task(store, "s", goal="查键盘物流", details={"order_id": "ORD-B"})
    started1, started2, release = threading.Event(), threading.Event(), threading.Event()
    def delayed(started):
        def call(_):
            started.set()
            assert release.wait(8)
            return response(calls=[("get_order", {"order_id": "ORD-A"})])
        return call
    first_client = Client(delayed(started1), response("请补充换货原因。"))
    second_client = Client(delayed(started2))
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(engine(first_client).execute_task, store, "alice", request(first, source1),
                        tools=lambda *a: pytest.fail("old plan executed"))
        b = pool.submit(engine(second_client).execute_task, store, "alice", request(second, source2),
                        tools=lambda *a: pytest.fail("cancelled matter executed"))
        try:
            assert started1.wait(5) and started2.wait(5)
            batch, svc = batch_for(store, "耳机改成换货，键盘不用查了，鼠标能开发票吗"), service(store)
            amend = update(batch, first, effect="amend")
            amend["goal"] = "耳机换货"
            svc.commit(batch, svc.snapshot(batch), proposal(amend, update(batch, second, effect="cancel"),
                new(batch, "咨询鼠标能否开票")))
        finally:
            release.set()
        assert a.result(timeout=10)["state"] == "ready"
        assert b.result(timeout=10)["state"] == "interrupted"
    assert len(TaskRepository(store, "alice").list(include_closed=True)) == 3


def test_replan_does_not_reset_model_budget(store):
    original, source = task(store, "s")
    def change(_):
        batch, svc = batch_for(store, "改成换货"), service(store)
        decision = update(batch, original, effect="amend")
        decision["goal"] = "耳机换货"
        svc.commit(batch, svc.snapshot(batch), proposal(decision))
        return response("旧回复")
    client = Client(change)
    result = engine(client).execute_task(store, "alice", request(original, source, model_calls=1))
    assert result["state"] == "deferred" and result["reason"] == "model_call_budget"
    assert len(client.requests) == 1


def test_active_job_absorbs_automatic_queued_version(store):
    original, source = task(store, "s")
    def change(_):
        batch, svc = batch_for(store, "改成换货"), service(store)
        decision = update(batch, original, effect="amend")
        decision["goal"] = "耳机换货"
        svc.commit(batch, svc.snapshot(batch), proposal(decision))
        current = svc.repo.get(original.task_id)
        TaskSchedule(store, "alice").enqueue(request(current, source))
        return response("旧回复")
    result = engine(Client(change, response("请补充换货原因。"))).execute_task(store, "alice", request(original, source))
    with store.transaction(read_only=True) as tx:
        statuses = [r["status"] for r in tx.all("SELECT status FROM runtime_jobs WHERE kind='task_execution'")]
    assert result["state"] == "ready" and sorted(statuses) == ["completed", "superseded"]


def test_worker_recovery_preserves_consumed_budget(store):
    from app.tasks.execution_updates import save_usage
    original, source = task(store, "s")
    req = TaskExecutionInput.model_validate(request(original, source, model_calls=3))
    TaskSchedule(store, "alice").enqueue(req)
    runs = TaskRuns(store, "alice")
    reservation, _ = runs.begin(req)
    save_usage(runs, req, reservation["payload"], {"model_calls": 2, "tool_calls": 1, "progress_calls": 0})
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (req.job_id,))
    worker = TaskWorker(store, engine(Client(response("请补充原因。"))))
    try:
        assert worker.recover() == 1
    finally:
        worker.close()
    with store.transaction(read_only=True) as tx:
        recovered = tx.one("SELECT payload FROM runtime_jobs WHERE kind='task_execution' AND status='pending'")
    payload = json.loads(recovered["payload"])
    assert payload["usage"]["model_calls"] == 2 and payload["usage"]["tool_calls"] == 1
    resumed = TaskExecutionInput.model_validate(payload["request"])
    result = engine(Client(response("请补充原因。"))).execute_task(store, "alice", resumed)
    assert result["model_calls"] == 3


def test_noop_update_preserves_version_and_links_new_message(store):
    original, _ = task(store, "s")
    batch, svc = batch_for(store, "还是给耳机退货"), service(store)
    svc.commit(batch, svc.snapshot(batch), proposal(update(batch, original)))
    assert svc.repo.get(original.task_id).version == original.version
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT task_id FROM support_task_messages WHERE task_id=? AND sequence=?",
                      (original.task_id, batch.messages[0]["sequence"]))


def test_supplement_before_question_publication_reopens_the_matter(store):
    from tests.test_task_candidates import ask
    original, source = task(store, "s")
    waiting = ask(store, original, source, publish=False)
    batch, svc = batch_for(store, "左耳没声音"), service(store)
    svc.commit(batch, svc.snapshot(batch), proposal(update(batch, waiting, details={"reason": "左耳没声音"})))
    current = svc.repo.get(original.task_id)
    assert current.status == "in_progress" and current.waiting_for is None


def test_rewrite_cannot_supply_fabricated_evidence(store):
    batch, svc = batch_for(store, "给那个退款"), service(store)
    def associate(context):
        return proposal(new(batch, details={"order_id": "ORD-FABRICATED"}, text="ORD-FABRICATED"))
    result = svc.process(batch, SimpleNamespace(rewrite=lambda _: "给订单 ORD-FABRICATED 退款", associate=associate))
    assert result["clarification"] and not svc.repo.list()


def test_late_read_after_timeout_is_audited_but_never_reused(store):
    original, source = task(store, "s")
    started, release, saved = threading.Event(), threading.Event(), threading.Event()
    original_save = TaskRuns.save_late_tool
    # Observe the committed receipt, not just the tool's return signal.
    def late_save(self, *args, **kwargs):
        value = original_save(self, *args, **kwargs)
        saved.set()
        return value
    def tool(name, arguments):
        started.set()
        assert release.wait(10)
        return {"success": True, "data": {"status": "shipped"}}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TaskRuns, "save_late_tool", late_save)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(engine(Client(response(calls=[("get_order", {"order_id": "ORD-A"})]))).execute_task,
                store, "alice", request(original, source, call_seconds=1), tools=tool)
            try:
                assert started.wait(3)
                assert future.result(timeout=5)["state"] == "deferred"
            finally:
                release.set()
            assert saved.wait(3)
    assert not TaskRuns(store, "alice").evidence(original)


def test_next_batch_rewrites_during_previous_association_but_associates_after_it(store, monkeypatch):
    from app.config.settings import settings
    from app.tasks.conversation_worker import ConversationWorker
    monkeypatch.setattr(settings, "query_rewrite_prefetch", True)
    batch_a = batch_for(store, "耳机退货")
    associating_a, rewriting_b = threading.Event(), threading.Event()
    release_a, release_b = threading.Event(), threading.Event()
    rewrites, batches = [], {"耳机退货": batch_a}
    def normalize(context):
        text = context.current_messages[0]["content"]
        rewrites.append(text)
        if text == "查键盘物流":
            rewriting_b.set()
            assert release_b.wait(10)
        return text
    def associate(context):
        text = context.current_messages[0]["content"]
        if text == "耳机退货":
            associating_a.set()
            assert release_a.wait(10)
        else:
            assert any(c["goal"] == "耳机退货" for c in context.candidates)
        return proposal(new(batches[text], text))
    worker = ConversationWorker(store, SimpleNamespace(rewrite=normalize, associate=associate), concurrency=2)
    intake = MessageIntake(store)
    try:
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(worker._associate, batch_a)
            try:
                assert associating_a.wait(5)
                intake.receive("s", "alice", request_id=str(uuid4()), content="查键盘物流")
                worker.rewrite_ahead.tick(job_ids=set())
                assert not rewriting_b.is_set()
                worker.rewrite_ahead.tick(job_ids={batch_a.job_id})
                assert rewriting_b.wait(5) and not first.done()
                # An additional message remains for a later immutable batch.
                intake.receive("s", "alice", request_id=str(uuid4()), content="鼠标能开票吗")
            finally:
                release_a.set()
                release_b.set()
            first.result(timeout=10)
        batch_b = intake.claim_next(batch_limits=worker.rewrite_ahead.limits())
        assert [m["content"] for m in batch_b.messages] == ["查键盘物流"]
        batches["查键盘物流"] = batch_b
        worker._associate(batch_b, worker.rewrite_ahead.take(batch_b))
        assert rewrites == ["耳机退货", "查键盘物流"]
        assert intake.claim_next().messages[0]["content"] == "鼠标能开票吗"
    finally:
        release_a.set()
        release_b.set()
        worker.close()
