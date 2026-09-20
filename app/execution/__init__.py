"""执行层：按 IntentSpec 的执行路径分发到 Workflow / ReAct。

管线：意图分类器 → Data Plan 装配 → :func:`dispatch` → 执行 → Evidence Pack
（:class:`ExecutionResult`）→ Response Agent → Session/Case 更新。

执行路径已合并为两路（对话流并入 Workflow，不再有独立 Rule 路径）：
- WORKFLOW：确定性工具编排（含对话流类：问候/收尾/确认/回答，无业务工具，
  回复由 reply_generator 生成或模板兜底）；
- REACT：LLM + 工具循环（理解 + 判断 + 灵活组合）。

异步入口 ``dispatch_async`` 供 API 生产路径使用：LLM 调用 ``await`` 不占线程，
工具 / DB 调用 ``asyncio.to_thread`` 不阻塞事件循环。
"""

from __future__ import annotations

from app.data_plan import DataPlan
from app.data_plan.enums import Execution
from app.execution.react import ReactExecutor
from app.execution.runner import ToolRunner
from app.execution.types import ExecutionResult, ToolStep
from app.execution.workflow import ReplyGenerator, run_workflow, run_workflow_async
from app.intent.query_rewrite import EntityExtraction

__all__ = [
    "ExecutionResult",
    "ToolStep",
    "ToolRunner",
    "ReactExecutor",
    "ReplyGenerator",
    "run_workflow",
    "run_workflow_async",
    "dispatch",
    "dispatch_async",
]


def dispatch(
    plan: DataPlan,
    *,
    user_input: str = "",
    messages: list[dict] | None = None,
    runner: ToolRunner | None = None,
    react_executor: ReactExecutor | None = None,
    confirmed: bool = False,
    reply_generator: ReplyGenerator | None = None,
    mutation_guard=None,
    confirmation_token: str | None = None,
    entities: EntityExtraction | None = None,
    historical_evidence: list[dict] | None = None,
) -> ExecutionResult:
    """按 ``plan.execution`` 分发到对应执行路径。

    - WORKFLOW：确定性脚本（需 ``user_input`` 与 ``runner``；可选
      ``reply_generator`` 把工具结果交给 LLM 生成最终回复，对话流类同）。
    - REACT：LLM 动态规划（需 ``react_executor`` 与 ``messages``）。
    - ``entities``：实体抽取层透传的通用实体（订单号/物流号/状态等），
      经 ``run_workflow`` 注入声明了该形参的 handler。
    """
    if plan.execution == Execution.WORKFLOW:
        return run_workflow(
            plan.intent, user_input, plan,
            runner if runner is not None else ToolRunner(),
            confirmed=confirmed,
            reply_generator=reply_generator,
            entities=entities,
        )

    if plan.execution == Execution.REACT:
        if react_executor is None:
            raise ValueError("ReAct 路径需要提供 react_executor")
        return react_executor.run(
            messages or [], plan, confirmed=confirmed,
            mutation_guard=mutation_guard, confirmation_token=confirmation_token,
            historical_evidence=historical_evidence,
        )

    raise ValueError(f"未知执行路径: {plan.execution}")


async def dispatch_async(
    plan: DataPlan,
    *,
    user_input: str = "",
    messages: list[dict] | None = None,
    runner: ToolRunner | None = None,
    react_executor: ReactExecutor | None = None,
    confirmed: bool = False,
    async_reply_generator: ReplyGenerator | None = None,
    mutation_guard=None,
    confirmation_token: str | None = None,
    entities: EntityExtraction | None = None,
    historical_evidence: list[dict] | None = None,
) -> ExecutionResult:
    """异步分发入口（API 生产路径）。

    - WORKFLOW：``await run_workflow_async`` — sync 确定性脚本放线程池，
      成功路径的回复走 ``await async_reply_generator``（LLM 不占线程）。
    - REACT：``await react_executor.run_async`` — LLM ``await``，工具 ``asyncio.to_thread``。
    - ``entities``：实体抽取层透传的通用实体，经 ``run_workflow_async`` 注入 handler。
    """
    if plan.execution == Execution.WORKFLOW:
        return await run_workflow_async(
            plan.intent, user_input, plan,
            runner if runner is not None else ToolRunner(),
            confirmed=confirmed,
            async_reply_generator=async_reply_generator,
            entities=entities,
        )

    if plan.execution == Execution.REACT:
        if react_executor is None:
            raise ValueError("ReAct 路径需要提供 react_executor")
        return await react_executor.run_async(
            messages or [], plan, confirmed=confirmed,
            mutation_guard=mutation_guard, confirmation_token=confirmation_token,
            historical_evidence=historical_evidence,
        )

    raise ValueError(f"未知执行路径: {plan.execution}")
