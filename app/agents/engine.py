"""统一执行引擎（替代旧 MultiAgentOrchestrator 的 Router + SubAgent + ReAct）。

新链路入口：意图识别 → IntentSpec → Data Plan → :func:`dispatch` 执行 →
包装为 :class:`CustomerServiceResponse`。Session / Case / LTM 等外壳能力
由调用方（API / 评测沙箱）按需组合，引擎本身保持纯执行职责。

- WORKFLOW：确定性脚本（ToolRunner 直跑；成功路径的回复可注入 LLM 生成器，
  把工具返回结果交给模型表述——见 :meth:`_workflow_reply_generator`）
- REACT：LLM + 工具循环（ReactExecutor，需 client/model）
- RULE：对话流固定回复（dispatch 兜底，活跃 Case 时由决策规则接管）
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from app.agents.response import CustomerServiceResponse
from app.data_plan import DataPlan, build_data_plan
from app.data_plan.enums import Execution, Intent
from app.execution import (
    ReactExecutor,
    ReplyGenerator,
    ToolRunner,
    dispatch,
    dispatch_async,
)
from app.execution.types import ExecutionResult, ToolStep
from app.prompts.react_guidelines import WORKFLOW_REPLY_GUIDELINES


class AgentEngine:
    """意图 → DataPlan → dispatch → 响应 的统一执行入口。"""

    def __init__(
        self,
        intent: Intent | str,
        *,
        client: Any | None = None,
        model: str = "",
        temperature: float = 0.0,
    ):
        self.intent = Intent(intent) if isinstance(intent, str) else intent
        self.plan: DataPlan = build_data_plan(self.intent)
        self.client = client
        self.model = model
        self.temperature = temperature

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> DataPlan:
        """返回装配好的 DataPlan（供外部观测 / 测试断言）。"""
        return self.plan

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute(
        self,
        user_input: str = "",
        *,
        messages: list[dict] | None = None,
        confirmed: bool = False,
        runner: ToolRunner | None = None,
        react_executor: ReactExecutor | None = None,
        mutation_guard=None,
        confirmation_token: str | None = None,
        entities=None,
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """按 plan.execution 分发执行，返回 Evidence Pack（ExecutionResult）。

        ``entities``：实体抽取层透传的通用实体（订单号/物流号/状态等），下游
        Workflow handler 直接复用，不再自行重复解析原文。
        """
        if self.plan.execution == Execution.REACT:
            if react_executor is None:
                if self.client is None:
                    raise ValueError("REACT 路径需要 client 与 model（LLM 动态规划）")
                react_executor = ReactExecutor(
                    client=self.client,
                    model=self.model,
                    temperature=self.temperature,
                )
            msgs = messages if messages is not None else [
                {"role": "user", "content": user_input},
            ]
            return dispatch(
                self.plan,
                user_input=user_input,
                messages=msgs,
                react_executor=react_executor,
                confirmed=confirmed,
                mutation_guard=mutation_guard,
                confirmation_token=confirmation_token,
                historical_evidence=historical_evidence,
                entities=entities,
            )
        if runner is None:
            runner = ToolRunner(
                mutation_guard=mutation_guard,
                mutation_intent=self.intent.value,
                confirmation_token=confirmation_token,
            )
        return dispatch(
            self.plan,
            user_input=user_input,
            runner=runner,
            confirmed=confirmed,
            reply_generator=self._workflow_reply_generator(messages),
            mutation_guard=mutation_guard,
            confirmation_token=confirmation_token,
            entities=entities,
            historical_evidence=historical_evidence,
        )

    async def execute_async(
        self,
        user_input: str = "",
        *,
        messages: list[dict] | None = None,
        confirmed: bool = False,
        runner: ToolRunner | None = None,
        react_executor: ReactExecutor | None = None,
        mutation_guard=None,
        confirmation_token: str | None = None,
        entities=None,
        historical_evidence: list[dict] | None = None,
    ) -> ExecutionResult:
        """异步执行（API 生产路径）：LLM 调用 await 不占线程，工具调用进线程池。

        与 :meth:`execute` 同参同语义，分发到 :func:`dispatch_async`；
        WORKFLOW 成功路径的回复走 ``await`` LLM（见
        :meth:`_async_workflow_reply_generator`），确定性脚本本体放线程池。
        ``entities`` 同 :meth:`execute`（实体抽取层透传的通用实体）。
        """
        if self.plan.execution == Execution.REACT:
            if react_executor is None:
                if self.client is None:
                    raise ValueError("REACT 路径需要 client 与 model（LLM 动态规划）")
                react_executor = ReactExecutor(
                    client=self.client,
                    model=self.model,
                    temperature=self.temperature,
                )
            msgs = messages if messages is not None else [
                {"role": "user", "content": user_input},
            ]
            return await dispatch_async(
                self.plan,
                user_input=user_input,
                messages=msgs,
                react_executor=react_executor,
                confirmed=confirmed,
                mutation_guard=mutation_guard,
                confirmation_token=confirmation_token,
                historical_evidence=historical_evidence,
                entities=entities,
            )
        if runner is None:
            runner = ToolRunner(
                mutation_guard=mutation_guard,
                mutation_intent=self.intent.value,
                confirmation_token=confirmation_token,
            )
        return await dispatch_async(
            self.plan,
            user_input=user_input,
            runner=runner,
            confirmed=confirmed,
            async_reply_generator=self._async_workflow_reply_generator(messages),
            mutation_guard=mutation_guard,
            confirmation_token=confirmation_token,
            entities=entities,
            historical_evidence=historical_evidence,
        )

    # ------------------------------------------------------------------
    # Workflow 回复生成器（LLM 表述层）
    # ------------------------------------------------------------------
    def _workflow_reply_generator(
        self, messages: list[dict] | None = None,
    ) -> ReplyGenerator | None:
        """成功路径的回复生成器：把工具返回结果 + 最近几轮对话 交给 LLM 组织语言。

        **回复优先级**：已配置 client/model 时始终优先走 LLM；只有 LLM 抛异常
        （网络/鉴权/限流）或返回空内容时才由 ``_compose_reply`` 回退到硬编码
        模板，保证客服回复永远不空、不崩。未配置 client/model（纯本地/测试）
        时返回 None，``_compose_reply`` 直接走模板兜底，行为不变。

        ``messages`` 即意图识别阶段使用的同一份最近轮次上下文（history +
        当前用户句）；与 tool_steps（本次工具证据）一起进入 LLM，使回复能
        结合多轮上下文（指代、追问、澄清）而不只依赖当前这一句。无 history
        时退化为仅当前句，保持单轮行为不变。
        """
        if self.client is None or not self.model:
            return None
        client = self.client
        model = self.model

        def generate(plan: DataPlan, tool_steps: list[ToolStep], user_input: str) -> str:
            system = self._workflow_reply_system(plan, tool_steps)
            # 多轮上下文：复用意图识别阶段的同一份最近轮次；为空则退化为仅当前句
            conv = list(messages) if messages else []
            if not conv:
                conv = [{"role": "user", "content": user_input}]
            # 第12期：LlmClient 绑定 workflow_reply 场景（token 按场景分开统计）
            llm = client.bind_scene("workflow_reply") if hasattr(client, "bind_scene") else client
            response = llm.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    *conv,
                ],
                temperature=self.temperature,
            )
            # 空回复视为失败，交由 _compose_reply 走模板兜底（绝不返回空串）
            content = (response.choices[0].message.content or "").strip()
            if not content:
                raise RuntimeError("workflow reply LLM returned empty content")
            return content

        return generate

    def _async_workflow_reply_generator(
        self, messages: list[dict] | None = None,
    ) -> ReplyGenerator | None:
        """成功路径的异步回复生成器：LLM 表述层走 ``await``，不占线程。

        与 :meth:`_workflow_reply_generator` 同语义（prompt 构造与多轮上下文
        处理完全一致）：``messages`` 复用意图识别阶段的同一份最近轮次，与工具
        证据一起进入 LLM；**优先走 LLM，LLM 异常或空回复时由调用方回退模板**。
        区别仅在 LLM 调用方式：要求 client 为 AsyncOpenAI
        风格（``await client.chat.completions.create(...)``）。未配置
        client/model 时返回 None（回退模板拼接）。
        """
        if self.client is None or not self.model:
            return None
        client = self.client
        model = self.model

        async def generate(plan: DataPlan, tool_steps: list[ToolStep], user_input: str) -> str:
            system = self._workflow_reply_system(plan, tool_steps)
            # 多轮上下文：复用意图识别阶段的同一份最近轮次；为空则退化为仅当前句
            conv = list(messages) if messages else []
            if not conv:
                conv = [{"role": "user", "content": user_input}]
            # 第12期：LlmClient 绑定 workflow_reply 场景（token 按场景分开统计）
            llm = client.bind_scene("workflow_reply") if hasattr(client, "bind_scene") else client
            # 兼容 LlmClient（暴露 acreate）与 AsyncOpenAI（无 acreate）
            create = getattr(llm.chat.completions, "acreate", None)
            if create is not None:
                response = await create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        *conv,
                    ],
                    temperature=self.temperature,
                )
            else:
                response = await llm.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        *conv,
                    ],
                    temperature=self.temperature,
                )
            # 空回复视为失败，交由 run_workflow_async 保留模板兜底（绝不返回空串）
            content = (response.choices[0].message.content or "").strip()
            if not content:
                raise RuntimeError("workflow reply LLM returned empty content")
            return content

        return generate

    @staticmethod
    def _workflow_reply_system(plan: DataPlan, tool_steps: list[ToolStep]) -> str:
        """构造 Workflow 回复生成的 system prompt（sync / async 共用）。"""
        tone = WORKFLOW_REPLY_GUIDELINES.get(plan.spec.domain, "语气自然、友好、简洁。")
        if tool_steps:
            # 业务路径：system 携带工具返回的真实证据，模型只能基于数据回答
            evidence = json.dumps(
                [asdict(step) for step in tool_steps], ensure_ascii=False,
            )
            return (
                "你是电商客服助手。以下是系统查询到的真实业务数据（JSON 格式），"
                "请基于这些数据用自然、友好的客服语气回答用户；"
                "只能使用数据中出现的信息，不得编造订单、物流、退款、价格等任何内容。\n\n"
                f"当前任务：{plan.description}\n\n"
                f"业务数据：\n{evidence}\n\n"
                f"语气要求：{tone}"
            )
        # 对话流路径（问候/收尾/确认/回答）：无业务数据，自然回应即可
        return (
            "你是电商客服助手。用户发来一句对话（问候、告别、确认或回答客服提问），"
            "请用自然、友好、简洁的客服语气回应，不要编造任何订单或业务信息。\n\n"
            f"当前任务：{plan.description}\n\n"
            f"语气要求：{tone}"
        )

    # ------------------------------------------------------------------
    # 包装响应
    # ------------------------------------------------------------------
    def chat(
        self,
        user_input: str,
        *,
        messages: list[dict] | None = None,
        confirmed: bool = False,
        mutation_guard=None,
        confirmation_token: str | None = None,
    ) -> CustomerServiceResponse:
        """执行并包装为对外响应（API / 评测共用）。"""
        result = self.execute(
            user_input, messages=messages, confirmed=confirmed,
            mutation_guard=mutation_guard, confirmation_token=confirmation_token,
        )
        return self._wrap_response(result)

    async def chat_async(
        self,
        user_input: str,
        *,
        messages: list[dict] | None = None,
        confirmed: bool = False,
        mutation_guard=None,
        confirmation_token: str | None = None,
    ) -> CustomerServiceResponse:
        """异步执行并包装为对外响应（API 生产路径）。与 :meth:`chat` 同语义。"""
        result = await self.execute_async(
            user_input, messages=messages, confirmed=confirmed,
            mutation_guard=mutation_guard, confirmation_token=confirmation_token,
        )
        return self._wrap_response(result)

    def _wrap_response(self, result: ExecutionResult) -> CustomerServiceResponse:
        """ExecutionResult → CustomerServiceResponse（sync / async 共用）。"""
        follow_up = None
        if result.needs_confirmation:
            follow_up = "请确认是否继续该操作？"
        elif result.needs_human:
            follow_up = (
                "已创建人工工单，请稍候。" if result.human_handoff_created
                else "当前需要人工介入，但系统尚未创建人工工单，请联系客服继续处理。"
            )

        confidence = 0.95 if result.execution != Execution.REACT else 0.80
        return CustomerServiceResponse(
            intent=self.intent,
            confidence=confidence,
            reply=result.reply,
            requires_human=result.needs_human,
            follow_up_question=follow_up,
        )
