"""Small, typed projections of tool receipts; model prose is never a receipt."""
from decimal import Decimal, InvalidOperation
import re


QUESTION_LABELS = {
    "order_id": "订单号", "item_id": "商品编号", "item_name": "商品名称",
    "sku_id": "商品规格编号", "product_id": "商品编号", "reason": "具体原因",
    "quantity": "数量", "tracking_number": "物流单号", "platform_code": "购买平台",
    "store_id": "店铺", "address": "收货地址", "details": "希望如何处理",
}


def label(value, limit=100):
    # Quote data as a single bounded line. The frontend also renders it as text.
    return re.sub(r"[\r\n\t\x00-\x1f]", " ", str(value or ""))[:limit]


def money(value):
    if type(value) not in (int, float, str):
        return None
    if isinstance(value, str) and len(value) > 64:
        return None
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and 0 <= amount <= Decimal("1000000000"):
            return f"¥{amount:.2f}"
    except InvalidOperation:
        pass
    return None


def facts(record, receipts):
    """Keep each task's facts together; never mix another task's evidence."""
    lines = []
    for receipt in receipts:
        result, tool = receipt["result"], receipt["tool"]
        if tool == "query_order":
            order = result.get("order", {})
            if (not isinstance(order, dict) or not record.details.get("order_id")
                    or order.get("order_id") != record.details["order_id"]):
                continue
            parts = ["订单 " + label(order["order_id"])]
            if isinstance(order.get("status"), str) and order["status"]:
                parts.append("当前状态：" + label(order["status"], 40))
            if (total := money(order.get("total"))) is not None:
                parts.append("订单金额：" + total)
            if len(parts) > 1:
                lines.append("；".join(parts) + "。")
        elif tool == "query_logistics":
            logistics = result.get("logistics") or {}
            if not isinstance(logistics, dict):
                continue
            oid = record.details.get("order_id")
            tracking = record.details.get("tracking_number")
            if ((oid and logistics.get("order_id") not in (None, "", oid))
                    or (tracking and logistics.get("tracking_number") != tracking)):
                continue
            if ((oid and receipt["arguments"].get("order_id") == oid)
                    or (tracking and receipt["arguments"].get("tracking_number") == tracking)):
                status = logistics.get("status")
                if isinstance(status, str) and status:
                    lines.append(f"订单 {label(oid) if oid else '对应包裹'}的物流状态：{label(status, 80)}。")
        elif tool == "query_product":
            products = result.get("matched_products") or result.get("products") or []
            if not isinstance(products, list):
                continue
            for product in products[:3]:
                if not isinstance(product, dict) or not isinstance(product.get("name"), str) or not product["name"]:
                    continue
                parts = ["商品「" + label(product["name"]) + "」"]
                if (price := money(product.get("price"))) is not None:
                    parts.append("价格：" + price)
                stock = product.get("stock")
                if type(stock) is int and stock >= 0:
                    parts.append("库存：" + str(stock))
                lines.append("；".join(parts) + "。")
    return "\n".join(dict.fromkeys(lines))


def question_text(record):
    question = record.waiting_for or {}
    field = question.get("field") if isinstance(question, dict) else None
    # A model asking for a field already in the record must not ask it again.
    if not field or (field != "details" and record.details.get(field) not in (None, "", [])):
        return None
    prefix = f"关于订单 {label(record.details['order_id'])}，" if record.details.get("order_id") else ""
    text = prefix + "请补充" + QUESTION_LABELS.get(field, "这件事项需要的补充信息") + "。"
    options = question.get("options", [])
    if options:
        text += "\n" + "；".join(f"{i + 1}. {label(o.get('label') or o.get('value'), 80)}"
                               for i, o in enumerate(options))
    return text
