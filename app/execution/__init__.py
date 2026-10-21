"""共享 ReAct 工具循环与结果类型；历史执行标签不再提供回滚路径。"""

from __future__ import annotations

from app.data_plan import DataPlan
from app.data_plan.enums import Execution
from app.execution.react import ReactExecutor
from app.execution.runner import ToolRunner
from app.execution.types import ExecutionResult, ToolStep

__all__ = [
    "ExecutionResult",
    "ToolStep",
    "ToolRunner",
    "ReactExecutor",
    "dispatch",
    "dispatch_async",
]


def unavailable_execution(plan: DataPlan) -> ExecutionResult:
    from app.data_plan.taxonomy import DEFINITIONS
    title = DEFINITIONS[plan.intent][0]
    return ExecutionResult(
        intent=plan.intent, execution=plan.execution, blocked=True,
        reply=f"已识别您的{title}需求，该业务执行路径尚未接入，本次未提交任何业务操作。您可以联系人工客服处理。",
    )


def dispatch(
    plan: DataPlan,
    *,
    user_input: str = "",
    messages: list[dict] | None = None,
    runner: ToolRunner | None = None,
    react_executor: ReactExecutor | None = None,
    confirmed: bool = False,
    mutation_guard=None,
    confirmation_token: str | None = None,
    entities=None,
    historical_evidence: list[dict] | None = None,
) -> ExecutionResult:
    """执行受支持的工具循环，未接入的能力返回明确阻断结果。"""
    if not plan.spec.execution_ready:
        return unavailable_execution(plan)
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
    mutation_guard=None,
    confirmation_token: str | None = None,
    entities=None,
    historical_evidence: list[dict] | None = None,
) -> ExecutionResult:
    """异步工具循环；同步工具在线程中调用。"""
    if not plan.spec.execution_ready:
        return unavailable_execution(plan)
    if plan.execution == Execution.REACT:
        if react_executor is None:
            raise ValueError("ReAct 路径需要提供 react_executor")
        return await react_executor.run_async(
            messages or [], plan, confirmed=confirmed,
            mutation_guard=mutation_guard, confirmation_token=confirmation_token,
            historical_evidence=historical_evidence,
        )

    raise ValueError(f"未知执行路径: {plan.execution}")
