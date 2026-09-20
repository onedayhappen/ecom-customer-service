from pathlib import Path
from datetime import datetime, timedelta

from app.memory.case_manager import CaseManager, CaseRecord


def test_exact_order_id_creates_separate_cases_and_records_turn(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))

    resolution = manager.resolve(
        "查询 ORD-001 物流，再给 ORD-002 申请退款",
        verify_order=lambda _order_id: {"success": True},
    )
    turn_id = manager.begin_turn("session-1", "查询 ORD-001 物流，再给 ORD-002 申请退款", resolution)
    cases = resolution.candidates

    assert turn_id.startswith("turn-")
    assert {case.order_ids[0] for case in cases} == {"ORD-001", "ORD-002"}
    assert {case.intent for case in cases} == {"logistics", "refund"}
    assert len([e for e in manager.events if e["event_type"] == "case_reference"]) == 2


def test_only_tool_results_update_business_state(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    resolution = manager.resolve("查询 ORD-001 的物流", verify_order=lambda _: {"success": True})
    turn_id = manager.begin_turn("session-1", "查询 ORD-001 的物流", resolution)
    case = resolution.selected

    manager.record_response("session-1", turn_id, "订单已经签收了")
    assert case.business_state["value"] == "unknown"

    manager.record_tool_result(
        "session-1", turn_id, "query_logistics",
        '{"success": true, "logistics": {"status": "in_transit"}, "order_id": "ORD-001"}',
    )
    assert case.business_state["source"] == "logistics_service"
    assert case.business_state["value"] == "in_transit"


def test_history_prefers_exact_order_over_semantic_intent(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    first = manager.resolve("ORD-001 要退款", verify_order=lambda _: {"success": True})
    manager.begin_turn("session-1", "ORD-001 要退款", first)
    second = manager.resolve("ORD-002 要退款", verify_order=lambda _: {"success": True})
    manager.begin_turn("session-2", "ORD-002 要退款", second)

    context = manager.relevant_context("ORD-002 的退款怎么样")

    assert len(context) == 1
    assert context[0].case_id != first.selected.case_id
    assert context[0].order_ids == ["ORD-002"]


def test_ambiguous_cases_require_user_confirmation(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    for order_id in ("ORD-001", "ORD-002"):
        resolution = manager.resolve(f"{order_id} 要退款", verify_order=lambda _: {"success": True})
        manager.begin_turn("session-1", f"{order_id} 要退款", resolution)

    resolution = manager.resolve("我的退款怎么样了")

    assert resolution.selected is None
    assert resolution.need_clarification is True
    assert len(resolution.candidates) == 2


def test_unverified_order_never_creates_case(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    resolution = manager.resolve(
        "查询 ORD-999", verify_order=lambda _: {"success": False, "error": "无权访问该订单"},
    )

    assert not resolution.candidates
    assert resolution.reason == "entity_verification_failed"
    assert manager.cases == {}


def test_inactive_open_case_is_auto_closed(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    old = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    manager.cases["case-1"] = CaseRecord(
        case_id="case-1",
        user_id="user-1",
        intent="refund",
        status="open",
        created_at=old,
        updated_at=old,
        last_active_at=old,
    )

    changed = manager.prune_cases(datetime.now())

    assert changed >= 1
    assert manager.cases["case-1"].status == "closed"


def test_archived_case_is_removed_from_hot_store(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    old = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    manager.cases["case-1"] = CaseRecord(
        case_id="case-1",
        user_id="user-1",
        intent="refund",
        status="closed",
        created_at=old,
        updated_at=old,
        last_active_at=old,
    )

    changed = manager.prune_cases(datetime.now())

    assert changed >= 1
    assert "case-1" not in manager.cases
    assert manager.archive_path.exists()


def test_order_selection_only_accepts_explicit_candidate(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    candidates = [
        {"order_id": "ORD-001", "items_summary": "运动鞋", "status": "shipped"},
        {"order_id": "ORD-002", "items_summary": "跑鞋", "status": "pending"},
    ]
    manager.request_order_selection("session-1", "取消我的鞋", "order_cancel", candidates)

    invalid = manager.consume_order_selection("session-1", "那个")
    selected = manager.consume_order_selection("session-1", "第 2 个")

    assert invalid and invalid["invalid"] is True
    assert selected == {"order_id": "ORD-002", "message": "取消我的鞋", "intent": "order_cancel"}
    assert "session-1" not in manager.pending_order_selections


def test_order_selection_accepts_ordinal_with_product_context(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    candidates = [
        {"order_id": "ORD-001", "items_summary": "吸尘器", "status": "shipped"},
        {"order_id": "ORD-002", "items_summary": "蓝牙耳机", "status": "shipped"},
    ]
    manager.request_order_selection("session-1", "查物流", "logistics_inquiry", candidates)

    selected = manager.consume_order_selection("session-1", "第二个，就是那个耳机")

    assert selected == {"order_id": "ORD-002", "message": "查物流", "intent": "logistics_inquiry"}


def test_recent_tool_evidence_keeps_results_without_order_id(tmp_path: Path):
    manager = CaseManager("user-1", str(tmp_path))
    resolution = manager.resolve("我买过哪些东西")
    turn_id = manager.begin_turn("session-1", "我买过哪些东西", resolution)
    raw = '{"success": true, "orders": [{"order_id": "ORD-001", "status": "已发货"}]}'

    manager.record_tool_result("session-1", turn_id, "list_user_orders", raw)

    assert manager.recent_tool_evidence("session-1") == [
        {"success": True, "orders": [{"order_id": "ORD-001", "status": "已发货"}]}
    ]
