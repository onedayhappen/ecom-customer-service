"""MCP 工具调用统一客户端（第12期）：所有工具调用都走 ``McpToolClient``。

需求：

1. 封装一层工具调用客户端 :class:`McpToolClient`，业务 / 执行器要调用 MCP
   工具时不直接调底层（mcp sdk / 本地 registry），统一走 ``call_tool()``。
   埋点逻辑全部写在客户端内部：调用前记录开始时间，捕获成功 / 超时 /
   各类业务异常，采集 trace_id、tool_name、**入参脱敏**、耗时、重试次数、
   错误信息。
2. 内部搭配 :func:`tool_observation` 装饰器做观测——装饰器**只负责
   Prometheus 指标发射**（counter / histogram）；日志落库（审计 + span）
   放在封装客户端内部。
3. 输出两份数据：指标给监控（调用次数、成功率、P95）；异常 100% 写
   ``agent_audit_log``、正常流量抽样，完整入参出参 JSON 落日志（Loki/ELK）。

兼容性：:class:`McpToolClient` 默认以 ``app.tools.registry.execute_tool``
为底层执行器（当前项目 MCP 未启用、工具走本地注册表分发）；未来接入真实
MCP server 时只需替换底层 ``execute``，埋点逻辑不变。底层第三方 MCP 库
代码完全不动——没有修改、没有运行时替换。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from functools import wraps
from typing import Any, Callable, Optional

from app.observability.audit import audit_event, mask_sensitive
from app.observability.metrics import emit_tool
from app.observability.trace import SpanKind, TraceManager

#: 工具执行函数签名：execute(name, arguments) -> str | dict（JSON 字符串或 dict）
ToolExecutor = Callable[[str, dict[str, Any]], Any]

#: 瞬时失败的错误特征（用于自动重试判定；写操作由调用方控制，这里不区分）
_TRANSIENT_MARKERS = ("超时", "timeout", "网络", "network", "暂不可用", "服务繁忙")


def _is_transient(error: str) -> bool:
    return any(marker in (error or "") for marker in _TRANSIENT_MARKERS)


def _parse_result(raw: Any) -> dict:
    """把工具返回统一为 dict（str JSON 或 dict 透传），解析失败保留原文。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"value": data}
        except json.JSONDecodeError:
            return {"value": raw}
    return {"value": raw}


# ---------------------------------------------------------------------------
# 指标装饰器（只做 metrics 发射；日志落库在客户端内部）
# ---------------------------------------------------------------------------


def tool_observation(tool_name: str | None = None) -> Callable:
    """工具观测装饰器：发射 Prometheus 指标（调用次数 / 耗时 / 重试）。

    只做指标统计；审计与 span 由封装客户端负责。支持同步与异步函数；
    ``tool_name`` 优先取装饰器参数，其次取函数 kwargs 的 ``tool_name``
    或位置参数（约定第 1 个参数为工具名），最后回退函数名。
    """

    def decorator(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                name = tool_name or kwargs.get("tool_name") or (
                    args[0] if args else fn.__name__
                )
                start = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                    emit_tool(
                        tool=str(name), status="success",
                        cost_ms=(time.perf_counter() - start) * 1000.0,
                    )
                    return result
                except Exception as exc:  # noqa: BLE001
                    emit_tool(
                        tool=str(name), status="error",
                        cost_ms=(time.perf_counter() - start) * 1000.0,
                    )
                    raise

            return async_wrapper

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            name = tool_name or kwargs.get("tool_name") or (
                args[0] if args else fn.__name__
            )
            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                emit_tool(
                    tool=str(name), status="success",
                    cost_ms=(time.perf_counter() - start) * 1000.0,
                )
                return result
            except Exception as exc:  # noqa: BLE001
                emit_tool(
                    tool=str(name), status="error",
                    cost_ms=(time.perf_counter() - start) * 1000.0,
                )
                raise

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 统一客户端
# ---------------------------------------------------------------------------


class McpToolClient:
    """统一工具调用客户端：埋点（trace/脱敏/耗时/重试/错误）全在内部。

    :param execute: 底层工具执行函数（默认 ``app.tools.registry.execute_tool``）；
        返回 str（JSON）或 dict，原样透传不转换，保证与现有调用方语义一致。
    :param retries: 瞬时失败自动重试次数（默认 0；ReAct 层的透明重试由
        ReActPolicy 负责，本客户端默认不重复重试，避免双重策略）。
    :param timeout_ms: 预期耗时预算（标注用；同步底层函数无法强制中断，
        接入真实 MCP server 后由异步路径 ``asyncio.wait_for`` 落实）。
    """

    def __init__(
        self,
        execute: ToolExecutor | None = None,
        retries: int = 0,
        timeout_ms: int = 0,
    ):
        if execute is None:
            from app.tools.registry import execute_tool

            execute = execute_tool
        self._execute = execute
        self.retries = max(0, int(retries))
        self.timeout_ms = max(0, int(timeout_ms))

    def set_execute(self, execute: ToolExecutor) -> None:
        """替换底层执行函数（Harness 故障注入 / 测试替换用）。

        原执行函数可从 ``self._execute`` 读取；替换后 ``call_tool`` /
        ``call_tool_async`` 立即走新执行链，埋点逻辑不变。
        """
        self._execute = execute

    # -- 对外入口 ----------------------------------------------------------

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        retries: int | None = None,
    ) -> Any:
        """同步调用工具，返回底层原样结果（str | dict）。

        埋点：trace span（kind=tool）+ 入参脱敏审计 + 指标发射；
        瞬时失败按 ``retries`` 自动重试并记录实际执行次数。
        底层抛出的异常记录后**原样 re-raise**（调用方现有 try/except 语义不变）。
        """
        args = arguments or {}
        retry_budget = self.retries if retries is None else max(0, int(retries))
        span_id = TraceManager.start_span(SpanKind.TOOL, tool_name, tool=tool_name)
        start = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            try:
                result = self._execute(tool_name, args)
                self._observe(
                    tool_name, args, result, "success", None,
                    start, attempts,
                )
                TraceManager.end_span(span_id, status="ok", tool=tool_name)
                return result
            except Exception as exc:  # noqa: BLE001 —— 重试判定 + 记录
                error_text = f"{type(exc).__name__}: {exc}"
                if _is_transient(error_text) and attempts <= retry_budget:
                    continue
                self._observe(
                    tool_name, args, None, "error", error_text,
                    start, attempts,
                )
                TraceManager.end_span(
                    span_id, status="error", error=error_text, tool=tool_name,
                )
                raise

    async def call_tool_async(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        retries: int | None = None,
    ) -> Any:
        """异步调用工具：同步底层函数包进 ``asyncio.to_thread`` 不阻塞事件循环。"""
        args = arguments or {}
        retry_budget = self.retries if retries is None else max(0, int(retries))
        span_id = TraceManager.start_span(SpanKind.TOOL, tool_name, tool=tool_name)
        start = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            try:
                result = await asyncio.to_thread(self._execute, tool_name, args)
                self._observe(
                    tool_name, args, result, "success", None,
                    start, attempts,
                )
                TraceManager.end_span(span_id, status="ok", tool=tool_name)
                return result
            except Exception as exc:  # noqa: BLE001
                error_text = f"{type(exc).__name__}: {exc}"
                if _is_transient(error_text) and attempts <= retry_budget:
                    continue
                self._observe(
                    tool_name, args, None, "error", error_text,
                    start, attempts,
                )
                TraceManager.end_span(
                    span_id, status="error", error=error_text, tool=tool_name,
                )
                raise

    # -- 观测 --------------------------------------------------------------

    def _observe(
        self,
        tool_name: str,
        arguments: dict,
        result: Any,
        status: str,
        error: str | None,
        start: float,
        attempts: int,
    ) -> None:
        """指标 + 审计（入参脱敏；异常 force 100% 写库）。"""
        cost_ms = (time.perf_counter() - start) * 1000.0
        emit_tool(
            tool=tool_name, status=status, cost_ms=cost_ms, attempts=attempts,
        )
        ctx = TraceManager.active()
        masked_args = mask_sensitive(arguments)
        audit_event(
            kind="tool",
            name=tool_name,
            trace_id=ctx.trace_id if ctx else "",
            session_id=ctx.session_id if ctx else "",
            user_id=ctx.user_id if ctx else "",
            status=status,
            cost_ms=cost_ms,
            error=error,
            summary={
                "tool": tool_name,
                "attempts": attempts,
                "success": status == "success",
            },
            raw={
                "arguments": masked_args,  # 完整报文（脱敏后）落 JSONL
                "result": mask_sensitive(_parse_result(result)) if result is not None else None,
            },
            force=status != "success",  # 异常 100% 写库，正常由抽样决定
        )


__all__ = ["McpToolClient", "ToolExecutor", "tool_observation"]
