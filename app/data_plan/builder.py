# 文件说明：根据服务端明确白名单构建单次 ReAct 执行的工具边界。
"""Build the ReAct tool boundary from an explicit server-owned task whitelist."""
from dataclasses import dataclass
from typing import Collection

from app.data_plan.enums import Execution, Intent


@dataclass(frozen=True)
class DataPlan:
    intent: Intent
    tools: tuple[str, ...]
    execution: Execution = Execution.REACT

    @property
    def prompt_tool_names(self) -> frozenset[str]:
        return frozenset(self.tools)


def build_task_plan(intent: Intent | str, *, allowed_tools: Collection[str]) -> DataPlan:
    """Labels describe results; only the harness decides which tools are allowed."""
    return DataPlan(intent=Intent(intent), tools=tuple(sorted(set(allowed_tools))))
