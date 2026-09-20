"""Trace/Span 核心：一条用户请求一条 trace，内部拆多个 span（内存中构建）。

设计动机（第12期埋点体系的基础设施）：

- 每一条用户请求 = 一条 ``trace``，内部拆多个 ``span``：
  状态流转 span（fsm）、LLM 生成 span（llm）、工具调用 span（tool）；
  阶段事件（stage）与 RAG 检索（rag）也以 span 形态参与，供 RT 拆分。
- 埋点字段全部放在**内存**（:class:`TraceContext` + ContextVar），请求结束时
  统一序列化落盘 / 发射指标，避免每个埋点都做一次 IO。
- 线程隔离：ContextVar 存放当前活跃 trace；FastAPI 的 ``asyncio.to_thread``
  会复制调用方 context，因此同步业务线程中同样能拿到活跃 trace（与
  ``tracer.py`` 的 ``_active_tracer`` 同一机制）。

span 树示例::

    trace
    ├── fsm/INTENT
    ├── fsm/REWRITE
    │   └── llm/rewrite_disambiguator
    ├── fsm/PLAN
    ├── fsm/EXECUTE
    │   ├── tool/query_order
    │   ├── llm/react_step
    │   └── tool/search_knowledge   (RAG 检索耗时由该 tool span 承担)
    └── fsm/RESPONSE

:class:`TraceManager` 是本模块对外门面：``begin`` / ``start_span`` /
``end_span`` / ``finish``。客户端（llm_client / mcp_client）与观测器（fsm）
都通过它采集，业务代码无感知。
"""

from __future__ import annotations

import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 内存埋点结构
# ---------------------------------------------------------------------------


class SpanKind(str, Enum):
    """span 类型：状态流转 / LLM 生成 / 工具调用 / 阶段 / RAG 检索。"""

    FSM = "fsm"
    LLM = "llm"
    TOOL = "tool"
    STAGE = "stage"
    RAG = "rag"


@dataclass
class Span:
    """一个 span：一次可观测单元（状态处理、LLM 调用、工具调用……）。"""

    span_id: str
    parent_span_id: Optional[str]
    kind: SpanKind
    name: str
    start_ms: float = 0.0          # 相对 trace 起点的毫秒
    cost_ms: float = 0.0           # 执行耗时（毫秒）
    status: str = "ok"             # ok / error / timeout
    error: Optional[str] = None
    tags: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind.value,
            "name": self.name,
            "start_ms": round(self.start_ms, 1),
            "cost_ms": round(self.cost_ms, 1),
            "status": self.status,
            "error": self.error,
            "tags": self.tags,
        }


@dataclass
class TraceContext:
    """一次请求的 trace 上下文：span 列表 + span 栈（内存埋点字段）。"""

    trace_id: str
    session_id: str
    user_id: str
    start_abs: float = 0.0        # 绝对时间（序列化用）
    start_mono: float = 0.0       # perf_counter 基准（耗时计算用）
    spans: list[Span] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)  # 活跃 span id 栈

    def now_ms(self) -> float:
        """当前相对 trace 起点的毫秒。"""
        return (time.perf_counter() - self.start_mono) * 1000.0

    def new_span_id(self) -> str:
        return uuid.uuid4().hex[:12]

    @property
    def duration_ms(self) -> float:
        return (time.perf_counter() - self.start_mono) * 1000.0

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "ts": round(self.start_abs, 3),
            "duration_ms": round(self.duration_ms, 1),
            "spans": [span.to_dict() for span in self.spans],
        }


# ---------------------------------------------------------------------------
# 活跃 trace 上下文（线程 / 协程隔离）
# ---------------------------------------------------------------------------

_active_trace: ContextVar["TraceContext | None"] = ContextVar(
    "ecom_trace", default=None,
)

# 并发写审计等场景的进程级锁（span 采集本身是单线程/单协程内的，无需锁）
_trace_lock = threading.Lock()


class TraceManager:
    """trace 生命周期门面：begin / start_span / end_span / finish。

    用法（业务入口处）::

        ctx = TraceManager.begin(session_id, user_id)
        try:
            span_id = TraceManager.start_span(SpanKind.LLM, "rewrite", model=...)
            ...
            TraceManager.end_span(span_id, status="ok")
        finally:
            payload = TraceManager.finish()   # 返回序列化 dict（未启用则 None）

    所有方法都是 best-effort：未 begin / 未启用观测时安全返回，不抛异常。
    """

    enabled: bool = True  # 由 settings.observability_enabled 在装配处覆盖

    # -- 生命周期 ----------------------------------------------------------

    @classmethod
    def begin(cls, session_id: str, user_id: str) -> Optional[TraceContext]:
        if not cls.enabled:
            return None
        ctx = TraceContext(
            trace_id=uuid.uuid4().hex[:16],
            session_id=session_id,
            user_id=user_id,
            start_abs=time.time(),
            start_mono=time.perf_counter(),
        )
        _active_trace.set(ctx)
        return ctx

    @classmethod
    def active(cls) -> Optional[TraceContext]:
        return _active_trace.get()

    @classmethod
    def finish(cls) -> Optional[dict]:
        """结束当前 trace：清空 ContextVar，返回序列化 payload。"""
        ctx = _active_trace.get()
        if ctx is None:
            return None
        _active_trace.set(None)
        return ctx.to_dict()

    # -- span 采集 ----------------------------------------------------------

    @classmethod
    def start_span(
        cls,
        kind: SpanKind,
        name: str,
        *,
        parent: Optional[str] = None,
        **tags: Any,
    ) -> Optional[str]:
        """开启一个 span（入栈），返回 span_id；未启用时返回 None。"""
        ctx = _active_trace.get()
        if ctx is None:
            return None
        span_id = ctx.new_span_id()
        parent_id = parent or (ctx.stack[-1] if ctx.stack else None)
        ctx.spans.append(
            Span(
                span_id=span_id,
                parent_span_id=parent_id,
                kind=kind,
                name=name,
                start_ms=ctx.now_ms(),
                tags=tags,
            )
        )
        ctx.stack.append(span_id)
        return span_id

    @classmethod
    def end_span(
        cls,
        span_id: Optional[str],
        *,
        status: str = "ok",
        error: Optional[str] = None,
        **tags: Any,
    ) -> None:
        """结束一个 span（弹栈并回填耗时/状态）。best-effort，异常不影响业务。"""
        ctx = _active_trace.get()
        if ctx is None or span_id is None:
            return
        try:
            # 栈顶匹配则弹栈；否则按 id 查找（防御嵌套异常路径漏配）
            if ctx.stack and ctx.stack[-1] == span_id:
                ctx.stack.pop()
            span = next((s for s in ctx.spans if s.span_id == span_id), None)
            if span is None:
                return
            span.cost_ms = ctx.now_ms() - span.start_ms
            span.status = status
            span.error = error
            span.tags.update(tags)
        except Exception:  # noqa: BLE001 —— 观测失败不污染业务
            pass

    # -- 便捷 --------------------------------------------------------------

    @classmethod
    def span(cls, kind: SpanKind, name: str, **tags: Any):
        """上下文管理器：``with TraceManager.span(kind, name): ...``。

        退出时自动 end_span；异常时标记 status="error" 并 re-raise。
        """
        return _SpanContext(kind, name, tags)


class _SpanContext:
    """``with TraceManager.span(...)`` 的实现。"""

    def __init__(self, kind: SpanKind, name: str, tags: dict):
        self._kind = kind
        self._name = name
        self._tags = tags
        self._span_id: Optional[str] = None

    def __enter__(self):
        self._span_id = TraceManager.start_span(self._kind, self._name, **self._tags)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._span_id is not None:
            if exc is not None:
                TraceManager.end_span(
                    self._span_id, status="error", error=f"{type(exc).__name__}: {exc}",
                )
            else:
                TraceManager.end_span(self._span_id, status="ok")
        return False
