"""Task scheduling contracts, on SQLite and opt-in isolated PostgreSQL/Redis."""
from concurrent.futures import ProcessPoolExecutor
import json
from multiprocessing import get_context
import os
import subprocess
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.runtime.task_queue import TaskQueue
from app.support.store import SupportError, SupportStore
from app.tasks.candidates import BusinessScope
from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.repository import TaskRepository
from app.tasks.scheduling import Dependency, ScheduleBlocked, TaskSchedule, TaskWorker
from tests.test_task_candidates import store, task
from tests.test_task_execution import engine, request, response, Client, local_budgets
from tests.test_task_storage import empty_store, message


class QueryClient:
    """Stateless fake provider, safe to share across simultaneous Agent runs."""
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **params):
        if any(m["role"] == "tool" for m in params["messages"]):
            return response("已保存订单查询结果。")
        content = json.loads(next(m["content"] for m in params["messages"] if m["role"] == "user"))
        return response(calls=[("query_order", {"order_id": content["task"]["details"]["order_id"]})])


def result(name, args):
    return {"success": True, "order": {"order_id": args["order_id"], "status": "已发货", "stock": 1}}


def drain(worker, ids, user="alice", timeout=10):
    deadline = time.monotonic() + timeout
    schedule = TaskSchedule(worker.store, user)
    while time.monotonic() < deadline:
        worker.tick()
        rows = [schedule.get(jid) for jid in ids]
        if all(r["status"] in {"completed", "interrupted", "expired", "superseded"} for r in rows):
            return rows
        time.sleep(.02)
    raise AssertionError("Tasks did not finish: " + repr(rows))


def enqueue(store, record, source, **kwargs):
    return TaskSchedule(store, record.user_id).enqueue(request(record, source), **kwargs)


def test_fast_and_slow_start_together_in_same_session(store):
    slow, ss = task(store, "s")
    fast, fs = task(store, "s", details={"order_id": "ORD-B"})
    barrier, release, started = threading.Barrier(2), threading.Event(), threading.Event()
    def tool(name, args):
        barrier.wait(timeout=5)
        started.set()
        if args["order_id"] == "ORD-A":
            assert release.wait(5)
        return result(name, args)
    a, b = enqueue(store, slow, ss), enqueue(store, fast, fs)
    worker = TaskWorker(store, engine(QueryClient()), tools=tool, concurrency=2)
    try:
        worker.tick()
        assert started.wait(5)
        assert drain(worker, [b])[0]["result"]["state"] == "ready"
        assert TaskSchedule(store, "alice").get(a)["status"] == "running"
    finally:
        release.set()
        worker.close()
    assert TaskSchedule(store, "alice").get(a)["result"]["state"] == "ready"
    assert len(store.public_history("s", "alice")) == 2


def test_cross_session_wakeups_reuse_one_version_and_reject_new_conditions(store):
    record, source = task(store, "s")
    service = TaskSchedule(store, "alice")
    first = request(record, source)
    jid = service.enqueue(first)
    again = {**request(record, source), "session_id": "old"}
    assert service.enqueue(again) == jid
    assert service.enqueue(first) == jid
    with pytest.raises(SupportError):
        service.enqueue({**first, "budget": {"model_calls": 1}})
    with pytest.raises(SupportError):
        TaskSchedule(store, "bob").get(jid)
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE kind='task_execution'")["n"] == 1


def test_duplicate_wakeup_after_completion_returns_same_outcome(store):
    record, source = task(store, "s")
    jid = enqueue(store, record, source)
    worker = TaskWorker(store, engine(QueryClient()), tools=result)
    try:
        drain(worker, [jid])
        assert enqueue(store, record, source) == jid
        worker.tick()
        with store.transaction(read_only=True) as tx:
            assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs")["n"] == 1
    finally:
        worker.close()


@pytest.mark.parametrize("dimension", ["user", "tenant", "global"])
def test_database_quotas_are_shared_between_workers_and_release_on_finish(store, monkeypatch, dimension):
    monkeypatch.setattr(settings, "task_global_concurrency", 1 if dimension == "global" else 8)
    monkeypatch.setattr(settings, "task_user_concurrency", 1 if dimension == "user" else 8)
    monkeypatch.setattr(settings, "api_tenant_agent_concurrency", 1 if dimension == "tenant" else 8)
    monkeypatch.setattr(settings, "api_user_tenants", {"alice": "tenant", "bob": "other" if dimension == "global" else "tenant"})
    first, fs = task(store, "s")
    second, ss = task(store, "s" if dimension == "user" else "other", user="alice" if dimension == "user" else "bob")
    req1, req2 = request(first, fs), request(second, ss)
    req2["session_id"] = ss["session_id"]
    TaskSchedule(store, first.user_id).enqueue(req1)
    TaskSchedule(store, second.user_id).enqueue(req2)
    runs1, runs2 = TaskRuns(store, first.user_id), TaskRuns(store, second.user_id)
    reserved, _ = runs1.begin(TaskExecutionInput.model_validate(req1))
    with pytest.raises(ScheduleBlocked, match="execution_capacity"):
        runs2.begin(TaskExecutionInput.model_validate(req2))
    runs1.finish(TaskExecutionInput.model_validate(req1), reserved["payload"], {"state": "ready"})
    assert runs2.begin(TaskExecutionInput.model_validate(req2))[0]


def test_waiting_tasks_take_no_slots_and_backlog_is_bounded(store, monkeypatch):
    monkeypatch.setattr(settings, "api_chat_queue_max_depth", 1)
    for status in ("waiting_user", "waiting_external"):
        record, source = task(store, "s", status=status)
        jid = enqueue(store, record, source)
        assert TaskSchedule(store, "alice").get(jid)["result"]["state"] == status
    record, source = task(store, "s")
    enqueue(store, record, source)
    another, src = task(store, "s")
    with pytest.raises(SupportError, match="容量"):
        enqueue(store, another, src)


def test_new_version_supersedes_pending_but_unrelated_task_continues(store):
    record, source = task(store, "s")
    other, src = task(store, "s", details={"order_id": "ORD-B"})
    old, other_job = enqueue(store, record, source), enqueue(store, other, src)
    updated = TaskRepository(store, "alice").update(record.task_id, "s", expected_version=1, change_id="edit",
        source=source, patch={"details": {"order_id": "ORD-C"}})
    new = enqueue(store, updated, source)
    worker = TaskWorker(store, engine(QueryClient()), tools=result)
    try:
        rows = drain(worker, [old, new, other_job])
        assert [r["status"] for r in rows] == ["superseded", "completed", "completed"]
    finally:
        worker.close()


@pytest.mark.parametrize("edit", ["reason", "order_id"])
def test_applicable_inflight_read_is_reused_only_for_same_object(store, edit):
    record, source = task(store, "s")
    entered, release, calls = threading.Event(), threading.Event(), []
    def tool(name, args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return result(name, args)
    old = enqueue(store, record, source)
    worker = TaskWorker(store, engine(QueryClient()), tools=tool)
    try:
        worker.tick()
        assert entered.wait(5)
        updated = TaskRepository(store, "alice").update(record.task_id, "old", expected_version=1, change_id="edit",
            source=source, patch={"details": {**record.details, edit: "ORD-B" if edit == "order_id" else "包装破损"}})
        new = enqueue(store, updated, source)
        release.set()
        rows = drain(worker, [old, new])
        assert rows[0]["result"]["state"] == "interrupted"
        assert rows[1]["result"]["state"] == "ready"
        assert len(calls) == (1 if edit == "reason" else 2)
        with store.transaction(read_only=True) as tx:
            evidence = [json.loads(r["payload"]) for r in tx.all("SELECT payload FROM support_events WHERE kind='task_tool_result'")]
        assert len(evidence) == 1
        assert evidence[0]["object"]["order_id"] == ("ORD-A" if edit == "reason" else "ORD-B")
        assert evidence[0]["task_version"] == (1 if edit == "reason" else 2)
    finally:
        release.set()
        worker.close()


def test_recovery_fences_late_worker_and_reuses_saved_read(store):
    record, source = task(store, "s")
    req = request(record, source)
    service = TaskSchedule(store, "alice")
    service.enqueue(req)
    request_model = TaskExecutionInput.model_validate(req)
    runs = TaskRuns(store, "alice")
    reserved, _ = runs.begin(request_model)
    runs.save_tool(request_model, reserved["payload"], 1, "query_order", {"order_id": "ORD-A"},
                   result("query_order", {"order_id": "ORD-A"}))
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (req["job_id"],))
    worker = TaskWorker(store, engine(QueryClient()), tools=lambda *_: pytest.fail("fresh read must be reused"))
    try:
        assert worker.recover() == 1
        assert worker.recover() == 0
        with pytest.raises(SupportError):
            runs.save_tool(request_model, reserved["payload"], 2, "query_order", {}, {"success": True})
        with store.transaction(read_only=True) as tx:
            child = tx.one("SELECT job_id FROM runtime_jobs WHERE status='pending'")["job_id"]
        rows = drain(worker, [child])
        assert rows[0]["result"]["state"] == "ready"
        assert runs.finish(request_model, reserved["payload"], {"reply": "late"}, interrupted=True)["reply"] == ""
    finally:
        worker.close()


def test_stock_condition_selects_only_verified_branch_and_records_shared_resources(store):
    parent, ps = task(store, "s", goal="有货就换，否则退")
    exchange, es = task(store, "s", goal="换货")
    refund, rs = task(store, "s", goal="退款")
    p = enqueue(store, parent, ps)
    dep = dict(job_id=p, kind="query", tool="query_order", field="order.stock")
    yes = enqueue(store, exchange, es, dependencies=[{**dep, "compare": "gt", "value": 0}])
    no = enqueue(store, refund, rs, dependencies=[{**dep, "value": 0}])
    worker = TaskWorker(store, engine(QueryClient()), tools=result)
    try:
        assert drain(worker, [p, yes])[1]["result"]["state"] == "ready"
        worker.tick()
        assert TaskSchedule(store, "alice").get(no)["status"] == "pending"
        with store.transaction(read_only=True) as tx:
            rows = tx.all("SELECT payload FROM runtime_jobs")
        resources = [json.loads(r["payload"])["schedule"]["resources"] for r in rows]
        assert resources[0] == resources[1] == resources[2]
    finally:
        worker.close()


def test_address_then_resend_requires_real_operation_receipt(store):
    parent, ps = task(store, "s", goal="先改地址")
    resend, rs = task(store, "s", goal="改地址后补发")
    p = enqueue(store, parent, ps)
    child = enqueue(store, resend, rs, dependencies=[dict(job_id=p, kind="operation", operation_id="not-a-receipt")])
    worker = TaskWorker(store, engine(QueryClient()), tools=result)
    try:
        drain(worker, [p])
        worker.tick()
        assert TaskSchedule(store, "alice").get(child)["status"] == "pending"
        with store.transaction(read_only=True) as tx:
            assert tx.one("SELECT COUNT(*) AS n FROM support_operations")["n"] == 0
    finally:
        worker.close()


def test_changed_parent_and_foreign_dependency_cannot_authorize_child(store):
    parent, ps = task(store, "s")
    child, cs = task(store, "s")
    p = enqueue(store, parent, ps)
    worker = TaskWorker(store, engine(QueryClient()), tools=result)
    try:
        drain(worker, [p])
        TaskRepository(store, "alice").update(parent.task_id, "s", expected_version=1, change_id="change",
            source=ps, patch={"details": {"order_id": "ORD-CHANGED"}})
        c = enqueue(store, child, cs, dependencies=[dict(job_id=p, kind="query", tool="query_order", field="order.stock", value=1)])
        worker.tick()
        assert TaskSchedule(store, "alice").get(c)["reason"] == "dependency_changed"
        foreign, fs = task(store, "other", user="bob")
        req = {**request(foreign, fs), "session_id": "other"}
        with pytest.raises(SupportError):
            TaskSchedule(store, "bob").enqueue(req, dependencies=[dict(job_id=p, kind="operation", operation_id="fake")])
    finally:
        worker.close()


def test_dependency_changed_during_child_read_blocks_its_evidence(store):
    parent, ps = task(store, "s")
    child, cs = task(store, "old")
    p = enqueue(store, parent, ps)
    worker = TaskWorker(store, engine(QueryClient()), tools=result)
    try:
        drain(worker, [p])
    finally:
        worker.close()
    request_child = {**request(child, cs), "session_id": "old"}
    c = TaskSchedule(store, "alice").enqueue(request_child, dependencies=[
        dict(job_id=p, kind="query", tool="query_order", field="order.stock", value=1)])
    def changed(name, args):
        TaskRepository(store, "alice").update(parent.task_id, "s", expected_version=1, change_id="changed-parent",
            source=ps, patch={"details": {"order_id": "ORD-B"}})
        return result(name, args)
    worker = TaskWorker(store, engine(QueryClient()), tools=changed)
    try:
        assert drain(worker, [c])[0]["result"]["state"] == "interrupted"
        with store.transaction(read_only=True) as tx:
            assert tx.one("SELECT COUNT(*) AS n FROM support_events WHERE kind='task_tool_result'")["n"] == 1
    finally:
        worker.close()


def test_recovery_stops_at_unknown_business_result_and_hands_off(store):
    record, source = task(store, "s")
    req = request(record, source)
    TaskSchedule(store, "alice").enqueue(req)
    TaskRuns(store, "alice").begin(TaskExecutionInput.model_validate(req))
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (req["job_id"],))
        tx.execute("INSERT INTO support_operations VALUES ('op','s',0,'refund','unknown','now',NULL)")
        tx.execute("INSERT INTO support_operation_tasks VALUES ('op',?)", (record.task_id,))
    worker = TaskWorker(store, engine(Client()))
    try:
        assert worker.recover() == 1
        with store.transaction(read_only=True) as tx:
            assert tx.one("SELECT mode FROM support_conversations WHERE session_id='s'")["mode"] == "handoff_pending"
            assert tx.one("SELECT status FROM support_tickets WHERE session_id='s'")["status"] == "queued"
            assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status='pending'")["n"] == 0
            assert tx.one("SELECT status FROM support_operations WHERE operation_id='op'")["status"] == "unknown"
    finally:
        worker.close()


def test_recovery_is_bounded_and_does_not_bypass_reset(store):
    record, source = task(store, "s")
    req = TaskExecutionInput.model_validate(request(record, source))
    TaskSchedule(store, "alice").enqueue(req)
    worker = TaskWorker(store, engine(Client()))
    try:
        for index in range(3):
            TaskRuns(store, "alice").begin(req)
            with store.transaction() as tx:
                tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (req.job_id,))
            assert worker.recover() == 1
            with store.transaction(read_only=True) as tx:
                pending = tx.one("SELECT payload FROM runtime_jobs WHERE status='pending'")
            if index < 2:
                req = TaskExecutionInput.model_validate(json.loads(pending["payload"])["request"])
            else:
                assert pending is None
        other, src = task(store, "old")
        req = TaskExecutionInput.model_validate({**request(other, src), "session_id": "old"})
        TaskSchedule(store, "alice").enqueue(req)
        TaskRuns(store, "alice").begin(req)
        with store.transaction() as tx:
            tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (req.job_id,))
            tx.execute("UPDATE support_conversations SET control_version=control_version+1 WHERE session_id='old'")
        worker.recover()
        with store.transaction(read_only=True) as tx:
            assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status='pending'")["n"] == 0
    finally:
        worker.close()


def test_scoped_receipt_does_not_follow_task_to_another_store(store):
    record, source = task(store, "s", details={"order_id": "ORD-A", "platform_code": "p", "store_id": "a"})
    jid = enqueue(store, record, source)
    TaskRepository(store, "alice").update(record.task_id, "s", expected_version=1, change_id="move",
        source=source, patch={"details": {"order_id": "ORD-B", "platform_code": "p", "store_id": "b"}})
    with pytest.raises(SupportError):
        TaskSchedule(store, "alice", scope=BusinessScope(platform_code="p", store_id="b")).get(jid)


def test_no_forward_dependency_and_no_unknown_schema_field(store):
    record, source = task(store, "s")
    with pytest.raises(SupportError):
        enqueue(store, record, source, dependencies=[dict(job_id="missing", kind="operation", operation_id="x")])
    with pytest.raises(ValueError):
        Dependency.model_validate(dict(job_id="p", kind="query", tool="query_order", field="x();", value=True))


def test_control_change_stops_pending_work_and_queue_expiry_calls_no_model(store):
    record, source = task(store, "s")
    jid = enqueue(store, record, source)
    with store.transaction() as tx:
        tx.execute("UPDATE support_conversations SET control_version=control_version+1 WHERE session_id='s'")
    worker = TaskWorker(store, engine(Client()))
    try:
        worker.tick()
        assert TaskSchedule(store, "alice").get(jid)["status"] == "pending"
        with store.transaction() as tx:
            tx.execute("UPDATE runtime_jobs SET queue_deadline=0 WHERE job_id=?", (jid,))
        worker.tick()
        assert TaskSchedule(store, "alice").get(jid)["status"] == "expired"
    finally:
        worker.close()


@pytest.mark.parametrize("started", [False, True])
def test_cross_session_handoff_then_resume_does_not_revive_old_work(store, started):
    record, source = task(store, "s")
    record = TaskRepository(store, "alice").update(record.task_id, "old", expected_version=1, change_id="link",
        source=source, patch={"details": record.details})
    req = TaskExecutionInput.model_validate(request(record, source))
    TaskSchedule(store, "alice").enqueue(req)
    runs = TaskRuns(store, "alice")
    reservation = runs.begin(req)[0] if started else None
    with store.transaction() as tx:
        control = store._control(tx, "old", "alice")
        store._handoff(tx, control, "customer_requested", "alice")
    store.resume("old", "alice")
    with pytest.raises(SupportError, match="控制"):
        if started:
            runs.save_tool(req, reservation["payload"], 1, "query_order", {"order_id": "ORD-A"},
                           result("query_order", {"order_id": "ORD-A"}))
        else:
            runs.begin(req)
    if started:
        with store.transaction() as tx:
            tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (req.job_id,))
        worker = TaskWorker(store, engine(Client()))
        try:
            worker.recover()
            with store.transaction(read_only=True) as tx:
                assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status='pending'")["n"] == 0
        finally:
            worker.close()


def test_handoff_resume_also_invalidates_recent_query_cache(store):
    record, source = task(store, "s")
    calls = []
    def tool(name, args):
        calls.append(args)
        return result(name, args)
    agent = engine(QueryClient())
    assert agent.execute_task(store, "alice", request(record, source), tools=tool)["state"] == "ready"
    with store.transaction() as tx:
        store._handoff(tx, store._control(tx, "s", "alice"), "customer_requested", "alice")
    store.resume("s", "alice")
    assert agent.execute_task(store, "alice", request(record, source), tools=tool)["state"] == "ready"
    assert len(calls) == 2


def reserve_in_process(args):
    dsn, req = args
    store = SupportStore(dsn)
    try:
        return TaskRuns(store, "alice").begin(TaskExecutionInput.model_validate(req))[0] is not None
    except SupportError:
        return False


def reserve_with_one_global_slot(args):
    settings.task_global_concurrency = 1
    return reserve_in_process(args)


def exit_without_finish(args):
    dsn, req = args
    runs = TaskRuns(SupportStore(dsn), "alice")
    req = TaskExecutionInput.model_validate(req)
    reservation, _ = runs.begin(req)
    runs.save_tool(req, reservation["payload"], 1, "query_order", {"order_id": "ORD-A"},
                   result("query_order", {"order_id": "ORD-A"}))
    return True  # Process ends without finish; there is no live in-memory state.


def recover_in_process(args):
    dsn, old_id = args
    def no_repeat(*_):
        raise AssertionError("Saved successful read must be reused")
    worker = TaskWorker(SupportStore(dsn), engine(QueryClient()), tools=no_repeat)
    try:
        worker.recover()
        with worker.store.transaction(read_only=True) as tx:
            job = tx.one("SELECT job_id FROM runtime_jobs WHERE status='pending'")["job_id"]
        return drain(worker, [job])[0]
    finally:
        worker.close()


def test_separate_processes_cannot_both_reserve(store):
    record, source = task(store, "s")
    req = request(record, source)
    TaskSchedule(store, "alice").enqueue(req)
    with ProcessPoolExecutor(2, mp_context=get_context("spawn")) as pool:
        assert sum(pool.map(reserve_in_process, [(store.dsn, req)] * 2)) == 1


def test_new_process_recovers_read_after_previous_process_exited(store):
    record, source = task(store, "s")
    req = request(record, source)
    TaskSchedule(store, "alice").enqueue(req)
    with ProcessPoolExecutor(1, mp_context=get_context("spawn")) as pool:
        assert pool.submit(exit_without_finish, (store.dsn, req)).result(timeout=20)
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (req["job_id"],))
    with ProcessPoolExecutor(1, mp_context=get_context("spawn")) as pool:
        assert pool.submit(recover_in_process, (store.dsn, req["job_id"])).result(timeout=20)["result"]["state"] == "ready"


def test_global_limit_also_holds_between_processes_and_for_direct_entry(store):
    first, fs = task(store, "s")
    second, ss = task(store, "old")
    requests = [request(first, fs), {**request(second, ss), "session_id": "old"}]
    # No enqueue here: even a direct controlled call cannot bypass the DB quota.
    with ProcessPoolExecutor(2, mp_context=get_context("spawn")) as pool:
        assert sum(pool.map(reserve_with_one_global_slot, [(store.dsn, r) for r in requests])) == 1


@pytest.mark.skipif(os.environ.get("ECOM_INTEGRATION") != "1", reason="dedicated integration infrastructure only")
def test_real_redis_restart_loses_hints_but_not_outbox_or_active_slots(store):
    if not store.postgres:
        pytest.skip("Redis process test paired with isolated PostgreSQL")
    prefix = "itest:task-scheduler:" + uuid4().hex
    queue = TaskQueue("redis://127.0.0.1:26379/0", prefix=prefix, max_hints=2)
    record, source = task(store, "s")
    req = request(record, source)
    jid = TaskSchedule(store, "alice", queue=queue).enqueue(req)
    TaskRuns(store, "alice").begin(TaskExecutionInput.model_validate(req))
    for _ in range(5):
        queue.notify(jid)
    assert queue.redis.llen(queue.key) == 2
    # Only the dedicated compose service is restarted. No FLUSHDB or business DB.
    subprocess.run(["docker", "compose", "-p", "ecom-refactor-tests", "-f", "deploy/compose.integration.yml",
                    "restart", "redis"], check=True, capture_output=True, timeout=30)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if queue.redis.ping():
                break
        except Exception:
            pass
        time.sleep(.1)
    assert queue.take() is None
    with pytest.raises(SupportError, match="正在进行"):
        TaskRuns(store, "alice").begin(TaskExecutionInput.model_validate(req))
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?", (jid,))
    worker = TaskWorker(SupportStore(store.dsn), engine(QueryClient()), tools=result, queue=queue)
    try:
        worker.recover()
        with store.transaction(read_only=True) as tx:
            child = tx.one("SELECT job_id FROM runtime_jobs WHERE status='pending'")["job_id"]
        assert drain(worker, [child])[0]["result"]["state"] == "ready"
        for _ in range(3):
            queue.notify(jid)
            worker.tick()
        with store.transaction(read_only=True) as tx:
            assert tx.one("SELECT COUNT(*) AS n FROM support_events WHERE kind='task_tool_result'")["n"] == 1
    finally:
        worker.close()
        queue.redis.delete(queue.key)
