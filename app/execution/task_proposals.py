"""Prepare only operations the business adapter can verify and submit exactly."""
from decimal import Decimal, InvalidOperation
import json

MUTATION_INTENTS = {
    'apply_refund': 'refund_service',
    'issue_invoice': 'invoice_request',
    'reissue_invoice': 'invoice_request',
}
from app.tasks.business_models import InvoiceFields


class PreparationRejected(ValueError):
    def __init__(self, message, *, field=None, manual=False):
        super().__init__(message)
        self.field, self.manual = field, manual


def minor_units(value):
    if type(value) not in (str, int, float, Decimal) or isinstance(value, bool):
        raise PreparationRejected("业务金额格式不正确，需人工核实", manual=True)
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount < 0 or amount > 10**12 or amount != amount.to_integral_value():
            raise InvalidOperation()
        return int(amount)
    except (InvalidOperation, ValueError):
        raise PreparationRejected("业务金额不能精确转换成整数分，需人工核实", manual=True) from None


def fixed_arguments(record, action):
    if action not in MUTATION_INTENTS:
        raise PreparationRejected("当前没有办理这项操作的接口，需由人工核实处理", manual=True)
    details = record.details
    if not isinstance(details.get("order_id"), str) or not details["order_id"].strip():
        raise PreparationRejected("请先明确订单号", field="order_id")
    if len(str(details["order_id"])) > 40 or details.get("platform_code", "pdd") != "pdd":
        raise PreparationRejected("当前业务接口无法核验该订单范围", manual=True)
    if action == "apply_refund":
        if not details.get("reason"):
            raise PreparationRejected("请补充退款原因", field="reason")
        if not isinstance(details["reason"], str) or not details["reason"].strip() or not 1 <= len(details["reason"]) <= 255:
            raise PreparationRejected("退款原因长度不符合业务接口要求", field="reason")
        return dict(order_id=details["order_id"], reason=details["reason"])
    for field in ("invoice_title", "invoice_type"):
        if not details.get(field):
            raise PreparationRejected("请补充" + ("发票抬头" if field == "invoice_title" else "发票类型"), field=field)
    kind = {"个人": "personal", "公司": "company", "企业": "company", "增值税专用": "vat_special"}.get(
        details["invoice_type"], details["invoice_type"])
    if kind != "personal" and not details.get("tax_id"):
        raise PreparationRejected("企业发票需要税号", field="tax_id")
    try:
        fields = InvoiceFields(title=details["invoice_title"], invoice_type=kind, tax_id=details.get("tax_id"))
    except ValueError:
        raise PreparationRejected("发票字段不符合业务接口要求", field="invoice_type") from None
    args = dict(order_id=details["order_id"], fields=fields.model_dump())
    if action == "reissue_invoice":
        if not details.get("invoice_id"):
            raise PreparationRejected("请提供要重开的原发票编号", field="invoice_id")
        args["invoice_id"] = details["invoice_id"]
    return args


def verify_whole_order(record, order):
    """An order-only interface must never silently expand a line-level request."""
    if order.get("order_id") != record.details["order_id"] or order.get("platform_code", "pdd") != "pdd":
        raise PreparationRejected("业务预检对象与事项不一致", manual=True)
    if record.details.get("store_id") and order.get("store_id") != record.details["store_id"]:
        raise PreparationRejected("无法核实订单店铺", manual=True)
    items = order.get("items") or []
    items = json.loads(items) if isinstance(items, str) else items
    if not isinstance(items, list) or not items or any(not isinstance(i, dict) for i in items):
        raise PreparationRejected("缺少完整的订单商品明细，不能按整单办理", manual=True)
    quantities = [i.get("quantity") for i in items]
    if any(type(q) is not int or q < 1 for q in quantities):
        raise PreparationRejected("无法核实商品数量", manual=True)
    selected = {k: record.details[k] for k in ("item_id", "sku_id", "product_id", "item_name") if record.details.get(k)}
    if len(items) > 1 and selected:
        raise PreparationRejected("当前接口只支持整单，不能替代指定商品的部分办理", manual=True)
    if record.details.get("quantity") is not None and record.details["quantity"] != sum(quantities):
        raise PreparationRejected("当前接口不支持指定数量的部分办理", manual=True)
    if len(items) == 1:
        for key, value in selected.items():
            actual = items[0].get("name" if key == "item_name" else key)
            if actual != value:
                raise PreparationRejected("无法核实事项中的商品与订单明细一致", manual=True)
    if record.details.get("conditions"):
        # Conditional writes need business-verifiable predicates. Free text
        # such as "有货才退" is not an executable authorization condition.
        raise PreparationRejected("这项操作带有尚未核实的办理条件，需人工处理", manual=True)
    return [dict(name=str(i.get("name") or i.get("item_id") or "未命名商品")[:200], quantity=i["quantity"])
            for i in items]
