"""业务链日志监控：线上每条 chat 请求的完整可观测轨迹。

采集模型调用、工具结果和执行阶段，供问题排查和审计。

- **阶段事件直埋**：API 层在各业务阶段显式调用 :meth:`BusinessTracer.record`；
- **LLM 调用插桩**：对共享 OpenAI client 的 ``completions.create`` 打一次
  幂等补丁，wrapper 通过 ContextVar 判断当前线程是否有活跃 tracer——
  无活跃 tracer 时零开销直接放行，线程隔离、不侵入业务代码；
- **工具调用复用**：直接取 ``ExecutionResult.tool_steps``（含 name /
  arguments / result / blocked / replayed），不重复采集。

安全：``confirmation_token`` 只落 SHA-256 摘要，不落完整令牌
（与 服务端操作确认 的服务端存储约定一致）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.config.settings import settings
from app.observability.audit import mask_sensitive

# 当前线程活跃 tracer：patch 的 client 通过它判断是否处于观测中的请求。
_active_tracer: ContextVar["BusinessTracer | None"] = ContextVar(
    "business_tracer", default=None
)

# JSONL 追加写与 client 补丁的进程级锁。
_write_lock = threading.Lock()
_patch_lock = threading.Lock()
_patched_clients: set[int] = set()


@contextmanager
def suspend_active_tracer():
    """临时清空当前线程的活跃 tracer（返回后恢复）。

    LlmClient 内部调用共享 OpenAI client 时用它绕开 patch_client 的插桩，
    避免「LlmClient 自身埋点 + patch 插桩」双重采集；无活跃 tracer 时
    patch wrapper 零开销放行。
    """
    token = _active_tracer.set(None)
    try:
        yield
    finally:
        _active_tracer.reset(token)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class TraceEvent:
    """业务链上的一个阶段事件（相对请求开始的毫秒时间戳）。"""

    phase: str
    ts_ms: float
    latency_ms: float
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "ts_ms": round(self.ts_ms, 1),
            "latency_ms": round(self.latency_ms, 1),
            **self.data,
        }


class BusinessTracer:
    """单次 chat 请求的业务链轨迹采集器。"""

    def __init__(self, session_id: str, user_id: str, message: str):
        self.request_id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.user_id = user_id
        self.message = message
        self._start_abs = time.time()
        self._start_mono = time.perf_counter()
        self._last_ts = 0.0
        self.events: list[TraceEvent] = []
        self.llm_calls: list[dict] = []
        self.tool_steps: list[dict] = []
        self.response: dict | None = None
        self.error: str | None = None

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    @classmethod
    def start(cls, session_id: str, user_id: str, message: str) -> "BusinessTracer":
        """开启一次追踪并注册为当前线程活跃 tracer。

        同时启动第12期 Trace/Span 核心（:class:`TraceManager`），
        客户端（LLM / 工具）埋的 span 会并入本 tracer 的 payload。
        """
        tracer = cls(session_id, user_id, message)
        tracer.record("request", session_id=session_id, user_id=user_id)
        _active_tracer.set(tracer)
        from app.observability.trace import TraceManager

        TraceManager.begin(session_id, user_id)
        return tracer

    @classmethod
    def active(cls) -> "BusinessTracer | None":
        return _active_tracer.get()

    def finish(self) -> dict | None:
        """序列化并追加写入当天 JSONL；返回 payload（观测失败不影响业务）。"""
        _active_tracer.set(None)
        payload = self.to_dict()
        # 收尾 Trace/Span 核心：span 树并入 payload，并发射 RT 指标
        try:
            from app.observability.rt import finalize_trace
            from app.observability.trace import TraceManager

            trace_payload = TraceManager.finish()
            if trace_payload:
                payload["spans"] = trace_payload["spans"]
                finalize_trace(trace_payload)
        except Exception:
            pass  # 观测故障不阻断客服响应
        if not settings.observability_enabled:
            return payload
        try:
            path = self._log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with _write_lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 观测故障不阻断客服响应
        return payload

    def _log_path(self) -> Path:
        day = date.fromtimestamp(self._start_abs).isoformat()
        return Path(settings.observability_log_dir) / f"business-trace-{day}.jsonl"

    # ------------------------------------------------------------
    # 事件记录
    # ------------------------------------------------------------
    def record(self, phase: str, latency_ms: float | None = None, **data: Any) -> None:
        """记录一个阶段事件。latency_ms 缺省时取与上一事件的间隔。"""
        now = (time.perf_counter() - self._start_mono) * 1000
        if latency_ms is None:
            latency_ms = now - self._last_ts
        self._last_ts = now
        self.events.append(
            TraceEvent(phase=phase, ts_ms=now, latency_ms=latency_ms, data=dict(data))
        )

    def _record_llm_call(self, response: Any, latency_ms: float) -> None:
        """由 patch 的 completions.create 回调；字段与 LLMCallRecord 对齐。"""
        usage = getattr(response, "usage", None)
        self.llm_calls.append({
            "model": getattr(response, "model", "") or "",
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            "latency_ms": round(latency_ms, 1),
            "tool_calls": self._extract_tool_calls(response),
        })

    @staticmethod
    def _extract_tool_calls(response: Any) -> list[dict]:
        calls: list[dict] = []
        try:
            message = response.choices[0].message
            for tc in (getattr(message, "tool_calls", None) or []):
                calls.append({"name": tc.function.name, "arguments": tc.function.arguments})
        except (AttributeError, IndexError):
            pass
        return calls

    def set_tool_steps(self, steps) -> None:
        """从 ExecutionResult.tool_steps 复制工具证据（Token 只留摘要）。"""
        for step in steps:
            token = getattr(step, "confirmation_token", None)
            self.tool_steps.append({
                "name": step.name,
                "arguments": step.arguments,
                "result": step.result,
                "blocked": getattr(step, "blocked", False),
                "replayed": getattr(step, "replayed", False),
                "confirmation_token_sha256": _sha256(token) if token else None,
            })

    def set_response(
        self,
        *,
        reply: str,
        requires_human: bool,
        follow_up_question: str | None,
        intent: str,
        execution: str,
        confidence: float,
    ) -> None:
        self.response = {
            "reply": reply,
            "requires_human": requires_human,
            "follow_up_question": follow_up_question,
            "intent": intent,
            "execution": execution,
            "confidence": confidence,
        }

    def set_error(self, error: str) -> None:
        self.error = error
        self.record("error", error=error)

    # ------------------------------------------------------------
    # 序列化与聚合
    # ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "ts": round(self._start_abs, 3),
            "duration_ms": round((time.perf_counter() - self._start_mono) * 1000, 1),
            "message": mask_sensitive(self.message),
            "events": mask_sensitive([e.to_dict() for e in self.events]),
            "llm_calls": mask_sensitive(self.llm_calls),
            "tool_steps": mask_sensitive(self.tool_steps),
            "response": mask_sensitive(self.response),
            "error": mask_sensitive(self.error),
        }

    @property
    def total_tokens(self) -> int:
        return sum(c.get("total_tokens", 0) for c in self.llm_calls)


def patch_client(client: Any) -> None:
    """给共享 OpenAI client 的 completions.create 打一次补丁（幂等、线程安全）。

    wrapper 常驻进程：每次调用先读当前线程活跃 tracer，无则零开销放行。
    """
    completions = client.chat.completions
    cid = id(completions)
    with _patch_lock:
        if cid in _patched_clients:
            return
        original = completions.create

        def wrapper(*args, **kwargs):
            tracer = _active_tracer.get()
            start = time.perf_counter()
            response = original(*args, **kwargs)
            latency_ms = (time.perf_counter() - start) * 1000
            if tracer is not None:
                tracer._record_llm_call(response, latency_ms)
            return response

        completions.create = wrapper
        _patched_clients.add(cid)
