from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.engine import AgentEngine
from app.harness.bounded_call import BoundedCalls, CallCapacityExceeded
from app.support.store import SupportError, SupportStore
from app.tasks.execution import TaskExecutor
from app.tasks.repository import TaskRepository
from app.tools.request_context import get_current_user_id
from tests.test_task_candidates import task
from tests.test_message_association import store
from tests.test_task_storage import empty_store, message


def response(content=None, calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, tool_calls=[SimpleNamespace(id=str(uuid4()), function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                                   for name, args in calls]))])


class Client:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        assert get_current_user_id() == "alice"
        self.requests.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value(kwargs) if callable(value) else value


def request(record, source, **budget):
    return dict(job_id=str(uuid4()), session_id="s", task_id=record.task_id,
                expected_version=record.version, source=source, budget=budget)


def engine(client):
    return AgentEngine("others", client=client, model="test-task-model")


def order_result(name, args):
    assert get_current_user_id() == "alice"
    assert name == "query_order" and args == {"order_id": "ORD-A"}
    return {"success": True, "order": {"order_id": "ORD-A", "status": "已发货"}}


@pytest.fixture(autouse=True)
def local_budgets(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "api_concurrency_redis_url", "")


@pytest.mark.parametrize("action", ["apply_refund", "text_to_sql", "run_shell"])
def test_unavailable_tools_cannot_run_even_when_model_requests_them(store, action):
    record, source = task(store, "s")
    result = engine(Client(response(calls=[(action, {"order_id": "ORD-A", "confirmed": True})]))).execute_task(
        store, "alice", request(record, source), tools=lambda *a: pytest.fail("unauthorized tool ran"))
    assert result["state"] == "needs_human"


def test_cross_session_handoff_stops_the_model_result(store):
    record, source = task(store, "old")
    def transfer(_):
        store.handoff("old", "alice")
        return response("可以继续。")
    result = engine(Client(transfer)).execute_task(store, "alice", request(record, source))
    assert result["state"] == "interrupted" and result["reply"] == ""


def test_only_one_effective_execution_for_a_task(store):
    record, source = task(store, "s")
    started, release = threading.Event(), threading.Event()
    def wait(_):
        started.set()
        release.wait(5)
        return response("已记录。")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(engine(Client(wait)).execute_task, store, "alice", request(record, source))
        try:
            assert started.wait(3)
            with pytest.raises(SupportError, match="已有有效执行"):
                engine(Client()).execute_task(store, "alice", request(record, source))
        finally:
            release.set()
        assert future.result()["state"] == "ready"


def test_timed_out_call_holds_capacity_until_real_completion():
    calls = BoundedCalls(1)
    release = threading.Event()
    with pytest.raises(TimeoutError):
        calls.run(lambda: release.wait(3), 0.02)
    try:
        with pytest.raises(CallCapacityExceeded):
            calls.run(lambda: "should not run", 1)
    finally:
        release.set()


def test_unrelated_source_or_foreign_user_cannot_start_execution(store):
    record, source = task(store, "s")
    with pytest.raises(SupportError):
        engine(Client()).execute_task(store, "bob", request(record, source))
    unrelated = message(store, "s", "alice", "另一件事")
    with pytest.raises(SupportError, match="未关联"):
        engine(Client()).execute_task(store, "alice", request(record, unrelated))
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs")["n"] == 0


def test_expired_execution_is_not_silently_replayed_after_restart(store):
    from app.tasks.execution_models import TaskExecutionInput
    from app.tasks.execution_store import TaskRuns
    record, source = task(store, "s")
    req = request(record, source)
    TaskRuns(store, "alice").begin(TaskExecutionInput.model_validate(req))
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0")
    result = engine(Client()).execute_task(SupportStore(store.dsn), "alice", req)
    assert result["state"] == "interrupted" and result["reason"] == "expired_execution"
    # A deliberate new read run is possible; the old job ID stays terminal.
    fresh = engine(Client(response("请补充当前需求。"))).execute_task(store, "alice", request(record, source))
    assert fresh["state"] == "ready"


def test_task_execution_and_replay_respect_trusted_channel_scope(store):
    from app.tasks.candidates import BusinessScope
    record, source = task(store, "s", details={"order_id": "ORD-A", "store_id": "shop-a"})
    req = request(record, source)
    with pytest.raises(SupportError):
        engine(Client()).execute_task(store, "alice", req, scope=BusinessScope(store_id="shop-b"))
    engine(Client(response("请补充具体需求。"))).execute_task(store, "alice", req, scope=BusinessScope(store_id="shop-a"))
    with pytest.raises(SupportError):
        engine(Client()).execute_task(store, "alice", req, scope=BusinessScope(store_id="shop-b"))
