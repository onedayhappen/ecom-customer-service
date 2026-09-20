"""Prometheus 指标定义（第12期）：所有埋点的指标出口。

四组指标，与三类 span + RT 一一对应：

- **LLM**（``agent_llm_*``）：调用次数、token 消耗（按 prompt/completion）、
  耗时直方图、异常计数——均带 ``scene`` 标签（rewrite=查询改写 /
  main=主回答 / workflow_reply=Workflow 表述 / react=ReAct 规划），
  场景分开统计，定位「哪个环节 token 暴涨」。
- **工具**（``agent_tool_*``）：调用次数（按状态 success/error/timeout/
  blocked）、耗时直方图、重试次数。
- **FSM**（``agent_fsm_*``）：状态流转计数器（from_state/to_state/result）、
  每个状态执行耗时直方图。
- **RT**（``agent_rt_*``）：整条 query 总 RT 与各阶段 RT（rewrite / llm_generate /
  tool / rag）直方图，供监控大盘聚合 P50/P95/P99。

fail-safe 设计：``prometheus_client`` 未安装（如离线测试环境）时，指标对象
自动降级为 no-op 桩（计数丢弃、耗时丢弃），**任何环境都能 import 本模块**，
不会因为缺少监控依赖而让 API 起不来。安装依赖后无需改代码即自动启用。
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - 环境差异分支
    from prometheus_client import Counter, Histogram, REGISTRY

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PROMETHEUS_AVAILABLE = False

    class Counter:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def labels(self, *args: Any, **kwargs: Any):
            return _NoopMetric()

        def inc(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Histogram:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def labels(self, *args: Any, **kwargs: Any):
            return _NoopMetric()

        def observe(self, *args: Any, **kwargs: Any) -> None:
            pass

    REGISTRY = None  # type: ignore[assignment]

    class _NoopMetric:
        def inc(self, *args: Any, **kwargs: Any) -> None:
            pass

        def observe(self, *args: Any, **kwargs: Any) -> None:
            pass


# 耗时直方图桶：50ms 起步到 30s，覆盖 LLM/工具/状态处理的典型区间。
_DURATION_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0,
)

# ---------------------------------------------------------------------------
# LLM 指标（scene: rewrite / main / workflow_reply / react）
# ---------------------------------------------------------------------------

LLM_CALLS = Counter(
    "agent_llm_calls_total", "LLM 调用次数", ["scene", "model", "status"],
)
LLM_TOKENS = Counter(
    "agent_llm_tokens_total", "LLM token 消耗", ["scene", "kind"],
)  # kind: prompt / completion
LLM_DURATION = Histogram(
    "agent_llm_duration_seconds", "LLM 调用耗时", ["scene"],
    buckets=_DURATION_BUCKETS,
)
LLM_ERRORS = Counter(
    "agent_llm_errors_total", "LLM 异常次数", ["scene", "error_type"],
)

# ---------------------------------------------------------------------------
# 工具指标
# ---------------------------------------------------------------------------

TOOL_CALLS = Counter(
    "agent_tool_calls_total", "工具调用次数", ["tool", "status"],
)  # status: success / error / timeout / blocked
TOOL_DURATION = Histogram(
    "agent_tool_duration_seconds", "工具调用耗时", ["tool"],
    buckets=_DURATION_BUCKETS,
)
TOOL_RETRIES = Counter(
    "agent_tool_retries_total", "工具重试次数", ["tool"],
)

# ---------------------------------------------------------------------------
# FSM 状态流转指标
# ---------------------------------------------------------------------------

FSM_TRANSITIONS = Counter(
    "agent_fsm_state_transition_total", "状态流转次数",
    ["from_state", "to_state", "result"],  # result: ok / error / timeout
)
FSM_DURATION = Histogram(
    "agent_fsm_state_duration_seconds", "状态执行耗时", ["state"],
    buckets=_DURATION_BUCKETS,
)

# ---------------------------------------------------------------------------
# RT 指标（stage: total / rewrite / llm_generate / tool / rag）
# ---------------------------------------------------------------------------

RT_TOTAL = Histogram(
    "agent_rt_total_seconds", "整条请求 RT", buckets=_DURATION_BUCKETS,
)
RT_STAGE = Histogram(
    "agent_rt_stage_seconds", "阶段 RT", ["stage"],
    buckets=_DURATION_BUCKETS,
)


def metrics_enabled() -> bool:
    """prometheus_client 是否可用（/metrics 端点据此决定返回内容）。"""
    return PROMETHEUS_AVAILABLE


# ---------------------------------------------------------------------------
# 统一发射入口（装饰器 / 客户端 / 观测器共用，保证标签一致）
# ---------------------------------------------------------------------------


def emit_llm(
    *,
    scene: str,
    model: str,
    status: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_ms: float,
    error_type: str | None = None,
) -> None:
    """发射一组 LLM 指标。token/耗时单位统一为 毫秒→秒。"""
    if not PROMETHEUS_AVAILABLE:
        return
    LLM_CALLS.labels(scene=scene, model=model, status=status).inc()
    LLM_TOKENS.labels(scene=scene, kind="prompt").inc(int(prompt_tokens))
    LLM_TOKENS.labels(scene=scene, kind="completion").inc(int(completion_tokens))
    LLM_DURATION.labels(scene=scene).observe(cost_ms / 1000.0)
    if error_type:
        LLM_ERRORS.labels(scene=scene, error_type=error_type).inc()


def emit_tool(
    *,
    tool: str,
    status: str,
    cost_ms: float,
    attempts: int = 1,
) -> None:
    """发射一组工具指标。status: success / error / timeout / blocked。"""
    if not PROMETHEUS_AVAILABLE:
        return
    TOOL_CALLS.labels(tool=tool, status=status).inc()
    TOOL_DURATION.labels(tool=tool).observe(cost_ms / 1000.0)
    if attempts > 1:
        TOOL_RETRIES.labels(tool=tool).inc(attempts - 1)


def emit_fsm_transition(
    *,
    from_state: str,
    to_state: str,
    result: str,
    cost_ms: float,
    state: str | None = None,
) -> None:
    """发射一组状态流转指标。result: ok / error / timeout。"""
    if not PROMETHEUS_AVAILABLE:
        return
    FSM_TRANSITIONS.labels(from_state=from_state, to_state=to_state, result=result).inc()
    FSM_DURATION.labels(state=state or from_state).observe(cost_ms / 1000.0)


def emit_rt(*, total_ms: float, stages: dict[str, float]) -> None:
    """发射 RT 指标：整条请求 + 各阶段拆分（P50/P95/P99 由 histogram 聚合）。"""
    if not PROMETHEUS_AVAILABLE:
        return
    RT_TOTAL.observe(total_ms / 1000.0)
    for stage, cost_ms in stages.items():
        RT_STAGE.labels(stage=stage).observe(cost_ms / 1000.0)
