# 文件说明：测试客户回复简洁性、字段投影及原始业务证据完整保留。
"""Customer-visible replies stay concise while original evidence stays intact."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.tasks.replies import ReplyCoordinator
from app.tasks.tool_facts import render
from tests.test_coordinated_replies import replies
from tests.test_reply_grounding import save_receipts
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store


CASES = json.loads((Path(__file__).parent / "fixtures/youyou_product_search_grounding.json").read_text(encoding="utf8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=["phone", "pan"])
@pytest.mark.parametrize("mode", ["selected", "invalid_model", "no_model"])
def test_archived_product_receipts_through_final_publication(store, case, mode):
    record, source = task(store, "s", goal=case["user"], details={"item_name": case["subject"]})
    original = deepcopy(case["receipts"])
    _, ids = save_receipts(store, record, source, original)
    calls = []
    def compose(context):
        calls.append(context)
        if mode == "invalid_model":
            return "商品编号：raw；规格编码：raw"
        return json.dumps({"claim_ids": [m["claims"][-1]["claim_id"] for m in context["matters"] if m["claims"]]})
    coordinator = ReplyCoordinator(store, "alice", composer=None if mode == "no_model" else compose)
    result = coordinator.publish("s")
    text = result["content"]
    product = original[-1]["result"]["data"]["items"][0]
    assert product["name"] in text
    assert "标价" in text and "库存记录" in text
    assert len(text) < 1800
    for unwanted in ("商品编号", "规格编号", "规格编码", "商家编号", "创建时间", "更新时间", "明细 /", "spec_values", "sku_id"):
        assert unwanted not in text
    assert product["product_id"] not in text
    assert product["sku_summary"][0]["sku_code"] not in text
    assert "数据同步时间未记录" in text
    assert sorted(result["evidence_ids"]) == sorted(ids)
    assert original == case["receipts"]
    assert len(calls) == (0 if mode == "no_model" else 1)
    assert coordinator.publish("s") is None and len(replies(store)) == 1
    with store.transaction(read_only=True) as tx:
        evidence = tx.all("SELECT payload FROM support_events WHERE kind='task_tool_result'")
        assert product["product_id"] in "".join(r["payload"] for r in evidence)
        assert tx.one("SELECT COUNT(*) AS n FROM support_operations")["n"] == 0


def test_refund_totals_keep_currency_status_and_bank_uncertainty():
    text = "\n".join(render("get_refunds", dict(success=True, data=dict(
        amounts_by_currency_and_status={"CNY": {"succeeded": "159", "processing": "10"}, "USD": {"failed": "8"}},
        items=[dict(refund_id="secret-internal", amount="999999", currency="CNY")]))))
    assert "159.00元" in text and "处理中" in text and "10.00元" in text
    assert "失败" in text and "8.00美元" in text
    assert "不等于已核实银行卡到账" in text and "不能单独证明已经全额退款" in text
    assert "999999" not in text and "secret-internal" not in text


def test_optional_attributes_cannot_turn_into_arbitrary_field_dump():
    result = dict(success=True, data=dict(product=dict(name="短袖", specs={"material": {"raw": "DO-NOT-PRINT"}}),
        items=[dict(spec_values={"颜色": "黑色", "尺码": "L", "debug_payload": {"token": "DO-NOT-PRINT"}},
                    list_price="99", currency="CNY", inventory=None, sku_id="internal", updated_at="timestamp")]))
    text = "\n".join(render("get_product_details", result, query="黑色L有没有，面料是什么"))
    assert "黑色、L" in text and "标价99.00元" in text
    assert "库存暂未核实" in text and "资料未记录面料" in text
    assert "DO-NOT-PRINT" not in text and "internal" not in text and "timestamp" not in text
    assert "缺货" not in text


def test_full_restrictions_survive_and_unknown_currency_is_not_guessed():
    restriction = "只有退回全部赠品才可办理；否则不能退款。"
    result = dict(success=True, data=dict(eligibility="unknown", requested_amount="159"), limitations=[restriction])
    text = "\n".join(render("assess_after_sale", result))
    assert restriction in text and "159.00（币种未核实）" in text
    assert "159.00元" not in text and "尚未提交售后申请或退款" in text


def test_unknown_shape_has_no_recursive_fallback():
    text = "\n".join(render("unknown_future_tool", dict(success=True, data={
        "deep": {"items": [{"name": "DO-NOT-PRINT", "status": "succeeded", "private_id": "x"}]}})))
    assert "暂时不足" in text and "DO-NOT-PRINT" not in text and "succeeded" not in text


def test_standalone_search_summary_keeps_the_actual_price_and_spec_scope():
    text = "\n".join(render("search_products", dict(success=True, data=dict(items=[], total=0,
        applied_conditions=dict(query="衣服", max_price=dict(amount="100", currency="CNY"),
                                specs=[dict(name="尺码", value="L")])))))
    assert "100.00元" in text and "尺码为L" in text and "本次条件下" in text
    assert "不能据此判断商品不存在" in text


def test_confirmation_is_never_presented_as_success():
    assert render("claim_coupon", dict(success=True, data=dict(next_state="needs_confirmation", status="available"))) == [
        "已准备办理内容，请查看确认卡片；尚未执行。"]


def test_after_sale_assessment_preserves_the_contract_amount_and_quantity():
    text = "\n".join(render("assess_after_sale", dict(success=True, data=dict(
        eligibility="eligible", amount="159", quantity=1, currency="CNY", order_ref="internal"))))
    assert "可申请金额为159.00元" in text and "核验数量为1件" in text
    assert "不代表整单数量" in text and "尚未提交" in text and "internal" not in text


def test_return_requirements_and_document_availability_are_not_lost():
    text = "\n".join(render("get_return_instructions", dict(success=True, data=dict(
        address=dict(province_name="浙江省", city_name="杭州市", detail="退货仓", recipient="收件员", phone="***"),
        deadline="2026-11-01", requirements=["必须寄回赠品，否则无法验收。"], authorization_id="internal"))))
    assert "浙江省杭州市退货仓" in text and "2026-11-01" in text
    assert "必须寄回赠品，否则无法验收。" in text and "internal" not in text
    result = dict(success=True, data=dict(invoice_number="1234", download_url="/api/commerce/invoices/internal/document"))
    assert "文件已就绪" in "".join(render("get_invoice_document", result))
    assert "1234" not in "".join(render("get_invoice_document", result))
    assert "1234" in "".join(render("get_invoice_document", result, query="发票号码是什么"))
