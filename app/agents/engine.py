"""统一对话 Agent：全部分类标签使用同一个可选工具循环。

默认使用 build_conversation_plan：标签是参考，当前输入决定对话走向；没有
工具调用时直接回复。确认、参数校验和业务写入由服务端保护层执行。
Workflow 回滚入口已经退役。
Session / Case / LTM 由 API 或评测调用方装配。
"""

from __future__ import annotations

from typing import Any

from app.agents.response import CustomerServiceResponse
from app.data_plan import DataPlan
from app.data_plan.builder import build_conversation_plan
from app.data_plan.enums import Execution, Intent
from app.execution import (
    ReactExecutor,
    ToolRunner,
    dispatch,
    dispatch_async,
)
from app.execution.types import ExecutionResult


class AgentEngine:
    """统一对话执行入口；分类结果不授予任何业务操作权限。"""

    def __init__(
        self,
        intent: Intent | str,
        *,
        client: Any | None = None,
        model: str = "",
        temperature: float = 0.0,
    ):
        self.intent = Intent(intent) if isinstance(intent, str) else intent
        self.plan: DataPlan = build_conversation_plan(self.intent)
        self.client = client
        self.model = model
        self.temperature = temperature

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> DataPlan:
        """返回装配好的 DataPlan（供外部观测 / 测试断言）。"""
        return self.plan

    def associate(self, context):
        """Propose message ownership using the same client, never execute tools."""
        from app.agents.association import propose
        if self.client is None:
            raise ValueError("Association requires the configured conversation client")
        return propose(self.client, self.model, context)

    def execute_task(self, store, authenticated_user_id, request, *, tools=None, scope=None):
        """Advance one durable matter through this Agent's existing tool loop."""
        from app.tasks.execution import TaskExecutor
        return TaskExecutor(store, authenticated_user_id, tools=tools, scope=scope).run(request, self)

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
        """运行统一工具循环并返回可校验的执行结果。"""
        if not self.plan.spec.execution_ready:
            return dispatch(self.plan)
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
        raise ValueError('Only the shared Agent tool loop is supported')

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
        if not self.plan.spec.execution_ready:
            return await dispatch_async(self.plan)
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
        raise ValueError('Only the shared Agent tool loop is supported')

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
