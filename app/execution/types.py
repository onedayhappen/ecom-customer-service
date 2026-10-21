"""Task execution results and tool evidence used by the Harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.data_plan.enums import Execution, Intent


@dataclass(frozen=True)
class ToolStep:
    """一次工具调用及其结果（证据包单元）。

    ``blocked=True`` 表示该工具能力未实现、被执行器显式降级，未真正调用。
    """

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    blocked: bool = False
    # 高风险写操作的确认令牌。仅在请求确认时返回给调用方；服务端只保存摘要。
    confirmation_token: str | None = None
    # 幂等命中时复用此前成功结果，而非再次调用下游。
    replayed: bool = False
    @property
    def success(self) -> bool:
        return bool(self.result.get("success")) and not self.blocked


@dataclass
class ExecutionResult:
    """一次执行路径的完整产物。

    - ``reply``            最终给用户的文本。
    - ``tool_steps``       证据包：本次所有工具调用及结果，供事项进度与审计。
    - ``needs_confirmation`` 写操作类在未确认时的门控信号（对应 CONFIRM_REQUIRED）。
    - ``needs_human``      是否转人工（降级 / 升级）。
    - ``blocked``          是否因关键工具未实现而无法完整执行。
    """

    intent: Intent
    execution: Execution
    reply: str
    tool_steps: list[ToolStep] = field(default_factory=list)
    needs_confirmation: bool = False
    needs_human: bool = False
    blocked: bool = False
    # 高风险写操作的确认令牌。仅在请求确认时填充；服务端只保存 SHA-256 摘要。
    confirmation_token: str | None = None
    # 幂等命中时复用此前成功结果，而非再次调用下游。
    replayed: bool = False
    # needs_human 只表示需要人工介入；只有真实工单系统返回 ID 后才为 True。
    human_handoff_created: bool = False
    # Explicit waiting state; never infer a pending task from punctuation in a reply.
    question: dict[str, Any] | None = None

    def add_step(self, name: str, arguments: dict, result: dict, blocked: bool = False) -> "ExecutionResult":
        """链式追加一个工具步骤（保持调用方代码简洁）。"""
        self.tool_steps.append(
            ToolStep(name=name, arguments=arguments, result=result, blocked=blocked)
        )
        return self
