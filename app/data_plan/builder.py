"""DataPlan 装配器：IntentSpec → 运行时装配实例。

IntentSpec 是静态定义（intent → 需求）；DataPlan 是结合当前可用工具集
（本地 registry / MCP）实例化后的**具体装配**，供执行层（Workflow/ReAct/规则）
直接消费。装配时做工具裁剪：已实现的工具进 prompt，未实现的进入
``pending_tools`` 供执行层显式降级（澄清 / 转人工）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Collection, Mapping

from app.data_plan.capabilities import (
    IMPLEMENTED_CAPABILITIES,
    LOAD_SKILL,
    MUTATING_CAPABILITIES,
)
from app.data_plan.enums import Execution, Fallback, Intent, Permission
from app.data_plan.intentspec import IntentSpec, MemorySpec, get_intentspec


@dataclass(frozen=True)
class DataPlan:
    """一次意图识别的运行时装配结果（不可变）。"""

    intent: Intent
    spec: IntentSpec
    execution: Execution
    description: str
    tools: tuple[str, ...]          # 已实现的业务能力（进 prompt）
    pending_tools: tuple[str, ...]  # 未实现的业务能力（执行层降级依据）
    skills: tuple[str, ...]         # 装配的 skill 名
    memory: MemorySpec              # 需装配的数据源
    permission: Permission
    required_slots: tuple[str, ...]
    fallback: Fallback
    retrievable: bool               # 派生自 spec（是否需检索数据源）
    conversational: bool = False    # 标签只作提示，不限制对话或授予操作权限

    @property
    def blocked(self) -> bool:
        """是否存在未实现的写操作工具。

        写操作缺失意味着任务无法落库（改址/取消/退款等），必须降级；
        只读工具缺失通常还有兜底（如缺 list_user_orders 可改为索要订单号）。
        """
        return not self.spec.execution_ready or any(t in MUTATING_CAPABILITIES for t in self.pending_tools)

    @property
    def prompt_tool_names(self) -> frozenset[str]:
        """实际装配进 prompt 的工具名（业务能力 + 需要时附 load_skill）。

        当装配了 skill 时附带 ``load_skill``，供执行层加载剧本。
        """
        names = set(self.tools)
        if self.skills:
            names.add(LOAD_SKILL)
        return frozenset(names)

    def init_pending_slots(
        self, resolved_entities: Mapping[str, str] | None = None
    ) -> list[str]:
        """由 required_slots 初始化 case.pending_slots。

        已解析实体（如已在 case.resolved_entities 中的 order_id）不计入待填槽位。
        """
        resolved = set(resolved_entities or {})
        return [slot for slot in self.required_slots if slot not in resolved]


def build_data_plan(
    intent: Intent | str,
    available_tools: Collection[str] | None = None,
) -> DataPlan:
    """由 intent 实例化 DataPlan。

    :param intent: Intent 枚举或分类器输出的字符串值。
    :param available_tools: 当前可用的工具名集合。缺省时用「已实现能力 ∪ load_skill」，
        即本地 registry 的能力全集。
    """
    spec = get_intentspec(intent)

    if available_tools is None:
        available_tools = IMPLEMENTED_CAPABILITIES | {LOAD_SKILL}
    available = frozenset(available_tools)

    tools = tuple(t for t in spec.tools if t in available)
    pending = tuple(t for t in spec.tools if t not in available)

    return DataPlan(
        intent=spec.intent,
        spec=spec,
        execution=spec.execution,
        description=spec.description,
        tools=tools,
        pending_tools=pending,
        skills=spec.skills,
        memory=spec.memory,
        permission=spec.permission,
        required_slots=spec.required_slots,
        fallback=spec.fallback,
        retrievable=spec.retrievable,
    )


def build_conversation_plan(intent: Intent | str) -> DataPlan:
    """一个对话 Agent 接待全部业务；未接入的动作不阻断咨询和查询。

    工具白名单来自服务端能力注册，不从分类标签推导。required_slots 仅在
    具体工具调用时校验，避免问政策也被强制索要订单号。
    """
    plan = build_data_plan(intent)
    tools = tuple(sorted(IMPLEMENTED_CAPABILITIES))
    memory = MemorySpec(session=True, case=True, user_profile=True,
                        order_db=True, product_db=True, policy_kb=True)
    spec = replace(plan.spec, execution=Execution.REACT, execution_ready=True,
                   tools=tools, skills=(), required_slots=(), memory=memory,
                   permission=Permission.CONFIRM_REQUIRED)
    return replace(plan, spec=spec, execution=Execution.REACT, tools=tools,
                   pending_tools=(), skills=(), required_slots=(), memory=memory,
                   permission=Permission.CONFIRM_REQUIRED, retrievable=True,
                   conversational=True)
