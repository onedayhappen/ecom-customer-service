"""统一对话执行器：直接回复或按需进行 LLM + 工具循环。

与 Workflow 的差别在于：下一步无法预先写死，需 Agent 根据工具返回动态规划。
默认接待全部 31 类意图，使用统一对话策略；历史意图剧本仅用于回滚模式。

工具集来自 :class:`DataPlan` 的 ``prompt_tool_names``（已实现能力 + load_skill），
因此本执行器**不会**碰到未实现工具。知识检索无结果时 fail-closed，绝不编造政策。

可靠执行策略（由 :class:`~app.reliability.failure_policy.ReActPolicy` 驱动）：

> ReAct 可以决定"查什么"，但不能决定"失败后无限试、没有证据也回答、
> 状态未知仍继续写"。

- 重复调用（同工具+同参数）被拦截并回放缓存结果；
- 读工具瞬时失败在执行器层透明重试一次（模型不感知中间失败）；
- 写工具瞬时失败永不自动重试，先查询幂等状态，状态未知即转人工；
- 全链路连续失败达到 3 次立即终止并降级（澄清/转人工/基于证据收尾）；
- 最终回复经过 Evidence Pack 校验：订单号/金额/状态没有工具证据支撑的
  句子被替换为保守表述。

异步化设计（适配器模式）：
- ``_run_impl`` 是共享核心逻辑（async），通过 ``call_llm`` 和 ``call_sync``
  两个适配器抽象 LLM 调用与同步工具/DB 调用；
- ``run()``（sync，测试/CLI 兼容）提供同步适配器——LLM 直接调用、工具直接执行；
- ``run_async()``（async，API 生产路径）提供异步适配器——LLM ``await`` 不占线程、
  工具 ``asyncio.to_thread`` 不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from app.data_plan import DataPlan
from app.data_plan.capabilities import MUTATING_CAPABILITIES, get_capability
from app.data_plan.enums import Permission
from app.execution.types import ExecutionResult, ToolStep
from app.harness.convergence import CONVERGENCE_REPLY
from app.prompts.react_guidelines import REACT_GUIDELINES
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

IN_PROGRESS_REPLY = "该操作正在处理中，请勿重复提交，稍后可再次查询处理结果。"

ToolExecutor = Callable[[str, dict[str, Any]], str]


def business_evidence(steps: list[ToolStep]) -> list[dict]:
    """Conversation drafts may contain user claims, never proof of business facts."""
    return [step.result for step in steps if step.name not in {'read_case_memory', 'save_case_context'}]


def default_system_prompt(plan: DataPlan) -> str:
    """从 IntentSpec 生成默认 system prompt。

    结构 = 动态装配（任务描述 + DataPlan 裁剪的工具清单）+ 通用规则
    + 按意图注入的行为准则（改编自旧子 Agent prompt，见
    ``app/prompts/react_guidelines.py``）。工具清单不写死，
    保证与 DataPlan 装配一致；准则提供角色定位与回复规范。
    """
    if plan.conversational:
        from app.prompts.conversation import CONVERSATION_PROMPT
        return CONVERSATION_PROMPT + f'\n本轮参考标签：{plan.intent.value}（{plan.description}）；以当前对话实际需求为准。'
    tool_lines = [
        f"- {name}: {get_capability(name).description}"
        for name in plan.tools
    ]
    tools_text = "\n".join(tool_lines) if tool_lines else "（无）"
    parts = [
        "你是并夕夕平台的电商客服助手。",
        f"当前任务：{plan.description}",
        "可用工具：\n" + tools_text,
        (
            "通用规则：\n"
            "1. 只基于工具返回的信息回答，绝不编造订单、商品或政策数据；\n"
            "2. 需要查数据时调用最必要的工具，信息足够时直接回复；\n"
            "3. 知识库检索无结果时，如实告知无法确认，不要臆测；\n"
            "4. 需要用户确认或补充信息时，直接提问；\n"
            "5. 不要用相同参数重复调用同一工具。"
        ),
    ]
    guideline = REACT_GUIDELINES.get(plan.intent)
    if guideline:
        parts.append(guideline)
    return "\n\n".join(parts)


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
        question_fields: tuple[str, ...] | None = None,
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
        self.question_fields = question_fields
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
    # 同步入口（测试 / CLI 兼容）
    # ------------------------------------------------------------------

    def run(
        self,
        messages: list[dict],
        plan: DataPlan,
        max_steps: int = 5,
        tool_definitions: list[dict] | None = None,
        confirmed: bool = False,
        mutation_guard=None,
        confirmation_token: str | None = None,
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """同步执行 ReAct 循环（测试 / CLI 兼容路径）。

        通过 ``asyncio.run`` 调用共享核心 ``_run_impl``，适配器为同步直调：
        LLM 直接 ``client.chat.completions.create()``，工具直接 ``self._execute()``。
        """
        async def call_llm(**kw: Any) -> Any:
            return self.client.chat.completions.create(**kw)

        async def call_sync(func: Callable, *a: Any, **kw: Any) -> Any:
            return func(*a, **kw)

        return asyncio.run(self._run_impl(
            messages, plan, max_steps, tool_definitions, confirmed,
            mutation_guard, confirmation_token, call_llm, call_sync,
            historical_evidence,
        ))

    # ------------------------------------------------------------------
    # 异步入口（API 生产路径）
    # ------------------------------------------------------------------

    async def run_async(
        self,
        messages: list[dict],
        plan: DataPlan,
        max_steps: int = 5,
        tool_definitions: list[dict] | None = None,
        confirmed: bool = False,
        mutation_guard=None,
        confirmation_token: str | None = None,
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """异步执行 ReAct 循环（API 生产路径）。

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
            messages, plan, max_steps, tool_definitions, confirmed,
            mutation_guard, confirmation_token, call_llm, call_sync,
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
        confirmed: bool,
        mutation_guard,
        confirmation_token: str | None,
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
        # 未在 schema 中声明的函数名。真正的白名单来自静态 DataPlan。
        allowed_tool_names = set(plan.prompt_tool_names)
        from app.support.runtime import current_scope
        if current_scope() or plan.conversational:
            # This is a conversation control tool, not a business capability.
            tools = [*tools, {'type': 'function', 'function': {
                'name': 'ask_user', 'description': '缺少必要信息时向用户提问并暂停当前任务。回答不会授权退款等写操作。',
                'parameters': {'type': 'object', 'properties': {
                    'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                    'field': {'type': 'string', 'minLength': 1, 'maxLength': 100,
                              **({'enum': list(self.question_fields)} if self.question_fields else {})},
                    'options': {'type': 'array', 'maxItems': 5, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
                }, 'required': ['prompt'], 'additionalProperties': False},
            }}]
            allowed_tool_names.add('ask_user')
            system_prompt += '\n缺少必要信息时调用 ask_user 提问并暂停，支持选项和自由回答。不可将回答问题视为写操作确认。'
        policy = ReActPolicy(mutating_tools=frozenset(MUTATING_CAPABILITIES))

        working: list[dict] = [{"role": "system", "content": system_prompt}, *messages]
        steps: list[ToolStep] = []
        any_replayed = False

        def make_result(reply: str, **kwargs: Any) -> ExecutionResult:
            """终点出口：Evidence Pack 校验后构造 ExecutionResult。"""
            report = verify_reply(reply, [*(historical_evidence or []), *business_evidence(steps)])
            if report.has_unsupported:
                reply = sanitize_reply(reply, report)
            return ExecutionResult(
                intent=plan.intent, execution=plan.execution,
                reply=reply, tool_steps=steps, replayed=any_replayed, **kwargs,
            )

        for _ in range(max_steps):
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
                tools=tools,
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

                if name == 'ask_user':
                    prompt, options = args.get('prompt'), args.get('options', [])
                    field = args.get('field', 'details')
                    if (not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 1000
                            or not isinstance(field, str) or not 0 < len(field.strip()) <= 100
                            or not isinstance(options, list) or len(options) > 5
                            or any(not isinstance(o, str) or not 0 < len(o.strip()) <= 200 for o in options)):
                        return make_result('提问信息不完整，需要人工协助核实。', needs_human=True)
                    from app.tasks.routing import ORDER
                    known_orders = set()
                    for step in steps:
                        if step.success:
                            if step.name == 'query_order':
                                known_orders.add(str((step.result.get('order') or {}).get('order_id', '')).upper())
                            if step.name == 'list_user_orders':
                                known_orders.update(str(o.get('order_id', '')).upper() for o in step.result.get('orders', []))
                    options = [option for option in dict.fromkeys(options)
                               if all(order.upper() in known_orders for order in ORDER.findall(option))]
                    result = make_result(prompt)
                    result.question = {'field': field, 'prompt': result.reply,
                                       'options': [{'value': option, 'label': option} for option in options]}
                    return result

                # 第13期：收敛检测（规则3 工具调用指纹）——相同工具+参数连续重复
                if self.convergence_detector is not None:
                    report = self.convergence_detector.record_tool_call(name, args)
                    if report is not None:
                        return make_result(CONVERGENCE_REPLY)

                # Unified conversation: authorization depends on the tool, never the label.
                mutation_intent = plan.intent.value
                if plan.conversational and name in MUTATING_CAPABILITIES:
                    from app.execution.proposals import MUTATION_INTENTS, prepare_proposal
                    mutation_intent = MUTATION_INTENTS.get(name, plan.intent.value)
                    if mutation_guard is None:
                        return make_result('当前缺少安全执行上下文，操作未执行，请重新进入会话或联系人工。', blocked=True)
                    if not confirmation_token:
                        proposal = await call_sync(prepare_proposal, name, args, self._observed_execute)
                        steps.extend(proposal.steps)
                        if proposal.error:
                            result = {'success': False, 'error_code': 'validation_error', 'error': proposal.error}
                            steps.append(ToolStep(name, args, result, blocked=True))
                            working.append({'role': 'tool', 'tool_call_id': tc.id,
                                            'content': json.dumps(result, ensure_ascii=False)})
                            continue
                        token = await call_sync(mutation_guard.request_confirmation, mutation_intent, name, args)
                        return ExecutionResult(intent=plan.intent, execution=plan.execution,
                                               reply=proposal.summary, tool_steps=steps,
                                               needs_confirmation=True, confirmation_token=token)

                # Legacy plans retain their historical confirmation contract.
                # CONFIRM_REQUIRED 意图的写操作工具在未 confirmed 时不真正执行，
                # 返回 needs_confirmation=True，等待用户确认后再带 confirmed=True 重跑。
                if (
                    name in MUTATING_CAPABILITIES
                    and not plan.conversational
                    and plan.permission == Permission.CONFIRM_REQUIRED
                    and not confirmed
                ):
                    token = None
                    if mutation_guard is not None:
                        token = await call_sync(
                            mutation_guard.request_confirmation,
                            plan.intent.value, name, args,
                        )
                    return ExecutionResult(
                        intent=plan.intent, execution=plan.execution,
                        reply="该操作需要您确认后才能执行，请问是否确认继续？",
                        tool_steps=steps,
                        needs_confirmation=True,
                        confirmation_token=token,
                    )

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

                # 执行工具（写操作走 MutationGuard 幂等链）
                replayed = False
                if mutation_guard is not None and name in MUTATING_CAPABILITIES:
                    outcome = await call_sync(
                        mutation_guard.execute,
                        mutation_intent, name, args, confirmation_token,
                        self._observed_execute,
                    )
                    result = dict(outcome.result)
                    replayed = outcome.replayed
                    if replayed:
                        result["replayed"] = True
                        any_replayed = True
                    result_str = json.dumps(result, ensure_ascii=False)
                else:
                    result_str = await call_sync(self._observed_execute, name, args)
                    result = self._parse_result(result_str)

                # 失败策略决策
                post = policy.after_call(name, args, result)
                if post.action is Action.RETRY_ONCE:
                    # 读工具瞬时失败：同参数透明重试一次，模型只看到最终结果
                    result_str = await call_sync(self._observed_execute, name, args)
                    result = self._parse_result(result_str)
                    post = policy.after_call(name, args, result)

                if post.action is Action.CHECK_IDEMPOTENCY:
                    # 写操作瞬时失败：查询幂等状态，状态未知即转人工，绝不重提
                    if mutation_guard is None:
                        return make_result(HANDOFF_REPLY, needs_human=True)
                    status = await call_sync(
                        mutation_guard.query_idempotency_status,
                        mutation_intent, name, args,
                    )
                    state = status.get("status")
                    if state == "succeeded":
                        result = dict(status.get("result") or {})
                        result["note"] = "检测到此前执行已成功，以上为该操作的权威结果"
                        result_str = json.dumps(result, ensure_ascii=False)
                    elif state == "in_progress" and not status.get("stale"):
                        return make_result(IN_PROGRESS_REPLY)
                    else:
                        # unknown / in_progress 已过期 / failed / not_found → 转人工
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
        report = verify_reply(reply, [*(historical_evidence or []), *business_evidence(steps)])
        if report.has_unsupported:
            reply = sanitize_reply(reply, report)
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
