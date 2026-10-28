"""Semantic publication regressions, including two unmodified evaluation cases."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.replies import ReplyCoordinator
from app.tasks.reply_claims import build_claims, render_plan
from tests.test_coordinated_replies import replies
from tests.test_task_candidates import store, task
from tests.test_task_execution import request
from tests.test_task_storage import empty_store


CASES = json.loads((Path(__file__).parent / "fixtures/reply_evidence_cases.json").read_text(encoding="utf-8"))["cases"]


def install_case(store, case):
    matter, source = task(store, "s", goal=case["user"], details={"item_name": "短袖" if case["session"] == 1 else "耐克鞋"})
    runs = TaskRuns(store, "alice")
    req = TaskExecutionInput.model_validate(request(matter, source))
    reservation, _ = runs.begin(req)
    receipts = [runs.save_tool(req, reservation["payload"], n, r["tool"], r["arguments"], r["result"])
                for n, r in enumerate(case["receipts"])]
    runs.finish(req, reservation["payload"], dict(state=case["state"], task_version=matter.version,
                reply="执行草稿：没有发货，鞋盒肯定不是它；不允许作为证据。",
                evidence_ids=[r["event_id"] for r in receipts]))
    return matter, receipts


def case_claim(context, case):
    claims = context["matters"][0]["claims"]
    return next(c for c in claims if (c["kind"] == "dispatch_unknown" if case["session"] == 1 else
                c["kind"] == "parcel_record" and "ship_DEMO-RECENT-1-shoes" in c["text"]))


def plan(*claims):
    return json.dumps({"claim_ids": [c["claim_id"] for c in claims]})


def assert_safe_case(text, case):
    assert "数据同步时间未记录" in text
    assert "所以可以判断它还没有发货" not in text
    assert "应该不是它" not in text
    assert "执行草稿" not in text
    if case["session"] == 1:
        assert "暂未查到包裹或发货记录" in text
        assert "实际是否已经交给快递仍需核实" in text
        assert "DEMO-RECENT-1-paid" in text
    else:
        assert "物流记录显示运输中" in text
        assert "无法判断家人拿到的包裹是否就是这一件" in text
        assert "无法确认实际是否已送到小区" in text
        assert "2026-10-24T15:02:08Z" in text
        assert "今天" not in text


@pytest.mark.parametrize("case", CASES, ids=["blue-shirt-empty-records", "shoes-physical-identity"])
@pytest.mark.parametrize("mode", ["selection", "historical_bad_reply", "injected_text", "no_model"])
def test_real_cases_through_publication(store, case, mode):
    install_case(store, case)
    calls = []

    def model(context):
        calls.append(context)
        claim = case_claim(context, case)
        assert claim["quality"]["completeness"] == "partial"
        assert claim["quality"]["data_as_of"] is None
        if mode == "historical_bad_reply":
            return case["historical_reply"]
        if mode == "injected_text":
            return json.dumps({"claim_ids": [claim["claim_id"]], "text": case["historical_reply"]})
        return plan(claim)

    result = ReplyCoordinator(store, "alice", composer=None if mode == "no_model" else model).publish("s")
    assert result is not None
    assert_safe_case(result["content"], case)
    assert len(calls) == (0 if mode == "no_model" else 1)  # Never retry/judge with another model call.
    assert len(replies(store)) == 1
    assert ReplyCoordinator(store, "alice", composer=model).publish("s") is None


def catalogue(tool="get_logistics", data=None, *, scope="order-1", evidence="e1", **quality):
    record = SimpleNamespace(task_id="t1", status="in_progress", goal="查进度", details={"order_id": "O1"})
    receipt = dict(event_id=evidence, tool=tool, arguments={"target_ref": scope}, result=dict(
        success=True, data=data if data is not None else {"items": [], "total": 0},
        completeness="partial", data_as_of=None, observed_at="2026-10-27T01:00:00Z", limitations=[]))
    receipt["result"].update(quality)
    claims = build_claims(record, [receipt])
    return dict(matters=[dict(task_id="t1", evidence_ids=[evidence], claims=claims)])


@pytest.mark.parametrize("quality", [dict(), dict(completeness="complete", data_as_of="2026-10-26T01:00:00Z")])
def test_empty_complete_or_partial_results_never_prove_non_dispatch(quality):
    context = catalogue(**quality)
    claim = context["matters"][0]["claims"][0]
    text = render_plan(plan(claim), context)
    assert "实际是否已经交给快递仍需核实" in text
    assert "还没有发货" not in text
    if quality:
        assert "数据截至 2026-10-26T01:00:00Z" in text
        assert "2026-10-27T01:00:00Z" not in text  # Fetch time isn't upstream freshness.


@pytest.mark.parametrize("bad", ["", "包裹肯定不是它", '{"claim_ids":["invented"]}',
    '{"claim_ids":[]}', '{"claim_ids":[null]}', '["e1"]', '{"claim_ids":[{}]}'])
def test_unsupported_output_is_rejected(bad):
    with pytest.raises(ValueError):
        render_plan(bad, catalogue())


def test_other_parcel_can_only_be_reported_with_its_own_subject():
    case = CASES[1]
    record = SimpleNamespace(task_id="t1", status="in_progress", goal=case["user"], details={})
    receipts = [dict(r, event_id=f"e{n}") for n, r in enumerate(case["receipts"])]
    claims = build_claims(record, receipts)
    target = next(c for c in claims if c["kind"] == "parcel_record" and "ship_DEMO-RECENT-1-shoes" in c["text"])
    other = next(c for c in claims if c["kind"] == "parcel_record" and "ex_WM2-I4" in c["text"])
    context = dict(matters=[dict(task_id="t1", evidence_ids=[r["event_id"] for r in receipts], claims=claims)])
    assert target["subject_ref"] != other["subject_ref"]
    assert target["scope_ref"] != other["scope_ref"]
    assert "物流记录显示运输中" in render_plan(plan(target), context)
    text = render_plan(plan(other), context)
    assert "DEMO-WILLIAM-MULTI-2" in text and "换货补发包裹" in text
    assert "39、红色、秋季" in text and "DEMO-RECENT-1-shoes" not in text
    with pytest.raises(ValueError):
        render_plan(json.dumps({"claim_ids": [other["claim_id"]], "subject_ref": target["subject_ref"]}), context)


@pytest.mark.parametrize("change", ["subject", "quality", "receipt"])
def test_old_claim_cannot_be_reused_after_evidence_changes(change):
    original = catalogue()
    old = original["matters"][0]["claims"][0]
    if change == "quality":
        changed = catalogue(data_as_of="2026-10-26T12:00:00Z")
    elif change == "subject":
        changed = catalogue(scope="order-2")
    else:
        changed = catalogue(evidence="e2")
    with pytest.raises(ValueError):
        render_plan(plan(old), changed)


@pytest.mark.parametrize("field,value", [("completeness", "complete"), ("data_as_of", "2026-10-26T00:00:00Z")])
def test_evidence_quality_change_during_model_call_discards_publication(store, field, value):
    case = CASES[0]
    install_case(store, case)

    def model(context):
        claim = case_claim(context, case)
        with store.transaction() as tx:
            row = tx.one("SELECT sequence,payload FROM support_events WHERE event_key=?",
                         ("task-tool:" + claim["evidence_id"],))
            payload = json.loads(row["payload"])
            payload["result"][field] = value
            tx.execute("UPDATE support_events SET payload=? WHERE session_id='s' AND sequence=?",
                       (json.dumps(payload), row["sequence"]))
        return plan(claim)

    assert ReplyCoordinator(store, "alice", composer=model).publish("s") is None
    assert not replies(store)


def test_refund_success_is_not_bank_arrival():
    context = catalogue("get_refunds", {"recorded_success_amount": "88.00", "currency": "CNY"})
    text = render_plan(plan(context["matters"][0]["claims"][0]), context)
    assert "渠道已报退款成功金额：88.00" in text
    assert "不等于已核实银行卡到账" in text


def test_plan_cannot_omit_another_ready_matter_or_duplicate_claims():
    context = catalogue()
    first = context["matters"][0]["claims"][0]
    with pytest.raises(ValueError):
        render_plan(plan(first, first), context)
    second = deepcopy(context["matters"][0])
    second["task_id"] = "t2"
    second["claims"][0].update(task_id="t2", claim_id="c2")
    context["matters"].append(second)
    with pytest.raises(ValueError):
        render_plan(plan(first), context)


def test_model_cannot_mutate_the_trusted_catalogue(store):
    install_case(store, CASES[0])

    def model(context):
        claim = case_claim(context, CASES[0])
        claim["text"] = "已经退款999元，包裹肯定不是它。"
        return plan(claim)

    result = ReplyCoordinator(store, "alice", composer=model).publish("s")
    assert_safe_case(result["content"], CASES[0])
    assert "退款999" not in result["content"]


@pytest.mark.parametrize("shipment_status,delivered_at", [("in_transit", None),
    ("delivered", None), ("in_transit", "2026-10-24T02:00:00Z"), ("delivered", "2026-10-24T02:00:00Z")])
def test_signature_records_do_not_prove_physical_identity(shipment_status, delivered_at):
    context = catalogue(data={"items": [dict(shipment_id="p1", order_id="O1", status=shipment_status,
        delivered_at=delivered_at, events=[dict(shipment_id="different", event_time="2026-10-27T01:00:00Z"),
            dict(shipment_id="p1", event_time="2026-10-23T01:00:00Z")])]})
    text = render_plan(plan(context["matters"][0]["claims"][0]), context)
    assert "无法判断家人拿到的包裹是否就是这一件" in text
    assert "2026-10-27T01:00:00Z" not in text
    assert "2026-10-23T01:00:00Z" in text
    if shipment_status == "delivered" and delivered_at:
        assert "记录签收时间为 2026-10-24T02:00:00Z" in text
    elif delivered_at:
        assert "记录不一致" in text
    else:
        assert "暂未查到签收时间记录" in text


def test_selection_view_keeps_quality_but_does_not_duplicate_rendered_text():
    from app.agents.reply_composition import selection_context
    context = catalogue()
    view = selection_context(context)
    claim = context["matters"][0]["claims"][0]
    assert "text" not in view["matters"][0]["claims"][0]
    assert "subject_ref" in claim and "text" in claim  # Trusted snapshot stays intact.
    assert view["matters"][0]["sources"][0]["data_as_of"] is None
    assert view["matters"][0]["sources"][0]["completeness"] == "partial"


def test_source_conditions_are_never_clipped_or_dropped():
    limitations = ["范围说明" + str(i) for i in range(6)] + ["同步时间未知；" + "补充说明" * 80 + "不能证明到账"]
    context = catalogue(limitations=limitations)
    text = render_plan(plan(context["matters"][0]["claims"][0]), context)
    assert all(v in text for v in limitations)
    assert not catalogue(limitations=["说明" * 3000])["matters"][0]["claims"]
