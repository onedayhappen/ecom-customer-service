"""Actual recorded failures, publication fences, and typed evidence boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.reply_composition import compose, validate_reply
from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.replies import ReplyCoordinator
from app.tasks.reply_grounding import choices, project, response
from app.services.commerce.common import ref
from tests.test_task_candidates import store, task
from tests.test_task_execution import request, response as model_response
from tests.test_task_storage import empty_store


CASES = Path(__file__).parent / "fixtures" / "reply_grounding"


@pytest.fixture(params=["shirt_no_shipment", "shoes_unidentified_box"])
def recorded(request):
    return json.loads((CASES / (request.param + ".json")).read_text(encoding="utf-8"))


def context_for(receipts, *, task_id="task-1", details=None):
    blocks = project(SimpleNamespace(details=details or {}), receipts)
    return dict(current_messages=[], recent_messages=[], matters=[dict(task_id=task_id,
        grounding=blocks, verified_facts="", fallback="暂时无法核实")], required_notices=[])


def receipt(tool, data, *, target=None, **result):
    return dict(event_id="e-1", tool=tool, arguments={"target_ref": target or ref("order", "mall", "O1")},
                result=dict(success=True, data=data, data_as_of=None, completeness="partial", **result))


def save_results(store, record, source, receipts):
    runs = TaskRuns(store, record.user_id)
    req = TaskExecutionInput.model_validate(request(record, source))
    reservation, _ = runs.begin(req)
    ids = []
    for index, r in enumerate(receipts):
        saved = runs.save_tool(req, reservation["payload"], index, r["tool"], r["arguments"], r["result"])
        ids.append(saved["event_id"])
    runs.finish(req, reservation["payload"], dict(state="ready", task_version=record.version,
                reply="不可信的执行模型文本", evidence_ids=ids))


def test_real_bad_reply_was_format_valid_but_is_now_rejected(recorded):
    context = context_for(recorded["receipts"])
    # The old validator checked only shape. Replay the exact recorded wording.
    assert validate_reply(recorded["original_reply"]) == recorded["original_reply"]
    with pytest.raises(ValueError, match="ungrounded_composed_reply"):
        validate_reply(recorded["original_reply"], context=context)
    answer = response(context)
    assert validate_reply(answer, context=context) == answer
    assert "上游数据同步时间未记录" in answer
    if recorded["source"]["reply_sequence"] == 20:
        assert "暂未查到包裹记录，不能据此确认实际尚未发货" in answer
        assert "可以判断它还没有发货" not in answer
    else:
        assert "不能判断家人收到的包裹是否就是这一件" in answer
        assert "2026-10-24T15:02:08Z" in answer
        assert "应该不是它" not in answer


def test_real_bad_reply_blocked_at_actual_publication_boundary(store, recorded):
    record, source = task(store, "s", goal=recorded["user_message"], details={"item_name": "本次商品"})
    save_results(store, record, source, recorded["receipts"])
    calls = []
    def wrong_model(context):
        calls.append(context)
        # Try to corrupt the proof supplied to the composer as well.
        context["matters"][0]["grounding"] = []
        return recorded["original_reply"]
    result = ReplyCoordinator(store, "alice", composer=wrong_model).publish("s")
    assert len(calls) == 1  # No model retry for a grounding failure.
    assert result["content"] != recorded["original_reply"]
    assert "上游数据同步时间未记录" in result["content"]
    assert ReplyCoordinator(store, "alice", composer=wrong_model).publish("s") is None
    with store.transaction(read_only=True) as tx:
        traces = tx.all("SELECT payload FROM interaction_trace_events WHERE stage='reply.composition.fallback'")
    assert any("ungrounded_composed_reply" in t["payload"] for t in traces)


def test_existing_single_call_selects_relevant_receipt_and_preserves_whole_claim(recorded):
    context = context_for(recorded["receipts"])
    context["current_messages"] = [{"role": "user", "content": recorded["user_message"]}]
    context["recent_messages"] = [{"role": "assistant", "content": recorded["original_reply"]}]
    candidates = choices(context)
    # In the shoe case there is also an old multi-item order with delivered
    # parcels. Selecting the named summer shoe must not borrow those statuses.
    if recorded["source"]["reply_sequence"] == 20:
        chosen = [b["id"] for b in candidates if "HLA海澜之家" in b["text"] and ("88.00" in b["text"] or "暂未查到包裹记录" in b["text"])]
    else:
        chosen = [b["id"] for b in candidates if "ROSHE RUN" in b["text"] and "最新轨迹时间" in b["text"]]
    requests = []
    def create(**params):
        requests.append(params)
        return model_response(json.dumps({"blocks": chosen}))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    answer = compose(client, "test", context)
    assert len(requests) == 1 and "tools" not in requests[0]
    sent = json.loads(requests[0]["messages"][1]["content"])
    assert "grounding" not in sent["matters"][0]  # No duplicated projection.
    assert sent["matters"][0]["verified_facts"] == ""
    assert "DEMO-WILLIAM-MULTI-2" not in answer
    assert "系统记录" in answer or "暂未查到" in answer
    assert validate_reply(answer, context=context) == answer


@pytest.mark.parametrize("bad", [
    "可以确认没有发货。", "还未出库，肯定没寄。", "系统未查到包裹，所以尚未发货。",
    "家人收的应该是别的鞋盒。", "鞋盒不是这双。", "鞋子今天就能到。",
    '{"blocks":["missing"]}', '{"blocks":["fact-0","fact-0"]}', '{"blocks":[]}',
    '{"blocks":["fact-0"],"text":"所以肯定没有发货"}',
])
def test_unknown_rewrites_negations_and_invented_references_fail_closed(bad):
    context = context_for([receipt("get_logistics", {"items": [], "total": 0})])
    with pytest.raises(ValueError, match="ungrounded_composed_reply"):
        validate_reply(bad, context=context)


def test_refund_success_is_per_currency_partial_and_not_bank_credit():
    rows = [dict(status="succeeded", amount="20", currency="CNY"),
            dict(status="processing", amount="80", currency="CNY"),
            dict(status="succeeded", amount="5", currency="USD")]
    context = context_for([receipt("get_refunds", {"items": rows}, next_cursor="next")])
    answer = response(context)
    assert "20.00 CNY" in answer and "5.00 USD" in answer and "100.00" not in answer
    assert "银行卡入账" in answer and "不能据此判断已全额退款" in answer
    assert "仅返回部分记录" in answer
    for bad in ("退款100元已全额到账。", "已退回银行卡20元。", answer.replace("20.00", "100.00")):
        with pytest.raises(ValueError, match="ungrounded_composed_reply"):
            validate_reply(bad, context=context)


def test_approval_is_not_refund_success():
    context = context_for([receipt("get_after_sale_status", {"after_sale": {"status": "approved"}})])
    assert "已批准" in response(context) and "不能证明退款已到账" in response(context)


def test_recorded_delivery_remains_supported_and_old_date_cannot_become_today():
    parcel = dict(order_id="O1", platform_code="mall", shipment_id="S1", status="delivered",
                  delivered_at="2026-10-23T01:00:00Z", events=[])
    context = context_for([receipt("get_logistics", {"items": [parcel]})])
    answer = response(context)
    assert "已签收" in answer and "2026-10-23T01:00:00Z" in answer
    assert validate_reply(answer, context=context) == answer
    with pytest.raises(ValueError, match="ungrounded_composed_reply"):
        validate_reply(answer.replace("2026-10-23T01:00:00Z", "今天"), context=context)


def test_two_tasks_cannot_exchange_status_or_omit_a_task():
    first = context_for([receipt("get_logistics", {"items": []})], task_id="shirt")
    second = context_for([receipt("get_refunds", {"items": []}, target=ref("order", "mall", "O2"))], task_id="shoes")
    context = dict(first, matters=first["matters"] + second["matters"])
    with pytest.raises(ValueError, match="ungrounded_composed_reply"):
        validate_reply('{"blocks":["fact-0"]}', context=context)
    with pytest.raises(ValueError, match="ungrounded_composed_reply"):
        validate_reply(response(context).replace("订单 O1", "订单 O2"), context=context)


def test_selection_cannot_borrow_a_different_orders_receipt():
    # Server-side task binding wins over a tool argument for another order.
    context = context_for([receipt("get_logistics", {"items": []})], details={"order_id": "O2"})
    assert "记录归属尚未确认" in response(context)
    with pytest.raises(ValueError, match="ungrounded_composed_reply"):
        validate_reply("订单 O2 还没有发货。", context=context)


@pytest.mark.parametrize("data", [{}, {"items": None}, {"items": ["bad"]},
    {"items": [{"order_id": "O2", "platform_code": "mall", "status": "delivered"}]}])
def test_missing_or_mismatched_payload_is_not_a_negative_observation(data):
    context = context_for([receipt("get_logistics", data)])
    assert "暂未查到包裹记录" not in response(context)
    assert "记录归属尚未确认" in response(context)


def test_latest_event_compares_timestamps_including_timezone():
    parcel = dict(order_id="O1", platform_code="mall", shipment_id="S1", status="in_transit",
        events=[dict(event_time="2026-10-24T20:00:00+08:00"), dict(event_time="2026-10-24T15:00:00Z")])
    context = context_for([receipt("get_logistics", {"items": [parcel]})])
    assert "最新轨迹时间为 2026-10-24T15:00:00Z" in response(context)


def test_same_record_used_by_two_tasks_still_has_a_valid_rendered_reply():
    r = receipt("get_logistics", {"items": []})
    a, b = context_for([r], task_id="a"), context_for([r], task_id="b")
    context = dict(a, matters=a["matters"] + b["matters"])
    rendered = validate_reply('{"blocks":["fact-0","fact-1"]}', context=context)
    assert validate_reply(rendered, context=context) == rendered


def test_noncritical_product_replies_still_use_normal_composition():
    context = dict(current_messages=[], recent_messages=[], matters=[dict(task_id="p",
        grounding=[], verified_facts="商品颜色：蓝色")], required_notices=[])
    assert validate_reply("这款是蓝色的。", context=context) == "这款是蓝色的。"


@pytest.mark.parametrize("rows", [[], [dict(order_id="O1", platform_code="mall", status="paid")]])
def test_order_search_alone_cannot_infer_unshipped_or_nonexistent_order(rows):
    context = context_for([receipt("find_orders", {"items": rows})])
    assert response(context)
    with pytest.raises(ValueError, match="ungrounded_composed_reply"):
        validate_reply("订单不存在，肯定没有发货。", context=context)


def test_empty_keyword_attempt_does_not_compete_with_successful_lookup():
    missing = receipt("find_orders", {"items": [], "applied_conditions": {"query": "红米 手机"}})
    context = context_for([missing])
    assert "红米 手机" in response(context)
    found = receipt("get_logistics", {"items": []})
    context = context_for([missing, found])
    assert "红米 手机" not in response(context)
    assert "暂未查到包裹记录" in response(context)


@pytest.mark.parametrize("invalid", ["stale", "failed", "wrong_user", "revoked_control"])
def test_unusable_receipts_do_not_reach_composer(store, invalid):
    record, source = task(store, "s", details={"order_id": "O1"})
    save_results(store, record, source, [receipt("get_logistics", {"items": []})])
    with store.transaction() as tx:
        row = tx.one("SELECT sequence,payload FROM support_events WHERE kind='task_tool_result'")
        payload = json.loads(row["payload"])
        if invalid == "stale":
            payload["observed_epoch"] = 0
        elif invalid == "failed":
            payload["result"]["success"] = False
        elif invalid == "wrong_user":
            # Move the receipt to another user's event stream; its ID alone is
            # never authority to include it in the current reply.
            tx.execute("UPDATE support_events SET session_id='other' WHERE kind='task_tool_result'")
        else:
            payload["controls"] = {"s": 999}
        tx.execute("UPDATE support_events SET payload=? WHERE kind='task_tool_result'", (json.dumps(payload),))
    def forbidden(_):
        pytest.fail("no verified evidence should reach the composer")
    result = ReplyCoordinator(store, "alice", composer=forbidden).publish("s")
    assert "暂时没有可核实" in result["content"]


def test_grounding_adds_no_database_reads_to_publication_snapshot(store, recorded, monkeypatch):
    from app.support.store import Tx
    import app.tasks.reply_grounding as grounding
    record, source = task(store, "s", details={"item_name": "本次商品"})
    save_results(store, record, source, recorded["receipts"])
    coordinator = ReplyCoordinator(store, "alice", composer=lambda _: "unused")
    executed = []
    execute = Tx.execute
    def capture(self, sql, args=()):
        executed.append(sql)
        return execute(self, sql, args)
    monkeypatch.setattr(Tx, "execute", capture)
    coordinator._prepare("s", {})
    guarded = list(executed)
    executed.clear()
    monkeypatch.setattr(grounding, "project", lambda *args: [])
    coordinator._prepare("s", {})
    assert guarded == executed
