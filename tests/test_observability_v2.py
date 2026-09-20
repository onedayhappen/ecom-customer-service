"""第12期埋点体系测试：Trace/Span 核心、LLM/工具统一客户端、FSM 观测、审计抽样、RT 聚合。

覆盖点：
- span 树（parent 关系 / cost_ms / status / 内存中构建）
- LlmClient：场景 token 分开统计、OpenAI 兼容接口、异常 force 审计
- McpToolClient：入参脱敏、错误记录 + re-raise
- fsm_state_observation 装饰器 / BaseStateMachine 基类 / StateObserver 阶段视图
- AuditLogger 抽样：异常 100%、正常按抽样率
- RT 阶段聚合（rewrite / llm_generate / rag / tool）
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.observability import audit
from app.observability.audit import AuditLogger, PostgresAuditSink
from app.observability.fsm import (
    BaseStateMachine,
    StateObserver,
    fsm_state_observation,
)
from app.observability.llm_client import LLMCallResult, LlmClient
from app.observability.mcp_client import McpToolClient, mask_sensitive
from app.observability.rt import aggregate_stages
from app.observability.trace import SpanKind, TraceManager


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


class _FakeRawCompletions:
    """模拟 OpenAI chat.completions.create（可注入异常）。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            model=kwargs.get("model", "test-model"),
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好", tool_calls=[]))],
        )


class _FakeRawClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=_FakeRawCompletions(error))


class _RecorderSink:
    """记录型审计 sink：把事件收集进列表（绕过 DB/JSONL，便于断言）。"""

    def __init__(self, events: list) -> None:
        self.events = events

    def write(self, event) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _clean_observability_state():
    """每个测试前清理全局状态（审计器 + 残留 trace ContextVar）。"""
    TraceManager.enabled = True
    TraceManager.finish()
    audit.set_audit_logger(None)
    yield
    TraceManager.finish()
    audit.set_audit_logger(None)


def _logger_with_recorder(events: list, sample_rate: float = 1.0) -> AuditLogger:
    logger = AuditLogger(sample_rate=sample_rate)
    logger._sinks = [_RecorderSink(events)]  # noqa: SLF001 - 测试注入
    return logger


def _begin_trace() -> None:
    TraceManager.begin("sess-1", "user-1")


# ---------------------------------------------------------------------------
# Trace/Span 核心
# ---------------------------------------------------------------------------


def test_span_tree_in_memory() -> None:
    _begin_trace()
    parent = TraceManager.start_span(SpanKind.FSM, "EXECUTE", execution="react")
    child = TraceManager.start_span(SpanKind.TOOL, "query_order", tool="query_order")
    time.sleep(0.001)
    TraceManager.end_span(child, status="ok")
    TraceManager.end_span(parent, status="ok")

    payload = TraceManager.finish()
    assert payload is not None
    spans = payload["spans"]
    assert len(spans) == 2
    by_name = {s["name"]: s for s in spans}
    tool_span = by_name["query_order"]
    fsm_span = by_name["EXECUTE"]
    assert tool_span["parent_span_id"] == fsm_span["span_id"]
    assert fsm_span["parent_span_id"] is None
    assert tool_span["status"] == "ok"
    assert tool_span["cost_ms"] > 0
    assert tool_span["kind"] == "tool"
    assert fsm_span["kind"] == "fsm"


def test_trace_disabled_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(TraceManager, "enabled", False)
    assert TraceManager.begin("s", "u") is None
    assert TraceManager.start_span(SpanKind.LLM, "main") is None
    assert TraceManager.finish() is None


def test_span_error_status_on_exception() -> None:
    _begin_trace()
    with pytest.raises(ValueError):
        with TraceManager.span(SpanKind.LLM, "main"):
            raise ValueError("boom")
    payload = TraceManager.finish()
    assert payload["spans"][0]["status"] == "error"
    assert "boom" in payload["spans"][0]["error"]


# ---------------------------------------------------------------------------
# LlmClient：场景 token 分开统计 + OpenAI 兼容 + 审计
# ---------------------------------------------------------------------------


def test_llm_client_scene_token_separate_and_audit() -> None:
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events))
    _begin_trace()

    client = LlmClient(raw_client=_FakeRawClient(), model="test-model")
    r1 = client.generate(messages=[{"role": "user", "content": "x"}], scene="rewrite")
    r2 = client.generate(messages=[{"role": "user", "content": "y"}], scene="main")

    assert r1.content == "你好"
    assert r1.prompt_tokens == 100 and r1.completion_tokens == 20
    assert r1.total_tokens == 120
    # 场景分开：两个场景各一条审计事件，摘要带各自 token
    llm_events = [e for e in events if e.kind == "llm"]
    assert {e.name for e in llm_events} == {"rewrite", "main"}
    rewrite_summary = next(e.summary for e in llm_events if e.name == "rewrite")
    assert rewrite_summary["prompt_tokens"] == 100
    # span 树里 llm span 的 name 即场景
    payload = TraceManager.finish()
    llm_spans = [s for s in payload["spans"] if s["kind"] == "llm"]
    assert {s["name"] for s in llm_spans} == {"rewrite", "main"}


def test_llm_client_error_force_audit_and_reraise() -> None:
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events, sample_rate=0.0))
    _begin_trace()

    client = LlmClient(raw_client=_FakeRawClient(error=TimeoutError("upstream slow")))
    with pytest.raises(TimeoutError):
        client.generate(messages=[], scene="main")

    llm_events = [e for e in events if e.kind == "llm"]
    assert len(llm_events) == 1
    assert llm_events[0].status == "error"
    assert "TimeoutError" in llm_events[0].error
    # 异常即使抽样率为 0 也 100% 写入（force=True）
    assert llm_events[0].status == "error"
    TraceManager.finish()


def test_llm_client_openai_compat_interface() -> None:
    """LlmClient 的 create/acreate 接口兼容 OpenAI client，可无感替换。"""
    _begin_trace()
    client = LlmClient(raw_client=_FakeRawClient(), model="m", default_scene="react")
    result = client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}],
    )
    assert isinstance(result, LLMCallResult)
    assert result.choices[0].message.content == "你好"
    assert result.usage.total_tokens == 120
    TraceManager.finish()


# ---------------------------------------------------------------------------
# McpToolClient：脱敏 + 埋点 + 错误 re-raise
# ---------------------------------------------------------------------------


def test_mask_sensitive() -> None:
    masked = mask_sensitive({
        "order_id": "ORD-1",
        "phone": "13800138000",
        "user_name": "张三",
        "note": "联系 13800138000",
        "nested": {"address": "北京市朝阳区"},
    })
    assert masked["order_id"] == "ORD-1"
    assert masked["phone"].startswith("138")
    assert masked["phone"].endswith("00")  # 保留前 3 后 2
    assert "138****" in masked["note"]  # 文本内嵌号码打码
    assert "朝阳" not in masked["nested"]["address"]  # 敏感 key 打码


def test_mcp_client_masks_arguments_in_audit() -> None:
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events))
    _begin_trace()

    def fake_execute(name: str, args: dict):
        return '{"success": true, "rows": 1}'

    client = McpToolClient(execute=fake_execute)
    result = client.call_tool("query_order", {"order_id": "O1", "phone": "13800138000"})

    assert result == '{"success": true, "rows": 1}'
    tool_events = [e for e in events if e.kind == "tool"]
    assert len(tool_events) == 1
    assert tool_events[0].name == "query_order"
    raw_args = tool_events[0].raw["arguments"]
    assert raw_args["order_id"] == "O1"
    assert "138" in raw_args["phone"] and "13800" not in raw_args["phone"]


def test_mcp_client_error_recorded_and_reraised() -> None:
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events, sample_rate=0.0))
    _begin_trace()

    def boom(name: str, args: dict):
        raise RuntimeError("工具崩了")

    client = McpToolClient(execute=boom)
    with pytest.raises(RuntimeError):
        client.call_tool("apply_refund", {"order_id": "O1"})

    tool_events = [e for e in events if e.kind == "tool"]
    assert len(tool_events) == 1
    assert tool_events[0].status == "error"  # 异常 100% 落审计（force）
    TraceManager.finish()


def test_mcp_client_transient_retry_counts_attempts() -> None:
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events))
    _begin_trace()
    calls = {"n": 0}

    def flaky(name: str, args: dict):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("上游超时")
        return '{"success": true}'

    client = McpToolClient(execute=flaky, retries=2)
    result = client.call_tool("query_logistics", {"order_id": "O1"})
    assert result == '{"success": true}'
    assert calls["n"] == 3
    tool_event = [e for e in events if e.kind == "tool"][0]
    assert tool_event.summary["attempts"] == 3
    TraceManager.finish()


# ---------------------------------------------------------------------------
# FSM 观测
# ---------------------------------------------------------------------------


def test_fsm_decorator_emits_transition() -> None:
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events))
    _begin_trace()

    @fsm_state_observation("TOOL_CALL", timeout_s=60)
    def handle_step(step: int) -> tuple[int, str]:
        time.sleep(0.001)
        return step + 1, "RESPONSE"

    result = handle_step(1, to_state="RESPONSE")
    assert result == 2

    fsm_events = [e for e in events if e.kind == "fsm"]
    assert len(fsm_events) == 1
    assert fsm_events[0].summary["from_state"] == "TOOL_CALL"
    assert fsm_events[0].summary["to_state"] == "RESPONSE"
    assert fsm_events[0].summary["result"] == "ok"
    payload = TraceManager.finish()
    assert any(s["kind"] == "fsm" and s["name"] == "TOOL_CALL" for s in payload["spans"])


def test_fsm_decorator_marks_error_and_reraises() -> None:
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events))
    _begin_trace()

    @fsm_state_observation("PLAN")
    def broken() -> int:
        raise ValueError("规划失败")

    with pytest.raises(ValueError):
        broken()

    fsm_events = [e for e in events if e.kind == "fsm"]
    assert fsm_events[0].summary["result"] == "error"
    assert "ValueError" in fsm_events[0].error
    TraceManager.finish()


def test_base_state_machine_auto_observation() -> None:
    """基类内部统一埋点：子类只写 step，不写任何埋点代码。"""
    events: list = []
    audit.set_audit_logger(_logger_with_recorder(events))
    _begin_trace()

    class CountMachine(BaseStateMachine):
        def step(self, state: str):
            if state == BaseStateMachine._INITIAL_STATE:
                return None, "COUNT"
            if state == "COUNT":
                return None, BaseStateMachine.FINISHED
            return None, BaseStateMachine.STOP

    machine = CountMachine(name="count")
    machine.run()

    assert len(machine.transitions) == 2  # INIT->COUNT, COUNT->FINISHED
    fsm_events = [e for e in events if e.kind == "fsm"]
    assert len(fsm_events) >= 2
    assert any(e.summary["from_state"] == "INIT" for e in fsm_events)
    TraceManager.finish()


def test_state_observer_stage_view() -> None:
    """执行链阶段视图：INTENT→REWRITE→PLAN→EXECUTE→RESPONSE 状态事件。"""
    _begin_trace()
    observer = StateObserver()
    observer.enter("INTENT")
    observer.leave("INTENT", to_state="REWRITE", intent="order_inquiry")
    observer.enter("EXECUTE")
    observer.close(error="unhandled_exception")  # 异常路径：残留状态标记 error

    payload = TraceManager.finish()
    fsm_spans = [s for s in payload["spans"] if s["kind"] == "fsm"]
    assert {s["name"] for s in fsm_spans} == {"INTENT", "EXECUTE"}
    execute_span = next(s for s in fsm_spans if s["name"] == "EXECUTE")
    assert execute_span["status"] == "error"
    assert "unhandled_exception" in execute_span["error"]


# ---------------------------------------------------------------------------
# 审计抽样
# ---------------------------------------------------------------------------


def test_audit_sampling_error_always_force_written() -> None:
    """异常 100% 写入；正常事件按抽样率（sample_rate=0 → 不写 DB）。"""
    from app.observability.audit import AuditEvent

    db_events: list = []
    logger = AuditLogger(sample_rate=0.0)
    logger._sinks = [_FakePgSink(db_events)]  # noqa: SLF001

    logger.log(AuditEvent(kind="llm", name="main", status="ok"), force=False)
    assert len(db_events) == 0  # 正常流量抽样率 0 → 不写

    logger.log(AuditEvent(kind="llm", name="main", status="error", error="boom"), force=True)
    assert len(db_events) == 1  # 异常 100% 写

    logger.log(AuditEvent(kind="tool", name="query_order", status="ok"), force=True)
    assert len(db_events) == 2


class _FakePgSink(PostgresAuditSink):
    """不连数据库的 PostgresAuditSink（跳过父类初始化，仅验证抽样判定）。"""

    def __init__(self, events: list) -> None:
        self.events = events

    def write(self, event) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# RT 聚合
# ---------------------------------------------------------------------------


def test_rt_stage_aggregation() -> None:
    payload = {
        "duration_ms": 1200.0,
        "spans": [
            {"kind": "stage", "name": "rewrite", "cost_ms": 150.0},
            {"kind": "llm", "name": "rewrite", "cost_ms": 100.0},
            {"kind": "llm", "name": "main", "cost_ms": 300.0},
            {"kind": "tool", "name": "query_order", "cost_ms": 80.0},
            {"kind": "tool", "name": "search_knowledge", "cost_ms": 120.0},
        ],
    }
    stages = aggregate_stages(payload)
    assert stages["rewrite"] == pytest.approx(250.0)   # stage + llm(rewrite)
    assert stages["llm_generate"] == pytest.approx(300.0)  # 主生成类 LLM
    assert stages["rag"] == pytest.approx(120.0)       # search_knowledge
    assert stages["tool"] == pytest.approx(80.0)       # 其余工具（排除 rag）
