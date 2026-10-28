from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.session.context_budget import estimate_request_tokens
from app.session.request_budget import ContextBudgetExceeded
from app.support.store import SupportError
from app.tasks.candidates import BusinessScope, CandidateService
from app.tasks.repository import TaskRepository
from tests.test_task_storage import empty_store, message


@pytest.fixture
def store(empty_store):
    for sid, user in (("s", "alice"), ("old", "alice"), ("old2", "alice"), ("other", "bob")):
        empty_store.ensure(sid, user)
    return empty_store


def task(store, sid="old", user="alice", goal="耳机退货", details=None, status="in_progress"):
    source = message(store, sid, user, goal)
    record = TaskRepository(store, user).create(
        sid, change_id=str(uuid4()), source=source, goal=goal,
        details=details or {"order_id": "ORD-A", "item_name": "耳机"}, status=status)
    return record, source


def ask(store, record, source, *, publish=True):
    repo = TaskRepository(store, record.user_id)
    question = repo.ask(record.task_id, source["session_id"], expected_version=record.version,
                        change_id="ask:" + record.task_id, source=source,
                        question={"field": "reason", "prompt": "请说明原因", "options": []})
    if publish:
        with store.transaction() as tx:
            c = store._control(tx, source["session_id"], record.user_id)
            store._event(tx, c, "message", "bot", {
                "role": "assistant", "content": "请说明原因", "task_id": question.task_id,
                "question_id": question.waiting_for["question_id"], "task_version": question.version,
            })
    return question


def build(store, text, **kwargs):
    source = message(store, "s", "alice", text)
    return CandidateService(store, "alice", **kwargs).build("s", [source["sequence"]])


def test_yesterday_retrieves_other_session_with_original_source(store):
    record, source = task(store)
    with store.transaction() as tx:
        tx.execute("UPDATE conversation_tasks SET updated_at=? WHERE task_id=?", ("2026-10-19T02:00:00+00:00", record.task_id))
    current = message(store, "s", "alice", "昨天那单继续处理")
    service = CandidateService(store, "alice")
    context = service.build("s", [current["sequence"]], at=datetime(2026, 10, 20, 10, tzinfo=timezone.utc))
    candidate = next(c for c in context.candidates if c["task_id"] == record.task_id)
    assert "history" in candidate["reasons"] and "task_updated_yesterday" in candidate["reasons"]
    assert candidate["sources"][0]["content"] == "耳机退货"
    assert service.source_message(source["session_id"], source["sequence"])["content"] == "耳机退货"
    assert TaskRepository(store, "alice").get(record.task_id).version == 1


def test_same_product_orders_across_sessions_remain_separate_candidates(store):
    first, _ = task(store, "s")
    second, _ = task(store, "old", details={"order_id": "ORD-B", "item_name": "耳机"})
    context = build(store, "耳机还有问题")
    assert {c["task_id"] for c in context.candidates} >= {first.task_id, second.task_id}
    assert "selected_task_id" not in context.data()


def test_two_questions_keep_ids_versions_and_publication_state(store):
    first, source1 = task(store, "s")
    second, source2 = task(store, "s", goal="鞋子退货", details={"order_id": "ORD-B"})
    first, second = ask(store, first, source1), ask(store, second, source2)
    context = build(store, "是的")
    questions = {c["task_id"]: c["question"] for c in context.candidates}
    assert questions[first.task_id]["valid"] and questions[second.task_id]["valid"]
    assert questions[first.task_id]["question_id"] != questions[second.task_id]["question_id"]
    assert questions[first.task_id]["task_version"] == first.version
    replay = TaskRepository(store, "alice").ask(
        first.task_id, "s", expected_version=1, change_id="ask:" + first.task_id, source=source1,
        question={"field": "reason", "prompt": "请说明原因", "options": []})
    assert replay == first
    TaskRepository(store, "alice").update(
        first.task_id, "s", expected_version=first.version, change_id="amended",
        source=source1, patch={"details": {"order_id": "ORD-C"}})
    context = build(store, "第二个")
    questions = {c["task_id"]: c["question"] for c in context.candidates}
    assert not questions[first.task_id]["valid"]
    assert questions[second.task_id]["valid"]


def test_unsent_question_cannot_consume_user_answer(store):
    record, source = task(store, "s")
    record = ask(store, record, source, publish=False)
    context = build(store, "是的")
    question = next(c["question"] for c in context.candidates if c["task_id"] == record.task_id)
    assert not question["valid"] and not question["published"]


def test_private_notes_and_other_user_never_enter_context(store):
    own, _ = task(store)
    forbidden, other_source = task(store, "other", "bob", "耳机退货，秘密订单", details={"order_id": "ORD-A"})
    with store.transaction() as tx:
        c = store._control(tx, "s", "alice")
        private_seq = store._event(tx, c, "message", "staff", {"role": "agent", "content": "PRIVATE-NOTE"}, private=True)
    context = build(store, "ORD-A 继续退货")
    serialized = json.dumps(context.data(), ensure_ascii=False)
    assert own.task_id in serialized and forbidden.task_id not in serialized and "PRIVATE-NOTE" not in serialized
    service = CandidateService(store, "alice")
    for sid, seq in (("s", private_seq), ("other", other_source["sequence"])):
        with pytest.raises(SupportError):
            service.source_message(sid, seq)


def test_business_scope_filters_tasks_and_source_backtracking(store, monkeypatch):
    monkeypatch.setattr(settings, "order_platform_codes", "pdd,taobao")
    allowed, _ = task(store, details={"order_id": "ORD-A", "platform_code": "pdd", "store_id": "shop-a"})
    denied, denied_source = task(store, "old2", goal="别店耳机", details={"order_id": "ORD-B", "platform_code": "pdd", "store_id": "shop-b"})
    context = build(store, "之前的耳机", scope=BusinessScope("pdd", "shop-a"))
    assert [c["task_id"] for c in context.candidates] == [allowed.task_id]
    assert denied.task_id not in json.dumps(context.data())
    service = CandidateService(store, "alice", scope=BusinessScope("pdd", "shop-a"))
    with pytest.raises(SupportError):
        service.source_message(denied_source["session_id"], denied_source["sequence"])
    with pytest.raises(SupportError):
        CandidateService(store, "alice", scope=BusinessScope("unapproved"))


def test_closed_task_is_only_a_clue_and_does_not_restore_authorization(store):
    closed, _ = task(store, status="completed")
    context = build(store, "ORD-A 上次的退款")
    candidate = next(c for c in context.candidates if c["task_id"] == closed.task_id)
    assert candidate["status"] == "completed"
    assert TaskRepository(store, "alice").get(closed.task_id) == closed


def test_old_explicit_order_is_found_outside_recent_scan(store):
    old, _ = task(store, details={"order_id": "ORD-OLD"})
    with store.transaction() as tx:
        tx.execute("UPDATE conversation_tasks SET updated_at='2020-01-01T00:00:00+00:00' WHERE task_id=?", (old.task_id,))
    for i in range(16):
        task(store, "old2", goal=f"鞋子事项{i}", details={"order_id": f"ORD-NOISE-{i}"})
    context = build(store, "不是 ORD-OLD 要取消，先查这单", max_candidates=3, scan_limit=4)
    assert any(c["task_id"] == old.task_id and c["required"] for c in context.candidates)
    assert context.current_messages[-1]["content"] == "不是 ORD-OLD 要取消，先查这单"


def test_explicit_product_id_keeps_both_orders_even_when_goals_differ(store):
    first, _ = task(store, goal="发票抬头修改", details={"order_id": "ORD-A", "sku_id": "SKU-42"})
    second, _ = task(store, "old2", goal="签收争议", details={"order_id": "ORD-B", "sku_id": "SKU-42"})
    context = build(store, "SKU-42 这一件")
    assert {c["task_id"] for c in context.candidates} == {first.task_id, second.task_id}
    assert all(c["required"] and "explicit_product" in c["reasons"] for c in context.candidates)


def test_retrieval_failure_is_explicit_and_current_message_survives(store, monkeypatch):
    source = message(store, "s", "alice", "昨天那单")
    service = CandidateService(store, "alice")
    def failing(*args, **kwargs):
        raise OSError("sensitive backend path must not enter context")
    monkeypatch.setattr(service, "_history", failing)
    context = service.build("s", [source["sequence"]])
    assert context.gaps == ["history_unavailable"]
    assert context.candidates == [] and context.current_messages[0]["content"] == "昨天那单"
    assert "sensitive" not in json.dumps(context.data())


def test_request_budget_preserves_conditions_and_counts_prompt_and_schema(store):
    explicit, _ = task(store, "s")
    for i in range(5):
        task(store, "s", goal=f"无关的历史事项{i}" + "背景" * 200, details={"order_id": f"ORD-X-{i}"})
    text = "不要取消 ORD-A；只有免运费才退货，否则先保留。"
    context = build(store, text)
    original = deepcopy(context.data())
    template = {"messages": [{"role": "system", "content": "只做归属判断"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "association", "schema": {"type": "object"}}}}
    required = deepcopy(context)
    required.candidates = [c for c in required.candidates if c["required"]]
    required.omitted_candidates += len(context.candidates) - len(required.candidates)
    required.omitted_recent_messages = len(required.recent_messages)
    required.recent_messages = []
    minimum = deepcopy(template)
    minimum["messages"] += [{"role": "user", "content": json.dumps(required.data(), ensure_ascii=False, separators=(",", ":"))}]
    limit = estimate_request_tokens(minimum) + 10
    params = context.request(template, input_limit=limit)
    assert estimate_request_tokens(params) <= limit
    result = json.loads(params["messages"][-1]["content"])
    assert result["current_messages"][-1]["content"] == text
    assert any(c["task_id"] == explicit.task_id for c in result["candidates"])
    assert result["omitted_candidates"] > 0
    assert context.data() == original
    with pytest.raises(ContextBudgetExceeded):
        context.request(template, input_limit=20)


def test_excess_required_candidates_is_visible(store):
    task(store, details={"order_id": "ORD-A"})
    task(store, "old2", details={"order_id": "ORD-B"})
    context = build(store, "不是 ORD-A，是 ORD-B", max_candidates=1)
    assert context.gaps == ["required_candidates_exceed_limit"]
    assert context.omitted_candidates == 1
    assert context.current_messages[0]["content"] == "不是 ORD-A，是 ORD-B"


def test_budget_never_silently_drops_recent_negation_or_condition(store):
    important = "只有免运费才退，不能收手续费；" + "说明" * 300
    message(store, "s", "alice", important)
    context = build(store, "继续")
    assert context.recent_messages[0]["required"]
    with pytest.raises(ContextBudgetExceeded):
        context.request(input_limit=100)
    data = json.loads(context.request(input_limit=4000)["messages"][-1]["content"])
    assert data["recent_messages"][0]["content"] == important


def test_read_only_lookup_uses_identity_and_restores_previous_context(store):
    from app.tools.request_context import get_current_user_id, reset_current_user_id, set_current_user_id
    seen = []
    def lookup(**kwargs):
        seen.append((get_current_user_id(), kwargs))
        return {"success": True, "data": {"items": [
            {"order_id": "ORD-A", "platform_code": "pdd", "purchase_summary": [{"name": "耳机"}]},
            {"order_id": "ORD-B", "platform_code": "pdd", "purchase_summary": [{"name": "耳机"}]},
        ]}}
    source = message(store, "s", "alice", "那件耳机")
    service = CandidateService(store, "alice", order_lookup=lookup)
    outer = set_current_user_id("bob")
    try:
        context = service.build("s", [source["sequence"]], check_orders=True)
        assert get_current_user_id() == "bob"
        assert seen == [("bob", {"user": "alice", "scope": BusinessScope()})]
        assert {o["order_id"] for o in context.orders} == {"ORD-A", "ORD-B"}
        assert context.candidates == []  # No invented task for a real order.
        service.order_lookup = lambda **kwargs: {"success": False, "error": "backend details"}
        context = service.build("s", [source["sequence"]], check_orders=True)
        assert "orders_unavailable" in context.gaps and context.orders == []
        assert get_current_user_id() == "bob"
    finally:
        reset_current_user_id(outer)
