"""Bounded ReAct loop for one task; business writes use TaskOperations.

Both sync and async callers share the same tool whitelist, evidence checks,
read retry limits and convergence rules. A model cannot confirm a mutation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from app.data_plan import DataPlan
from app.data_plan.capabilities import MUTATING_CAPABILITIES
from app.execution.types import ExecutionResult, ToolStep
from app.harness.convergence import CONVERGENCE_REPLY
from app.reliability.evidence import sanitize_reply, verify_reply
from app.reliability.failure_policy import (
    EVIDENCE_FINALIZE_NOTE,
    Action,
    CLARIFY_REPLY,
    HANDOFF_REPLY,
    RECONFIRM_REPLY,
    ReActPolicy,
)

# 知识库不可用时的固定拒答（避免模型凭常识补写政策）
KNOWLEDGE_UNAVAILABLE_REPLY = (
    "抱歉，当前知识库暂时不可用或没有找到可核实的依据。"
    "为了避免提供不准确的信息，我暂时不能确认这项平台政策；"
    "请稍后重试，或联系人工客服核实。"
)

EVIDENCE_STOP_REPLY = (
    "抱歉，经过多次尝试仍无法完成该操作，为避免误操作已停止执行。"
    "如仍需帮助，请联系人工客服处理。"
)


ToolExecutor = Callable[[str, dict[str, Any]], str]


def business_evidence(steps: list[ToolStep]) -> list[dict]:
    """Conversation drafts may contain user claims, never proof of business facts."""
    return [step.result for step in steps if step.name not in {'read_task_context', 'save_task_progress'}]


def default_system_prompt(plan: DataPlan) -> str:
    from app.tasks.execution_models import TASK_PROMPT
    return TASK_PROMPT


class ReactExecutor:
    """LLM 驱动的 ReAct 循环，工具集由 DataPlan 裁剪。"""

    def __init__(
        self,
        client: Any,
        model: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        execute: ToolExecutor | None = None,
        chat_completion_options: dict | None = None,
        convergence_detector=None,
        checkpoint=None,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.chat_completion_options = chat_completion_options or {}
        # 第13期：收敛检测（ReAct 无限循环防护）。默认 None 不启用，行为不变；
        # Harness 测试 / 生产可注入 ConvergenceDetector，触发后终止循环并返回
        # CONVERGENCE_REPLY（不经过 LLM，保证语义稳定）。
        self.convergence_detector = convergence_detector
        self.checkpoint = checkpoint
        # 第12期：若传入 LlmClient，绑定 react 场景（与查询改写/主生成分开统计 token）
        if hasattr(client, "bind_scene"):
            from app.observability.llm_client import SCENE_REACT

            self.client = client.bind_scene(SCENE_REACT)
        if execute is None:
            from app.tools.registry import execute_tool

            execute = execute_tool
        self._execute = execute
        # 第12期：工具执行统一走 McpToolClient（埋点在客户端内部）。
        # 默认不自动重试（ReActPolicy 负责透明重试），透传结果与异常，
        # 对既有测试（注入 fake execute）行为完全一致。
        from app.observability.mcp_client import McpToolClient

        self._tool_client = McpToolClient(execute=execute, retries=0)
        self._observed_execute = self._tool_client.call_tool

    # ------------------------------------------------------------------
    # 同步入口（事项 Worker）
    # ------------------------------------------------------------------

    def run(
        self,
        messages: list[dict],
        plan: DataPlan,
        max_steps: int = 5,
        tool_definitions: list[dict] | None = None,
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """同步执行 ReAct 循环，供事项 Worker 调用。

        通过 ``asyncio.run`` 调用共享核心 ``_run_impl``，适配器为同步直调：
        LLM 直接 ``client.chat.completions.create()``，工具直接 ``self._execute()``。
        """
        async def call_llm(**kw: Any) -> Any:
            return self.client.chat.completions.create(**kw)

        async def call_sync(func: Callable, *a: Any, **kw: Any) -> Any:
            return func(*a, **kw)

        return asyncio.run(self._run_impl(
            messages, plan, max_steps, tool_definitions, call_llm, call_sync,
            historical_evidence,
        ))

    # ------------------------------------------------------------------
    # 异步调用适配器
    # ------------------------------------------------------------------

    async def run_async(
        self,
        messages: list[dict],
        plan: DataPlan,
        max_steps: int = 5,
        tool_definitions: list[dict] | None = None,
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """异步执行相同的受控 ReAct 循环。

        LLM 调用通过 ``await client.chat.completions.create()`` 不占用线程，
        工具 / DB 调用通过 ``asyncio.to_thread`` 不阻塞事件循环。
        事件循环始终保持响应，``/health`` 等轻请求不会被排队。
        """
        async def call_llm(**kw: Any) -> Any:
            # 兼容 LlmClient（暴露 acreate）与 AsyncOpenAI（无 acreate）
            create = getattr(self.client.chat.completions, "acreate", None)
            if create is not None:
                return await create(**kw)
            return await self.client.chat.completions.create(**kw)

        async def call_sync(func: Callable, *a: Any, **kw: Any) -> Any:
            return await asyncio.to_thread(func, *a, **kw)

        return await self._run_impl(
            messages, plan, max_steps, tool_definitions, call_llm, call_sync,
            historical_evidence,
        )

    # ------------------------------------------------------------------
    # 共享核心逻辑（async）
    # ------------------------------------------------------------------

    async def _run_impl(
        self,
        messages: list[dict],
        plan: DataPlan,
        max_steps: int,
        tool_definitions: list[dict] | None,
        call_llm: Callable[..., Any],
        call_sync: Callable[..., Any],
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """ReAct 循环核心逻辑（sync 和 async 路径共用）。

        ``call_llm(**kw)`` → LLM 调用（sync 直调 / async await）。
        ``call_sync(func, *a, **kw)`` → 同步工具/DB 调用（sync 直调 / async to_thread）。
        """
        system_prompt = self.system_prompt or default_system_prompt(plan)
        tools = tool_definitions if tool_definitions is not None else self._tools_for(plan)
        # 工具 schema 只是给模型的提示，不能当作授权边界：模型可能返回
        # 未在 schema 中声明的函数名。真正的白名单来自事项 Harness 提供的 DataPlan。
        allowed_tool_names = set(plan.prompt_tool_names)
        policy = ReActPolicy(mutating_tools=frozenset(MUTATING_CAPABILITIES))

        working: list[dict] = [{"role": "system", "content": system_prompt}, *messages]
        steps: list[ToolStep] = []
        any_replayed = False

        def make_result(reply: str, **kwargs: Any) -> ExecutionResult:
            """终点出口：Evidence Pack 校验后构造 ExecutionResult。"""
            original_reply = reply
            report = verify_reply(reply, [*(historical_evidence or []), *business_evidence(steps)])
            if report.has_unsupported:
                reply = sanitize_reply(reply, report)
            from app.observability.interactions import record
            record("reply.validation", status="corrected" if report.has_unsupported else "ok",
                   input=original_reply, output=reply, report=report)
            return ExecutionResult(
                intent=plan.intent, execution=plan.execution,
                reply=reply, tool_steps=steps, replayed=any_replayed, **kwargs,
            )

        for _ in range(max_steps):
            if self.checkpoint is not None:
                await call_sync(self.checkpoint)
            from app.support.runtime import check_bot
            await call_sync(check_bot)
            # 第13期：收敛检测（规则1 最大轮次阈值）——每轮「思考+工具」计一步
            if self.convergence_detector is not None:
                report = self.convergence_detector.record_agent_step()
                if report is not None:
                    return make_result(CONVERGENCE_REPLY)
            response = await call_llm(
                model=self.model,
                messages=working,
                temperature=self.temperature,
                **({"tools": tools} if tools else {}),
                **self.chat_completion_options,
            )
            assistant_msg = response.choices[0].message

            # 无工具调用 → 直接回复
            if not assistant_msg.tool_calls:
                return make_result(assistant_msg.content or "暂时未能形成有效答复，请补充您的问题或稍后再试。")

            working.append({
                "role": "assistant",
                "content": assistant_msg.content,
                "tool_calls": [
                    {
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in assistant_msg.tool_calls
                ],
            })

            for tc in assistant_msg.tool_calls:
                if self.checkpoint is not None:
                    await call_sync(self.checkpoint)
                name = tc.function.name
                args = self._safe_arguments(tc.function.arguments)

                if name not in allowed_tool_names:
                    blocked = {
                        "success": False,
                        "blocked": True,
                        "error": f"工具 {name} 不属于当前任务允许的能力范围",
                        "error_code": "validation_error",
                    }
                    steps.append(ToolStep(
                        name=name, arguments=args, result=blocked, blocked=True,
                    ))
                    return make_result(
                        "当前请求包含未开放的操作，已停止执行，请联系人工客服。",
                        needs_human=True,
                        blocked=True,
                    )

                from app.tools.catalog import BY_NAME
                if name in MUTATING_CAPABILITIES and name not in BY_NAME:
                    return make_result('此操作需要可信确认流程，已停止执行。', blocked=True, needs_confirmation=True)

                # 第13期：收敛检测（规则3 工具调用指纹）——相同工具+参数连续重复
                if self.convergence_detector is not None:
                    report = self.convergence_detector.record_tool_call(name, args)
                    if report is not None:
                        return make_result(CONVERGENCE_REPLY)

                # 重复调用拦截：同工具+同参数不再次执行，反馈已有结果
                pre = policy.before_call(name, args)
                if pre.action is Action.RETURN_CACHED:
                    cached = dict(pre.cached_result or {})
                    cached["note"] = "重复调用：已返回上次结果，请基于已有结果继续，勿重复调用"
                    steps.append(ToolStep(name=name, arguments=args, result=cached, replayed=True))
                    working.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(cached, ensure_ascii=False),
                    })
                    continue
                if pre.action is Action.STOP_WITH_EVIDENCE:
                    return await self._finalize_impl(
                        working, plan, steps, any_replayed,
                        call_llm, call_sync, fixed_reply=EVIDENCE_STOP_REPLY,
                        historical_evidence=historical_evidence,
                    )

                result_str = await call_sync(self._observed_execute, name, args)
                result = self._parse_result(result_str)
                replayed = False

                # 失败策略决策
                post = policy.after_call(name, args, result)
                if post.action is Action.RETRY_ONCE:
                    # 读工具瞬时失败：同参数透明重试一次，模型只看到最终结果
                    result_str = await call_sync(self._observed_execute, name, args)
                    result = self._parse_result(result_str)
                    post = policy.after_call(name, args, result)

                if post.action is Action.CHECK_IDEMPOTENCY:
                    return make_result(HANDOFF_REPLY, needs_human=True)

                elif post.action is Action.STOP_CLARIFY:
                    return make_result(CLARIFY_REPLY)
                elif post.action is Action.STOP_HANDOFF:
                    return make_result(HANDOFF_REPLY, needs_human=True)
                elif post.action is Action.STOP_RECONFIRM:
                    return ExecutionResult(
                        intent=plan.intent, execution=plan.execution,
                        reply=RECONFIRM_REPLY, tool_steps=steps,
                        needs_confirmation=True, replayed=any_replayed,
                    )
                elif post.action is Action.STOP_WITH_EVIDENCE:
                    return await self._finalize_impl(
                        working, plan, steps, any_replayed,
                        call_llm, call_sync, fixed_reply=EVIDENCE_STOP_REPLY,
                        historical_evidence=historical_evidence,
                    )

                steps.append(ToolStep(name=name, arguments=args, result=result, replayed=replayed))
                working.append({
                    "role": "tool", "tool_call_id": tc.id, "content": result_str,
                })

                from app.services.commerce.writes import PREPARES
                if result.get("success") and name in PREPARES | {"ask_user", "handoff_to_human"}:
                    data = result.get("data", {})
                    state = data.get("next_state")
                    if state == "waiting_user":
                        return make_result(data["question"])
                    if state == "needs_human":
                        return make_result(data["reply"], needs_human=True)
                    if state == "needs_confirmation":
                        return make_result("已准备办理内容，请查看并确认操作卡片。", needs_confirmation=True)

                # 知识检索无结果 → fail-closed（对重试后的最终结果判定）
                if name == "search_knowledge" and self._knowledge_unavailable(result):
                    return ExecutionResult(
                        intent=plan.intent, execution=plan.execution,
                        reply=KNOWLEDGE_UNAVAILABLE_REPLY, tool_steps=steps,
                        replayed=any_replayed,
                    )

        # 达到步数上限：禁用工具再请求一次，强制基于已有证据收尾
        return await self._finalize_impl(
            working, plan, steps, any_replayed, call_llm, call_sync,
        )

    # -- 内部 helpers -------------------------------------------------------

    async def _finalize_impl(
        self,
        working: list[dict],
        plan: DataPlan,
        steps: list[ToolStep],
        any_replayed: bool,
        call_llm: Callable[..., Any],
        call_sync: Callable[..., Any],
        fixed_reply: str | None = None,
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """禁用工具，要求模型仅基于已有证据组织最终答复，并做 Evidence 校验。"""
        if fixed_reply is None:
            nudged = [*working, {
                "role": "system",
                "content": (
                    "本轮处理即将结束，请停止调用工具，仅基于以上工具返回的信息"
                    "组织最终答复。信息不足时如实告知用户，并建议补充信息或转人工，"
                    "不要编造任何业务数据。"
                ),
            }]
            response = await call_llm(
                model=self.model, messages=nudged, temperature=self.temperature,
                **self.chat_completion_options,
            )
            reply = (response.choices[0].message.content or "").strip()
            reply = (reply.rstrip("\u3002") + "\u3002" + EVIDENCE_FINALIZE_NOTE) if reply else EVIDENCE_FINALIZE_NOTE
        else:
            reply = fixed_reply
        original_reply = reply
        report = verify_reply(reply, [*(historical_evidence or []), *business_evidence(steps)])
        if report.has_unsupported:
            reply = sanitize_reply(reply, report)
        from app.observability.interactions import record
        record("reply.validation", status="corrected" if report.has_unsupported else "ok",
               input=original_reply, output=reply, report=report)
        return ExecutionResult(
            intent=plan.intent, execution=plan.execution,
            reply=reply, tool_steps=steps, replayed=any_replayed,
        )

    @staticmethod
    def _tools_for(plan: DataPlan) -> list[dict]:
        """从 registry 的 schema 裁剪出 plan 需要的工具定义。"""
        from app.tools.registry import TOOL_DEFINITIONS
        wanted = plan.prompt_tool_names
        return [td for td in TOOL_DEFINITIONS if td["function"]["name"] in wanted]

    @staticmethod
    def _safe_arguments(raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _parse_result(result_str: str) -> dict[str, Any]:
        try:
            data = json.loads(result_str)
        except (TypeError, json.JSONDecodeError):
            return {"success": False, "raw": str(result_str)}
        return data if isinstance(data, dict) else {"success": True, "value": data}

    @staticmethod
    def _knowledge_unavailable(result: dict[str, Any]) -> bool:
        if result.get("success") is not True:
            return True
        return not bool(result.get("results"))
