"""LLM 统一客户端（第12期）：所有 LLM 调用都走 ``LlmClient``，埋点全在内部。

需求：系统多次调用 LLM（查询改写子模型、主回答模型……），必须**按场景分开
统计**（token 消耗、QPS、成本预估），定位哪个环节 token 暴涨。

设计（与 :mod:`app.observability.mcp_client` 范式完全对齐）：

- :class:`LlmClient` 是统一客户端：业务 / 执行器一律调用 ``generate()``
  （或 OpenAI 兼容的 ``chat.completions.create`` / ``acreate``），
  **不直接调底层 SDK**。内部记录 trace_id、输入/输出 token、耗时、
  异常类型、模型名称、场景标签。
- :func:`llm_observation` 装饰器只负责 **Prometheus 指标发射**（counter /
  token / histogram / error），**日志落库逻辑放在客户端内部**（审计 +
  span）。
- 输出三份：指标给监控；异常 100% 写 ``agent_audit_log``、正常流量抽样，
  完整 prompt/response 报文落 JSONL（Loki/ELK 等价物）。

兼容性：:class:`LlmClient` 实现了 OpenAI client 的 ``chat.completions.create``
接口（同步）与 ``acreate``（异步），返回值 :class:`LLMCallResult` 兼容
``choices[0].message.content`` / ``usage`` / ``model`` 访问，因此
``AgentEngine`` / ``ReactExecutor`` / 查询改写器可无感替换为 LlmClient，
无需改业务代码。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from functools import wraps
from typing import Any, Callable, Optional

from app.observability.audit import audit_event, mask_sensitive
from app.observability.metrics import emit_llm
from app.observability.trace import SpanKind, TraceManager

logger = logging.getLogger(__name__)

#: LLM 场景标签：分开统计的维度
SCENE_MAIN = "main"                # 主回答生成
SCENE_REACT = "react"              # ReAct 循环的动态规划



# ---------------------------------------------------------------------------
# 兼容 OpenAI response 的返回对象
# ---------------------------------------------------------------------------


class _FakeMessage:
    __slots__ = ("content", "tool_calls")

    def __init__(self, content: str):
        self.content = content
        self.tool_calls = []


class _FakeChoice:
    __slots__ = ("message",)

    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeUsage:
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")

    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class LLMCallResult:
    """LlmClient 的返回值；兼容 OpenAI response 的常用访问路径。"""

    __slots__ = (
        "content", "model", "prompt_tokens", "completion_tokens",
        "total_tokens", "latency_ms", "scene", "status", "error", "_choices",
    )

    def __init__(
        self,
        content: str,
        *,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        scene: str = SCENE_MAIN,
        status: str = "ok",
        error: str | None = None,
        choices: list | None = None,
    ):
        self.content = content
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        self.latency_ms = latency_ms
        self.scene = scene
        self.status = status
        self.error = error
        self._choices = choices

    @property
    def choices(self) -> list[_FakeChoice]:
        return self._choices if self._choices is not None else [_FakeChoice(self.content)]

    @property
    def usage(self) -> _FakeUsage:
        return _FakeUsage(self.prompt_tokens, self.completion_tokens)


# ---------------------------------------------------------------------------
# 指标装饰器（只做 metrics 发射；日志落库在客户端内部）
# ---------------------------------------------------------------------------


def llm_observation(scene: str | None = None) -> Callable:
    """LLM 观测装饰器：发射 Prometheus 指标（调用次数 / token / 耗时 / 异常）。

    只做指标统计；审计与 span 由封装客户端负责。支持同步与异步函数；
    ``scene`` 优先取装饰器参数，其次取函数 kwargs 的 ``scene``，
    最后回退函数名。token 从返回值提取（要求返回值有 ``usage`` 属性，
    如 OpenAI response 或 :class:`LLMCallResult`）。
    """

    def decorator(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                resolved = scene or kwargs.get("scene") or fn.__name__
                model = kwargs.get("model", "")
                start = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                    _emit_from_result(resolved, model, result, "ok", start, None)
                    return result
                except Exception as exc:  # noqa: BLE001
                    _emit_from_result(
                        resolved, model, None, "error", start, f"{type(exc).__name__}: {exc}",
                    )
                    raise

            return async_wrapper

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved = scene or kwargs.get("scene") or fn.__name__
            model = kwargs.get("model", "")
            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                _emit_from_result(resolved, model, result, "ok", start, None)
                return result
            except Exception as exc:  # noqa: BLE001
                _emit_from_result(
                    resolved, model, None, "error", start, f"{type(exc).__name__}: {exc}",
                )
                raise

        return wrapper

    return decorator


def _emit_from_result(
    scene: str, model: str, result: Any, status: str, start: float, error: str | None,
) -> None:
    cost_ms = (time.perf_counter() - start) * 1000.0
    prompt_tokens = completion_tokens = 0
    if result is not None:
        usage = getattr(result, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        model = model or getattr(result, "model", "")
    emit_llm(
        scene=scene, model=model, status=status,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        cost_ms=cost_ms,
        error_type=error.split(":")[0] if error else None,
    )


# ---------------------------------------------------------------------------
# 统一客户端
# ---------------------------------------------------------------------------


class _CompletionsProxy:
    """暴露 ``create``（同步）与 ``acreate``（异步）两个 OpenAI 兼容入口。"""

    def __init__(self, client: "LlmClient"):
        self._client = client

    @property
    def completions(self) -> "_CompletionsProxy":
        return self

    def create(self, **kwargs: Any) -> LLMCallResult:
        return self._client._sync_call(kwargs)

    async def acreate(self, **kwargs: Any) -> LLMCallResult:
        return await self._client._async_call(kwargs)


class LlmClient:
    """统一 LLM 客户端。

    :param raw_client: OpenAI 兼容客户端；None 时懒加载共享客户端
        （``app.runtime.model_client.get_openai_client``），避免模块加载期拉入重依赖。
    :param model: 默认模型名。
    :param default_scene: 未显式指定 scene 时的场景标签。
    :param audit_errors_only: 仅审计异常（正常流量抽样在 AuditLogger 内做）。
    """

    def __init__(
        self,
        raw_client: Any = None,
        model: str = "",
        default_scene: str = SCENE_MAIN,
        audit_errors_only: bool = False,
    ):
        self._raw_client = raw_client
        self._model = model
        self._default_scene = default_scene
        self._audit_errors_only = audit_errors_only
        self.chat = _CompletionsProxy(self)

    def bind_scene(self, scene: str) -> "LlmClient":
        """返回绑定指定场景的客户端视图（共享底层 client 与模型配置）。

        事项归属、事项执行、回复和记忆使用不同标签，分别统计 token。
        未显式调用时沿用构造时的 ``default_scene``。
        """
        view = LlmClient(
            raw_client=self._raw_client,
            model=self._model,
            default_scene=scene,
            audit_errors_only=self._audit_errors_only,
        )
        return view

    # -- 对外入口 ----------------------------------------------------------

    def generate(
        self,
        *,
        messages: list[dict],
        scene: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> LLMCallResult:
        """同步生成：内部完成 span + 指标 + 审计。"""
        return self._sync_call({
            "messages": messages,
            "scene": scene or self._default_scene,
            "model": model or self._model,
            "temperature": temperature,
            **kwargs,
        })

    async def generate_async(
        self,
        *,
        messages: list[dict],
        scene: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> LLMCallResult:
        """异步生成（不阻塞事件循环）。"""
        return await self._async_call({
            "messages": messages,
            "scene": scene or self._default_scene,
            "model": model or self._model,
            "temperature": temperature,
            **kwargs,
        })

    # -- 内部实现（被 OpenAI 兼容代理调用） --------------------------------

    def _sync_call(self, params: dict) -> LLMCallResult:
        params = dict(params)
        scene = params.pop("scene", self._default_scene)
        model = params.pop("model", self._model) or ""
        span_id = TraceManager.start_span(SpanKind.LLM, scene, model=model)
        start = time.perf_counter()
        budget_info: dict = {}
        try:
            params = self._prepare_request(dict(params, model=model), scene, model, budget_info)
            response = self._raw_create(**params)
            result = self._build_result(response, scene, model, start, "ok", None)
            self._observe(scene, model, result, "ok", None, params, budget_info)
            TraceManager.end_span(span_id, status="ok", model=model)
            return result
        except Exception as exc:  # noqa: BLE001 —— 埋点后原样抛出
            error = f"{type(exc).__name__}: {exc}"
            result = LLMCallResult("", model=model, scene=scene, status="error", error=error)
            self._observe(scene, model, result, "error", error, params, budget_info)
            TraceManager.end_span(
                span_id, status="error", error=error, model=model,
            )
            raise

    async def _async_call(self, params: dict) -> LLMCallResult:
        params = dict(params)
        scene = params.pop("scene", self._default_scene)
        model = params.pop("model", self._model) or ""
        span_id = TraceManager.start_span(SpanKind.LLM, scene, model=model)
        start = time.perf_counter()
        budget_info: dict = {}
        try:
            params = self._prepare_request(dict(params, model=model), scene, model, budget_info)
            response = await self._raw_acreate(**params)
            result = self._build_result(response, scene, model, start, "ok", None)
            self._observe(scene, model, result, "ok", None, params, budget_info)
            TraceManager.end_span(span_id, status="ok", model=model)
            return result
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            result = LLMCallResult("", model=model, scene=scene, status="error", error=error)
            self._observe(scene, model, result, "error", error, params, budget_info)
            TraceManager.end_span(span_id, status="error", error=error, model=model)
            raise

    @staticmethod
    def _prepare_request(params: dict, scene: str, model: str, report: dict) -> dict:
        from app.config.settings import settings
        from app.session.context_budget import estimate_request_tokens
        from app.session.request_budget import apply_request_budget, request_input_limit

        # Resolve all provider options before estimating the actual wire request.
        for key, value in settings.chat_completion_options.items():
            params.setdefault(key, value)
        if not params.get("max_completion_tokens") and not params.get("max_tokens"):
            params["max_tokens"] = settings.model_max_output_tokens
        before = estimate_request_tokens(params)
        report.update(context_budget_enabled=settings.context_request_budget_enabled,
                      context_input_estimated_before=before,
                      context_input_limit=request_input_limit(params))
        bounded = apply_request_budget(params, scene=scene, model=model)
        # Tool deduplication must reflect optional memory actually sent to the
        # response model, including removal by the total request budget.
        from app.tools.memory_tool import sync_memory_prompt_visibility
        sync_memory_prompt_visibility(bounded.get("messages", []))
        after = estimate_request_tokens(bounded)
        removed = len(params.get("messages", [])) - len(bounded.get("messages", []))
        report.update(context_input_estimated_after=after, context_messages_removed=removed)
        logger.info("LLM context budget scene=%s model=%s before=%s after=%s limit=%s removed=%s",
                    scene, model, before, after, report["context_input_limit"], removed)
        return bounded

    # -- 底层调用（绕开 patch_client，避免双重采集） -----------------------

    def _raw_create(self, **kwargs: Any) -> Any:
        from app.support.runtime import check_bot
        check_bot()
        from app.tasks.runtime import current_task
        if current_task() is not None:
            current_task().model_request(kwargs)
        from app.observability.tracer import suspend_active_tracer
        from app.config.settings import settings

        raw = self._raw_client
        if raw is None:
            from app.runtime.model_client import get_openai_client

            raw = get_openai_client()
        with suspend_active_tracer():
            options = settings.chat_completion_options
            for key, value in options.items():
                kwargs.setdefault(key, value)
            from app.runtime.budgets import model_budget
            if not kwargs.get('max_completion_tokens') and not kwargs.get('max_tokens'):
                kwargs['max_tokens'] = settings.model_max_output_tokens
            with model_budget(str(kwargs.get('model', 'default')), kwargs):
                check_bot()
                return raw.chat.completions.create(**kwargs)

    async def _raw_acreate(self, **kwargs: Any) -> Any:
        from app.support.runtime import check_bot
        await asyncio.to_thread(check_bot)
        from app.tasks.runtime import current_task
        if current_task() is not None:
            await asyncio.to_thread(current_task().model_request, kwargs)
        from app.observability.tracer import suspend_active_tracer
        from app.config.settings import settings

        raw = self._raw_client
        if raw is None:
            from app.runtime.model_client import get_openai_client

            raw = get_openai_client()
        with suspend_active_tracer():
            options = settings.chat_completion_options
            for key, value in options.items():
                kwargs.setdefault(key, value)
            from app.runtime.budgets import model_budget
            import inspect
            if not inspect.iscoroutinefunction(raw.chat.completions.create):
                # The lease stays inside the thread until the sync network call
                # actually returns, even if its async caller is cancelled.
                return await asyncio.to_thread(self._raw_create, **kwargs)
            if not kwargs.get('max_completion_tokens') and not kwargs.get('max_tokens'):
                kwargs['max_tokens'] = settings.model_max_output_tokens
            budget = model_budget(str(kwargs.get('model', 'default')), kwargs)
            from app.runtime.async_utils import settle
            admission = asyncio.create_task(asyncio.to_thread(budget.__enter__))
            try:
                await asyncio.shield(admission)
            except asyncio.CancelledError:
                # Acquisition may already be running in a thread. Settle it
                # before propagating cancellation so no slot is leaked.
                try:
                    await settle(admission)
                except Exception:
                    pass
                else:
                    await settle(asyncio.create_task(asyncio.to_thread(budget.__exit__, None, None, None)))
                raise
            try:
                await asyncio.to_thread(check_bot)
                if inspect.iscoroutinefunction(raw.chat.completions.create):
                    return await raw.chat.completions.create(**kwargs)
                return await asyncio.to_thread(raw.chat.completions.create, **kwargs)
            finally:
                await settle(asyncio.create_task(asyncio.to_thread(budget.__exit__, None, None, None)))

    # -- 观测 --------------------------------------------------------------

    def _observe(
        self,
        scene: str,
        model: str,
        result: LLMCallResult,
        status: str,
        error: str | None,
        params: dict,
        budget_info: dict | None = None,
    ) -> None:
        """指标 + 审计（审计由 AuditLogger 做抽样；异常 force 100% 写入）。"""
        emit_llm(
            scene=scene, model=model, status=status,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_ms=result.latency_ms,
            error_type=error.split(":")[0] if error else None,
        )
        ctx = TraceManager.active()
        audit_event(
            kind="llm",
            name=scene,
            trace_id=ctx.trace_id if ctx else "",
            session_id=ctx.session_id if ctx else "",
            user_id=ctx.user_id if ctx else "",
            status=status,
            cost_ms=result.latency_ms,
            error=error,
            summary={
                "model": model,
                "scene": scene,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
                **(budget_info or {}),
            },
            # 完整报文也必须先脱敏；审计 sink 不应成为用户地址、手机号的泄漏出口。
            raw={"messages": mask_sensitive(params.get("messages", []))},
            force=status != "ok",  # 异常 100% 写库，正常由抽样决定
        )

    @staticmethod
    def _build_result(
        response: Any,
        scene: str,
        model: str,
        start: float,
        status: str,
        error: str | None,
    ) -> LLMCallResult:
        latency_ms = (time.perf_counter() - start) * 1000.0
        content = ""
        choices = None
        prompt_tokens = completion_tokens = 0
        try:
            content = response.choices[0].message.content or ""
            if getattr(response.choices[0].message, 'tool_calls', None):
                choices = response.choices
            usage = getattr(response, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            model = getattr(response, "model", "") or model
        except (AttributeError, IndexError):
            pass
        return LLMCallResult(
            content, model=model, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, latency_ms=latency_ms,
            scene=scene, status=status, error=error,
            choices=choices,
        )


__all__ = [
    "LLMCallResult",
    "LlmClient",
    "SCENE_MAIN",
    "SCENE_REACT",
    "llm_observation",
]
