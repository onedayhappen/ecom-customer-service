# 文件说明：记录工具实现状态与业务副作用，供能力查询和执行约束使用。
"""工具能力目录：描述实现状态与业务副作用。

实际可调用工具由事项 Harness 的白名单决定；能力名称和标签不授予权限。
未接入的能力只用于明确边界，不能作为可执行工具提供给模型。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data_plan.enums import CapabilityCategory


@dataclass(frozen=True)
class ToolCapability:
    """一个工具能力的静态描述。"""

    name: str                     # 稳定的能力 ID
    category: CapabilityCategory  # 所属类别
    side_effect: bool             # True = 写操作（执行前须确认）
    implemented: bool             # True = 已在 registry 实现
    description: str              # 语义说明


from app.tools.catalog import TOOLS
CAPABILITIES = {t.name: ToolCapability(t.name, CapabilityCategory.BUSINESS,
    t.effect != "read", True, t.description) for t in TOOLS}
IMPLEMENTED_CAPABILITIES = frozenset(CAPABILITIES)
# Direct transaction commits are internal; prepared operations are callable but never commit.
MUTATING_CAPABILITIES = frozenset(t.name for t in TOOLS if t.effect != "read")


def is_implemented(name: str) -> bool:
    capability = CAPABILITIES.get(name)
    return bool(capability and capability.implemented)


def get_capability(name: str) -> ToolCapability:
    return CAPABILITIES[name]
