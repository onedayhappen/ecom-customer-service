import json
import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.observability import interactions as traces
from app.support.store import SCHEMA_VERSION, SupportStore
from app.tasks.replies import ReplyCoordinator
from tests.test_task_storage import empty_store


@pytest.fixture
def store(empty_store, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "observability_enabled", True)
    monkeypatch.setattr(settings, "interaction_trace_enabled", True)
    monkeypatch.setattr(settings, "observability_log_dir", str(tmp_path / "logs"))
    empty_store.ensure("s", "alice")
    empty_store.ensure("other", "bob")
    return empty_store


def send(store, text, sid="s", role="user", kind="message", key=None):
    with store.transaction() as tx:
        control = store._control(tx, sid)
        return store._event(tx, control, kind, control["user_id"] if role == "user" else "bot",
                            {"role": role, "content": text}, key=key)


def publish(store, text="请提供订单号", jobs=()):
    coordinator = ReplyCoordinator(store, "alice")
    with store.transaction() as tx:
        control = store._control(tx, "s", "alice")
        pieces = [dict(key="job:" + job, content=text) for job in jobs] or [dict(key=str(uuid4()), content=text)]
        return coordinator._publish(tx, control, pieces, text, control["sequence"], str(uuid4()))


def rows(sid="s"):
    return [json.loads(line) for line in traces.trace_path(sid).read_text(encoding="utf-8").splitlines()]


def test_continuous_messages_batches_and_progress_are_one_interaction(store):
    send(store, "我要退货")
    with traces.bind(store, "s", job_id="batch-a"):
        traces.record("association.candidates", output={"candidates": []})
    send(store, "昨天买的耳机")
    send(store, "正在处理", role="assistant", kind="task_processing")
    send(store, "左耳没声音")
    with traces.bind(store, "s", job_id="batch-b"):
        traces.record("association.proposal", output={"action": "update"})
    pending = rows()
    assert len(pending) == 1 and pending[0]["status"] == "processing"
    interaction_id = pending[0]["interaction_id"]
    reply = publish(store)
    result = rows()
    assert len(result) == 1 and result[0]["interaction_id"] == interaction_id
    assert [m["content"] for m in result[0]["user_messages"]] == ["我要退货", "昨天买的耳机", "左耳没声音"]
    assert result[0]["assistant_reply"]["event_id"] == reply["event_id"]
    assert result[0]["status"] == "completed"
    assert "task_processing" in [s["stage"] for s in result[0]["steps"]]
    send(store, "ORD-A")
    publish(store, "收到订单号")
    assert len(rows()) == 2
    assert [m["content"] for m in rows()[1]["user_messages"]] == ["ORD-A"]


def test_actual_final_reply_and_all_execution_outputs_are_logged(store):
    from tests.test_task_candidates import task
    from tests.test_task_execution import Client, engine, request, response
    target, source = task(store, "s")
    client = Client(response(calls=[("get_order", {"order_ref": "ref-a"})]), response("订单已发货。"))
    result = engine(client).execute_task(store, "alice", request(target, source),
        tools=lambda name, args: {"success": True, "data": {"order_id": "ORD-A", "status": "shipped"}})
    assert result["state"] == "ready"
    reply = ReplyCoordinator(store, "alice").publish("s")
    assert reply
    trace = rows()[0]
    assert trace["assistant_reply"]["content"] == reply["content"]
    stages = [step["stage"] for step in trace["steps"]]
    assert stages.count("model.started") == 2
    assert stages.count("model.finished") == 2
    assert {"tool.started", "tool.finished", "execution.saved", "react.result", "reply.published"} <= set(stages)
    tool = next(s for s in trace["steps"] if s["stage"] == "tool.finished")
    assert tool["data"]["output"]["data"]["order_id"] == "ORD-A"


def test_invalid_model_output_and_retries_survive_fallback(store):
    from app.agents.engine import AgentEngine
    from tests.test_message_association import batch_for, service
    from tests.test_task_execution import response
    batch = batch_for(store, "帮我退货")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response("not json"))))
    result = service(store).process(batch, AgentEngine("others", client=client, model="fake"))
    assert result["clarification"]
    trace = rows()[0]
    model = [s for s in trace["steps"] if s["stage"] == "model.finished"]
    assert len(model) == 3  # one plain-text rewrite, then two association attempts
    assert any(s["stage"] == "query_rewrite" for s in trace["steps"])
    assert model[0]["data"]["output"]["choices"][0]["content"] == "not json"
    assert len([s for s in trace["errors"] if s["stage"] == "association.validation"]) == 2
    assert any(s["stage"] == "association.fallback" for s in trace["steps"])


def test_rollback_and_replayed_message_never_add_a_log_line(store):
    send(store, "退款", key="same")
    send(store, "退款", key="same")
    assert len(rows()[0]["user_messages"]) == 1
    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            control = store._control(tx, "s")
            store._event(tx, control, "message", "bot", {"role": "assistant", "content": "未提交回复"})
            raise RuntimeError("rollback")
    assert rows()[0]["assistant_reply"] is None
    assert "未提交回复" not in traces.trace_path("s").read_text(encoding="utf-8")


def test_file_failure_keeps_journal_and_rebuild_is_idempotent(store, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(traces.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")))
        send(store, "我要退货")
        publish(store)
    assert not traces.trace_path("s").exists()
    restarted = SupportStore(store.dsn)
    traces.flush_pending(restarted)
    first = traces.trace_path("s").read_bytes()
    traces.export_session(restarted, "s")
    assert traces.trace_path("s").read_bytes() == first
    assert len(rows()) == 1 and rows()[0]["status"] == "completed"


def test_journal_failure_does_not_rollback_customer_message(store, monkeypatch):
    with monkeypatch.context() as patch:
        def failed(tx, *args):
            tx.execute("SELECT * FROM nonexistent_trace_table")
        patch.setattr(traces, "_insert", failed)
        sequence = send(store, "日志故障不能影响接收")
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT payload FROM support_events WHERE session_id='s' AND sequence=?", (sequence,))


def test_failure_without_reply_and_stale_process_remain_visible(store):
    send(store, "查询订单")
    with traces.bind(store, "s"):
        with pytest.raises(TimeoutError), traces.span("model", request={"messages": []}):
            raise TimeoutError("upstream timeout")
    assert rows()[0]["status"] == "failed"
    assert rows()[0]["assistant_reply"] is None
    assert rows()[0]["errors"][0]["data"]["error"]["type"] == "TimeoutError"
    traces.record("execution.started", store=store, session_id="s", status="running")
    with store.transaction() as tx:
        tx.execute("UPDATE interaction_trace_events SET created_at='2020-01-01T00:00:00+00:00' WHERE session_id='s'")
    traces.export_session(store, "s")
    assert rows()[0]["status"] == "interrupted"


def test_late_model_completion_does_not_erase_failed_execution(store):
    send(store, "查询订单")
    with traces.bind(store, "s", job_id="expired-job"):
        traces.record("execution.finished", status="error", output={"state": "deferred"})
        traces.record("model.finished", output={"content": "late response"})
    assert rows()[0]["status"] == "failed"
    assert len(rows()[0]["errors"]) == 1


def test_nested_sensitive_fields_redacted_without_losing_tool_name(store):
    send(store, "电话13800138000")
    secret = "private-confirmation-value"
    data = {"tool_calls": [{"name": "prepare_refund", "arguments": json.dumps({"confirmation_token": secret,
            "address": "具体住址", "phone": "13800138000"})}], "content": json.dumps({"field": "address", "value": "具体住址"})}
    traces.record("model.finished", store=store, session_id="s", output=data)
    text = traces.trace_path("s").read_text(encoding="utf-8")
    assert secret not in text and "具体住址" not in text and "13800138000" not in text
    assert "prepare_refund" in text
    with store.transaction(read_only=True) as tx:
        assert secret not in str(tx.all("SELECT payload FROM interaction_trace_events"))


def test_concurrent_sessions_and_followup_replies_keep_correct_causality(store):
    send(store, "两笔订单都查一下")
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda n: traces.record("model.finished", store=store, session_id="s" if n % 2 else "other",
                                              job_id="job-a" if n % 2 else "job-b", output={"index": n}), range(12)))
    traces.flush_pending(store)
    publish(store, "第一笔结果", jobs=["job-a"])
    publish(store, "第二笔结果", jobs=["job-a"])
    result = rows()
    assert len(result) == 2 and result[1]["user_messages"] == []
    assert len([s for s in result[1]["steps"] if s["stage"] == "model.finished"]) == 6
    assert all(s["job_id"] != "job-b" for r in result for s in r["steps"])
    assert traces.trace_path("../outside").parent.name == "conversations"


def test_disabled_collection_creates_no_trace(store, monkeypatch):
    monkeypatch.setattr(settings, "interaction_trace_enabled", False)
    send(store, "不采集")
    traces.record("model.finished", store=store, session_id="s", output="不采集")
    assert not traces.trace_path("s").exists()


def _process_observations(dsn, directory, index):
    settings.observability_enabled = settings.interaction_trace_enabled = True
    settings.observability_log_dir = directory
    worker_store = SupportStore(dsn)
    for n in range(4):
        traces.record("process.step", store=worker_store, session_id="s", output={"process": index, "step": n})


def test_process_exporters_do_not_lose_or_duplicate_events(store):
    send(store, "并发采集")
    with ProcessPoolExecutor(2, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(_process_observations, store.dsn, settings.observability_log_dir, i) for i in range(2)]
        for future in futures:
            future.result(timeout=30)
    traces.export_session(store, "s")
    steps = [s for s in rows()[0]["steps"] if s["stage"] == "process.step"]
    assert len(steps) == 8 and len({s["event_id"] for s in steps}) == 8


def test_schema_22_upgrade_preserves_business_events(store):
    sequence = send(store, "升级前消息")
    with store.transaction() as tx:
        tx.execute("DROP TABLE interaction_trace_events")
        tx.execute("DROP TABLE interaction_trace_sessions")
        # Simulate version 22 even when the application has newer migrations.
        tx.execute("DELETE FROM support_schema_versions WHERE version>22")
        tx.execute("INSERT INTO support_schema_versions VALUES (22,'2026-10-25') ON CONFLICT DO NOTHING")
    restarted = SupportStore(store.dsn)
    restarted.initialize()
    with restarted.transaction(read_only=True) as tx:
        assert tx.one("SELECT payload FROM support_events WHERE session_id='s' AND sequence=?", (sequence,))
        assert tx.one("SELECT COUNT(*) AS n FROM interaction_trace_events")["n"] == 0
        assert tx.one("SELECT MAX(version) AS v FROM support_schema_versions")["v"] == SCHEMA_VERSION
    send(restarted, "升级后消息")
    assert rows()[0]["user_messages"][0]["content"] == "升级后消息"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unified_model_client_records_actual_request_and_output(store, asynchronous):
    from app.observability.llm_client import LlmClient
    from tests.test_task_execution import response
    send(store, "你好")
    sent = []
    def create(**params):
        sent.append(params)
        return response("模型返回内容")
    async def acreate(**params):
        return create(**params)
    raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=acreate if asynchronous else create)))
    client = LlmClient(raw_client=raw, model="trace-test")
    with traces.bind(store, "s", job_id="model-job"):
        params = {"messages": [{"role": "user", "content": "你好"}], "max_tokens": 128}
        if asynchronous:
            asyncio.run(client.chat.completions.acreate(**params))
        else:
            client.chat.completions.create(**params)
    steps = rows()[0]["steps"]
    assert len([s for s in steps if s["stage"] == "model.started"]) == 1
    request = next(s for s in steps if s["stage"] == "model.request")
    output = next(s for s in steps if s["stage"] == "model.finished")
    assert request["data"]["input"] == traces.sanitize(sent[0])
    assert request["data"]["call_id"] == output["data"]["call_id"]
    assert output["data"]["output"]["choices"][0]["content"] == "模型返回内容"
