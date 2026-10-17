"""IntentSpec / DataPlan 装配层的单元测试。

覆盖：17 类标签全覆盖与契约一致性、执行路径划分、retrievable 派生、
权限等级、工具裁剪（已实现/待实现）、blocked 判定、pending_slots 初始化。
"""

from __future__ import annotations

import pytest

from app.data_plan import (
    DataPlan,
    Execution,
    Fallback,
    Intent,
    Permission,
    build_data_plan,
    get_intentspec,
    validate_specs,
)
from app.data_plan.capabilities import (
    CAPABILITIES,
    IMPLEMENTED_CAPABILITIES,
    LOAD_SKILL,
    MUTATING_CAPABILITIES,
)

from app.data_plan.taxonomy import CLASSIFICATION_INTENTS, LEGACY_INTENTS


# ---------------------------------------------------------------------------
# 配置完备性
# ---------------------------------------------------------------------------

def test_canonical_and_legacy_intents_fully_covered():
    assert set(Intent) == CLASSIFICATION_INTENTS | LEGACY_INTENTS
    assert len(CLASSIFICATION_INTENTS) == 31
    assert len(LEGACY_INTENTS) == 5
    assert validate_specs() == []


def test_every_spec_references_known_capability():
    from app.data_plan.intentspec import SPECS
    for spec in SPECS.values():
        for tool in spec.tools:
            assert tool in CAPABILITIES, f"{spec.intent.value} 引用未知工具 {tool}"


# ---------------------------------------------------------------------------
# 执行路径划分（11 workflow（含对话流）/ 6 react；Rule 已并入 Workflow）
# ---------------------------------------------------------------------------

def test_execution_path_distribution():
    from app.data_plan.intentspec import SPECS
    from collections import Counter
    dist = Counter(SPECS[intent].execution for intent in CLASSIFICATION_INTENTS)
    assert dist[Execution.WORKFLOW] == 19
    assert dist[Execution.REACT] == 12


def test_dialog_flow_labels_are_workflow_driven():
    """对话流并入 Workflow：无独立 Rule 路径。"""
    for intent in (Intent.CONFIRMATION, Intent.ANSWER_QUESTION,
                   Intent.CLOSURE, Intent.GREETINGS):
        assert get_intentspec(intent).execution == Execution.WORKFLOW


def test_react_labels():
    for intent in (Intent.PRODUCT_INQUIRY, Intent.PRODUCT_RECOMMENDATION,
                   Intent.AFTER_SALE_REQUEST, Intent.ISSUE_REPORT,
                   Intent.COMPLAINT, Intent.OTHERS):
        assert get_intentspec(intent).execution == Execution.REACT


# ---------------------------------------------------------------------------
# retrievable 派生（12 True / 5 False）
# ---------------------------------------------------------------------------

def test_retrievable_distribution():
    from app.data_plan.intentspec import SPECS
    true_count = sum(SPECS[i].retrievable for i in CLASSIFICATION_INTENTS)
    assert true_count == 28  # 28 业务类
    assert len(CLASSIFICATION_INTENTS) - true_count == 3  # 2 对话流 + 1 兜底


def test_dialog_flow_is_not_retrievable():
    for intent in (Intent.CONFIRMATION, Intent.ANSWER_QUESTION,
                   Intent.CLOSURE, Intent.GREETINGS, Intent.OTHERS):
        assert get_intentspec(intent).retrievable is False


# ---------------------------------------------------------------------------
# 权限等级
# ---------------------------------------------------------------------------

def test_mutating_intents_require_confirmation():
    for intent in (Intent.ADDRESS_CHANGE, Intent.ORDER_CANCEL, Intent.EXPEDITE,
                   Intent.AFTER_SALE_REQUEST, Intent.INVOICE):
        assert get_intentspec(intent).permission == Permission.CONFIRM_REQUIRED


def test_complaint_is_escalate():
    assert get_intentspec(Intent.COMPLAINT).permission == Permission.ESCALATE


def test_mutating_tools_never_on_read_only_spec():
    from app.data_plan.intentspec import SPECS
    for spec in SPECS.values():
        if spec.permission != Permission.CONFIRM_REQUIRED:
            assert not (set(spec.tools) & MUTATING_CAPABILITIES), spec.intent.value


# ---------------------------------------------------------------------------
# 槽位
# ---------------------------------------------------------------------------

def test_required_slots():
    assert get_intentspec(Intent.ADDRESS_CHANGE).required_slots == ("order_id", "new_address")
    assert get_intentspec(Intent.INVOICE).required_slots == ("order_id", "invoice_type", "title")
    assert get_intentspec(Intent.AFTER_SALE_REQUEST).required_slots == ("order_id", "reason")
    # 对话流无槽位（填槽规则由 case.pending_slots 驱动，非静态声明）
    assert get_intentspec(Intent.ANSWER_QUESTION).required_slots == ()


# ---------------------------------------------------------------------------
# DataPlan 装配
# ---------------------------------------------------------------------------

def test_build_data_plan_accepts_string_intent():
    plan = build_data_plan("refund_status")
    assert plan.intent == Intent.REFUND_STATUS


def test_build_data_plan_prunes_unimplemented_tools():
    plan = build_data_plan(Intent.AFTER_SALE_REQUEST)
    # 已实现：query_order / list_user_orders / search_knowledge / apply_refund
    assert set(plan.tools) == {"query_order", "list_user_orders", "search_knowledge", "apply_refund"}
    # 未实现：apply_exchange / apply_price_protect
    assert set(plan.pending_tools) == {"apply_exchange", "apply_price_protect"}


def test_blocked_when_mutating_tool_missing():
    # 缺换货/价保写工具 → 任务无法完整落库 → blocked
    assert build_data_plan(Intent.AFTER_SALE_REQUEST).blocked is True
    # refund_status 只缺读工具 query_refund（有 query_order 兜底）→ 不 blocked
    assert build_data_plan(Intent.REFUND_STATUS).blocked is False


def test_prompt_tools_include_load_skill_when_skill_assembled():
    plan = build_data_plan(Intent.AFTER_SALE_REQUEST)
    assert LOAD_SKILL in plan.prompt_tool_names  # 装配了 process-return

    plan_no_skill = build_data_plan(Intent.ORDER_INQUIRY)
    assert LOAD_SKILL not in plan_no_skill.prompt_tool_names


def test_init_pending_slots_deducts_resolved_entities():
    plan = build_data_plan(Intent.ADDRESS_CHANGE)
    assert plan.init_pending_slots() == ["order_id", "new_address"]
    # order_id 已解析 → 只剩 new_address
    assert plan.init_pending_slots({"order_id": "ORD-001"}) == ["new_address"]


def test_dialog_flow_plan_has_no_business_tools():
    plan = build_data_plan(Intent.CONFIRMATION)
    assert plan.tools == ()
    assert plan.retrievable is False


def test_custom_available_tools_override():
    # 若执行环境额外实现了 query_refund，则裁剪结果随之变化
    plan = build_data_plan(
        Intent.REFUND_STATUS,
        available_tools=IMPLEMENTED_CAPABILITIES | {"query_refund", LOAD_SKILL},
    )
    assert "query_refund" in plan.tools
    assert plan.pending_tools == ()


def test_unknown_intent_raises():
    with pytest.raises((KeyError, ValueError)):
        get_intentspec("nonexistent_intent")
