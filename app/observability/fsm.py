"""FSM 状态流转观测（第12期）：切面（装饰器）+ 状态机内部埋点。

需求：Agent FSM 是进程内状态流转（状态切换、进入、退出、超时、回退），
没有对外网络 IO，因此**没有可封装的网络客户端**，用两套机制结合：

1. :func:`fsm_state_observation` 通用装饰器——加在每个状态处理函数上，
   采集 trace_id、session_id、当前状态、下一个跳转状态、状态执行耗时、
   是否异常、是否超时。
2. :class:`BaseStateMachine` 状态机核心基类内部埋点——状态进入、状态跳转、
   异常回调在基类统一打日志、上报指标；**新增状态不需要每个状态手写埋点**，
   子类只需实现 ``step()`` 返回下一个状态。

指标：

- ``agent_fsm_state_transition_total``：状态流转计数器（from_state/to_state/result）
- ``agent_fsm_state_duration_seconds``：每个状态执行耗时直方图

日志：状态流转事件写入审计日志（记录状态跳转链路），排查 Agent 卡在哪个状态。

本项目执行链是「无状态机架构」（决策规则表 + Workflow/ReAct 三路分发），
因此额外提供 :class:`StateObserver` 观测器：把执行阶段
（INTENT → REWRITE → PLAN → EXECUTE → RESPONSE）**建模为状态流转事件**，
与装饰器产出完全同构（同一套 emit + 审计），但业务层不引入状态机——
「观测层有状态视图，业务层无状态机」，可在不影响定稿架构的前提下
看到「大量请求卡在 TOOL_CALL → 外部 MCP 接口慢」这类问题。
"""

from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any, Callable, Optional

from app.observability.audit import audit_event, mask_sensitive
from app.observability.metrics import emit_fsm_transition
from app.observability.trace import SpanKind, TraceManager

#: 状态执行结果标签（与指标 result 维度一致）
RESULT_OK = "ok"
RESULT_ERROR = "error"
RESULT_TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# 状态流转事件发射（装饰器 / 基类 / 观测器共用）
# ---------------------------------------------------------------------------


def emit_state_transition(
    *,
    from_state: str,
    to_state: str,
    result: str,
    cost_ms: float,
    error: str | None = None,
    tags: dict | None = None,
) -> None:
    """发射一条状态流转事件：指标 + 审计日志（记录跳转链路）。"""
    emit_fsm_transition(
        from_state=from_state, to_state=to_state, result=result, cost_ms=cost_ms,
        state=from_state,
    )
    ctx = TraceManager.active()
    audit_event(
        kind="fsm",
        name=f"{from_state}->{to_state}",
        trace_id=ctx.trace_id if ctx else "",
        session_id=ctx.session_id if ctx else "",
        user_id=ctx.user_id if ctx else "",
        status=result,
        cost_ms=cost_ms,
        error=error,
        summary={"from_state": from_state, "to_state": to_state, "result": result},
        raw={"tags": mask_sensitive(tags or {})},
        force=result != RESULT_OK,  # 异常/超时 100% 写审计
    )


# ---------------------------------------------------------------------------
# 切面：状态处理函数装饰器
# ---------------------------------------------------------------------------


def fsm_state_observation(
    state_name: str | None = None,
    *,
    timeout_s: float | None = None,
) -> Callable:
    """状态机通用装饰器：加在每个状态处理函数上。

    采集：trace_id、session_id、当前状态（``state_name`` 或函数名）、
    下一个跳转状态、状态执行耗时、是否异常、是否超时。

    「下一个跳转状态」的约定（三选一，按序识别）：

    1. 被装饰函数返回值是 ``(result, next_state)`` 元组；
    2. 返回值的 ``next_state`` 属性非空；
    3. 显式传 ``to_state=...`` 关键字参数。

    超时判定：``timeout_s`` 设置后，耗时超过阈值即标记 ``timeout``（不中断
    执行，只改变观测标签——同步函数无法强制中断，真实超时中断留给异步层）。
    """

    def decorator(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                from_state = state_name or kwargs.pop("fsm_state", None) or fn.__name__
                to_state = kwargs.pop("to_state", None)
                span_id = TraceManager.start_span(
                    SpanKind.FSM, from_state, to_state=to_state or "",
                )
                start = time.perf_counter()
                result_value = None
                try:
                    result_value = await fn(*args, **kwargs)
                    result, next_state = _resolve_transition(result_value, to_state)
                    emit_state_transition(
                        from_state=from_state, to_state=next_state or "",
                        result=_result_label(start, timeout_s),
                        cost_ms=(time.perf_counter() - start) * 1000.0,
                    )
                    TraceManager.end_span(
                        span_id, status=_result_label(start, timeout_s),
                        to_state=next_state or "",
                    )
                    return result
                except Exception as exc:  # noqa: BLE001 —— 埋点后原样抛出
                    error_text = f"{type(exc).__name__}: {exc}"
                    emit_state_transition(
                        from_state=from_state, to_state=to_state or "",
                        result=RESULT_ERROR, cost_ms=(time.perf_counter() - start) * 1000.0,
                        error=error_text,
                    )
                    TraceManager.end_span(
                        span_id, status="error", error=error_text, to_state=to_state or "",
                    )
                    raise

            return async_wrapper

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from_state = state_name or kwargs.pop("fsm_state", None) or fn.__name__
            to_state = kwargs.pop("to_state", None)
            span_id = TraceManager.start_span(
                SpanKind.FSM, from_state, to_state=to_state or "",
            )
            start = time.perf_counter()
            try:
                result_value = fn(*args, **kwargs)
                result, next_state = _resolve_transition(result_value, to_state)
                emit_state_transition(
                    from_state=from_state, to_state=next_state or "",
                    result=_result_label(start, timeout_s),
                    cost_ms=(time.perf_counter() - start) * 1000.0,
                )
                TraceManager.end_span(
                    span_id, status=_result_label(start, timeout_s),
                    to_state=next_state or "",
                )
                return result
            except Exception as exc:  # noqa: BLE001 —— 埋点后原样抛出
                error_text = f"{type(exc).__name__}: {exc}"
                emit_state_transition(
                    from_state=from_state, to_state=to_state or "",
                    result=RESULT_ERROR, cost_ms=(time.perf_counter() - start) * 1000.0,
                    error=error_text,
                )
                TraceManager.end_span(
                    span_id, status="error", error=error_text, to_state=to_state or "",
                )
                raise

        return wrapper

    return decorator


def _resolve_transition(result_value: Any, explicit_to: Any) -> tuple[Any, Any]:
    """从返回值 / 显式参数解析 (结果, 下一状态)。

    优先级：返回值是 ``(result, next_state)`` 元组 → 解包（next_state 优先
    取元组内值，其次显式参数）；否则取显式 ``to_state`` / 返回值的
    ``next_state`` 属性；都没有则 (结果, None)。
    """
    if isinstance(result_value, tuple) and len(result_value) == 2:
        inner_result, inner_next = result_value
        return inner_result, explicit_to if inner_next is None else inner_next
    if explicit_to is not None:
        return result_value, explicit_to
    if result_value is not None:
        next_state = getattr(result_value, "next_state", None)
        if next_state:
            return result_value, next_state
    return result_value, None


def _result_label(start: float, timeout_s: float | None) -> str:
    if timeout_s is not None and (time.perf_counter() - start) > timeout_s:
        return RESULT_TIMEOUT
    return RESULT_OK


# ---------------------------------------------------------------------------
# 状态机基类（基类内部统一埋点，新增状态无需手写埋点）
# ---------------------------------------------------------------------------


class BaseStateMachine:
    """状态机核心基类：状态进入 / 跳转 / 异常回调统一埋点。

    子类约定：

    - 实现 :meth:`step` 返回 ``(result, next_state)``；``next_state`` 为
      ``None`` / ``STOP`` / ``FINISHED`` 之一时状态机结束。
    - 状态切换统一走 :meth:`_transition`（基类埋点），子类不应绕过。
    - :meth:`on_error` / :meth:`on_timeout` 可重写做业务兜底，基类会先埋点。

    指标与审计：每次 :meth:`_transition` 发射
    ``agent_fsm_state_transition_total`` / ``agent_fsm_state_duration_seconds``
    并写状态流转审计日志；:meth:`run` 期间任何异常都会被捕获、记录并
    走 :meth:`on_error`（异常不会裸抛给调用方——除非 on_error 重新抛出）。
    """

    STOP = "STOP"
    FINISHED = "FINISHED"
    _INITIAL_STATE = "INIT"

    def __init__(self, name: str = "", timeout_s: float | None = None, convergence_detector=None):
        self.fsm_name = name or self.__class__.__name__
        self.timeout_s = timeout_s
        # 第13期：收敛检测（状态 A→B→A 震荡防护）。默认 None 不启用；
        # 注入 ConvergenceDetector 后，基类在每次 _transition 统一喂状态序列，
        # 检测到震荡即终止 run（子类零感知）。
        self.convergence_detector = convergence_detector
        self._converged = False
        self._convergence_reason: str | None = None
        self._state: str = self._INITIAL_STATE
        self._ended = False
        self.transitions: list[dict[str, Any]] = []  # 内存中的跳转链路（可观测）

    @property
    def state(self) -> str:
        return self._state

    @property
    def converged(self) -> bool:
        """是否因状态震荡被收敛检测终止。"""
        return self._converged

    # -- 模板方法 ----------------------------------------------------------

    def run(self) -> Any:
        """驱动状态机：循环 step → transition，直到结束；异常统一埋点。

        第13期：配置了收敛检测器时，检测到 A→B→A 震荡会优雅终止
        （不抛异常，置 :attr:`converged`，供调用方判断）。
        """
        self._state = self._INITIAL_STATE
        self._ended = False
        self._converged = False
        self._convergence_reason = None
        self.transitions.clear()
        try:
            while not self._ended and not self._converged:
                start = time.perf_counter()
                step_result = self.step(self._state)
                cost_ms = (time.perf_counter() - start) * 1000.0
                if self.timeout_s is not None and cost_ms / 1000.0 > self.timeout_s:
                    self._on_timeout(self._state, cost_ms)
                result, next_state = _resolve_transition(step_result, None)
                if next_state in (None, self.STOP, self.FINISHED):
                    self._transition(
                        self._state, next_state or self.FINISHED,
                        result=RESULT_OK, cost_ms=cost_ms,
                    )
                    self._ended = True
                else:
                    self._transition(
                        self._state, next_state, result=RESULT_OK, cost_ms=cost_ms,
                    )
                    self._state = next_state
            return self._finalize()
        except Exception as exc:  # noqa: BLE001 —— 基类统一埋异常
            self._on_error(self._state, exc)
            return self.on_error(self._state, exc)

    def step(self, state: str) -> Any:
        """子类实现：处理当前状态，返回 (result, next_state)。"""
        raise NotImplementedError

    # -- 可重写钩子 ----------------------------------------------------------

    def on_error(self, state: str, exc: Exception) -> Any:
        """异常兜底：默认吞掉异常返回 None；子类可重写（如转人工/重试）。"""
        return None

    def _finalize(self) -> Any:
        return None

    # -- 基类统一埋点 --------------------------------------------------------

    def _transition(self, from_state: str, to_state: str, *, result: str, cost_ms: float) -> None:
        """状态跳转唯一出口：统一发射指标 + 审计 + span（子类禁止绕过）。

        第13期：配置了收敛检测器时，每次跳转喂入状态序列；检测到
        A→B→A 震荡 → 记录原因并停止状态机（优雅终止，不抛异常）。
        """
        span_id = TraceManager.start_span(
            SpanKind.FSM, from_state, to_state=to_state,
        )
        emit_state_transition(
            from_state=from_state, to_state=to_state, result=result, cost_ms=cost_ms,
            tags={"fsm": self.fsm_name},
        )
        TraceManager.end_span(span_id, status=result, to_state=to_state)
        self.transitions.append({
            "from": from_state, "to": to_state,
            "result": result, "cost_ms": round(cost_ms, 1),
        })
        if self.convergence_detector is not None:
            report = self.convergence_detector.record_state_transition(from_state, to_state)
            if report is not None:
                self._converged = True
                self._convergence_reason = f"{report.reason.value}: {report.detail}"

    def _on_timeout(self, state: str, cost_ms: float) -> None:
        """状态超时埋点（不中断，仅标记）。"""
        emit_state_transition(
            from_state=state, to_state=state, result=RESULT_TIMEOUT, cost_ms=cost_ms,
            error=f"状态执行超时（>{self.timeout_s}s）", tags={"fsm": self.fsm_name},
        )

    def _on_error(self, state: str, exc: Exception) -> None:
        """状态异常埋点（跳转链路上标记 error）。"""
        error_text = f"{type(exc).__name__}: {exc}"
        emit_state_transition(
            from_state=state, to_state="", result=RESULT_ERROR, cost_ms=0.0,
            error=error_text, tags={"fsm": self.fsm_name},
        )


# ---------------------------------------------------------------------------
# 阶段视图观测器（无状态机架构下的观测层状态视图）
# ---------------------------------------------------------------------------


class StateObserver:
    """把执行链建模为状态流转的观测器（API 层用，业务层无状态机）。

    用法::

        observer = StateObserver()
        observer.enter("REWRITE")
        ...
        observer.leave("REWRITE", to_state="PLAN")      # ok
        # 异常时：observer.leave("REWRITE", to_state="PLAN", error=...)  # error

    产出与 :func:`fsm_state_observation` / :class:`BaseStateMachine`
    完全同构（同一 emit / 审计 / span），可观测价值一致：
    看请求卡在哪个状态、各状态停留时长、异常跳转占比。
    """

    def __init__(self) -> None:
        self._stack: list[tuple[str, float]] = []

    def enter(self, state: str, **tags: Any) -> None:
        """进入状态：开启 fsm span 并入栈。"""
        span_id = TraceManager.start_span(SpanKind.FSM, state, **tags)
        self._stack.append((state, time.perf_counter(), span_id))

    def close(self, error: str | None = None) -> None:
        """收尾：把栈上残留（未配对 leave）的状态标记并清空。

        异常路径调用：正常路径所有状态都已 leave，栈空，本方法无副作用。
        """
        while self._stack:
            state, start, span_id = self._stack.pop()
            cost_ms = (time.perf_counter() - start) * 1000.0
            result = RESULT_ERROR if error else RESULT_OK
            emit_state_transition(
                from_state=state, to_state="", result=result,
                cost_ms=cost_ms, error=error,
            )
            TraceManager.end_span(span_id, status=result, error=error)

    def leave(self, state: str, *, to_state: str, error: str | None = None, **tags: Any) -> None:
        """离开状态：发射状态流转事件（异常自动标记 error）。"""
        # 弹出与该状态匹配的栈顶（防御未配对调用）
        matched = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == state:
                matched = self._stack.pop(index)
                break
        if matched is None:
            return
        _, start, span_id = matched
        cost_ms = (time.perf_counter() - start) * 1000.0
        result = RESULT_ERROR if error else RESULT_OK
        emit_state_transition(
            from_state=state, to_state=to_state, result=result,
            cost_ms=cost_ms, error=error, tags=tags,
        )
        TraceManager.end_span(
            span_id, status=result, error=error, to_state=to_state, **tags,
        )


__all__ = [
    "RESULT_ERROR",
    "RESULT_OK",
    "RESULT_TIMEOUT",
    "BaseStateMachine",
    "StateObserver",
    "emit_state_transition",
    "fsm_state_observation",
]
