"""Intent metadata and tool plans.

Classification labels describe requests. Current task execution replaces the
plan's tool set with a server-validated task whitelist; labels grant no authority.
"""

from app.data_plan.enums import (
    Domain,
    Execution,
    Fallback,
    Intent,
    Permission,
)
from app.data_plan.capabilities import (
    CAPABILITIES,
    IMPLEMENTED_CAPABILITIES,
    LOAD_SKILL,
    MUTATING_CAPABILITIES,
    ToolCapability,
    get_capability,
    is_implemented,
)
from app.data_plan.intentspec import (
    IntentSpec,
    MemorySpec,
    SPECS,
    get_intentspec,
    validate_specs,
)
from app.data_plan.builder import DataPlan, build_data_plan

__all__ = [
    # 枚举
    "Intent",
    "Domain",
    "Execution",
    "Permission",
    "Fallback",
    # 能力目录
    "ToolCapability",
    "CAPABILITIES",
    "IMPLEMENTED_CAPABILITIES",
    "MUTATING_CAPABILITIES",
    "LOAD_SKILL",
    "get_capability",
    "is_implemented",
    # IntentSpec
    "IntentSpec",
    "MemorySpec",
    "SPECS",
    "get_intentspec",
    "validate_specs",
    # DataPlan
    "DataPlan",
    "build_data_plan",
]
