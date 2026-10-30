# 文件说明：将工具回执压缩为带类型的答复事实和待补问题。
"""Small, typed projections of tool receipts; model prose is never a receipt."""
from decimal import Decimal, InvalidOperation
import re


QUESTION_LABELS = {
    "order_id": "订单号", "item_id": "商品编号", "item_name": "商品名称",
    "sku_id": "商品规格编号", "product_id": "商品编号", "reason": "具体原因",
    "quantity": "数量", "tracking_number": "物流单号", "platform_code": "购买平台",
    "store_id": "店铺", "channel": "购买渠道", "category": "商品品类",
    "address": "收货地址", "details": "希望如何处理",
}


def label(value, limit=100):
    # Quote data as a single bounded line. The frontend also renders it as text.
    return re.sub(r"[\r\n\t\x00-\x1f]", " ", str(value if value is not None else ""))[:limit]


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


def facts(record, receipts, *, include_context=True):
    """Keep each task's facts together; never mix another task's evidence."""
    lines = []
    for receipt in receipts:
        result, tool = receipt["result"], receipt["tool"]
        if tool=='search_policies':
            lines.extend(label(v,300) for v in result.get('limitations',[]))
            result=result.get('data',{}).get('policy_evidence',{})
            tool='search_knowledge'
        elif tool in __import__('app.tools.catalog',fromlist=['BY_NAME']).BY_NAME:
            from app.tasks.tool_facts import render
            lines.extend(render(receipt['tool'],result,query=record.goal,include_context=include_context))
            continue
        if tool == 'search_knowledge' and result.get('backend') == 'chroma-policy':
            for clause in result.get('results', []):
                scope = '；'.join(QUESTION_LABELS[key] + '：' + (
                    '不限' if clause[key] == '*' else label(clause[key]))
                    for key in ('platform_code', 'store_id', 'channel', 'category'))
                lines.append(f"规则条款（{label(clause['doc'])}，版本 {label(clause['version'])}）：\n"
                             + clause['text'] + '\n来源：' + clause['source_uri']
                             + '\n适用范围：' + scope
                             + '\n版本有效区间：' + label(clause['effective_from']) + ' 至 '
                             + (label(clause['effective_until']) + '（不含结束时刻）' if clause['effective_until'] else '未设置结束时间')
                             + '\n适用依据：' + label(clause['selection_reference'], 1000))
            lines.append('以上为条款依据；当前订单是否符合条件及实际办理结果仍需业务核实。')
            continue
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
        elif tool == 'query_after_sale':
            from app.tasks.external_evidence import after_sale_observation, timestamp
            binding = {k: record.details[k] for k in ('order_id', 'after_sale_id', 'item_id', 'item_name', 'sku_id', 'product_id',
                'quantity', 'tracking_number', 'store_id', 'platform_code') if record.details.get(k) is not None}
            binding['kind'] = 'return' if '退货' in record.goal else 'refund'
            try:
                previous = record.waiting_for.get('external_progress') if isinstance(record.waiting_for, dict) else None
                if previous:
                    binding = dict(previous['binding'])
                    if previous['source'] != 'mysql_order_fields':
                        binding['after_sale_id'] = previous['reference']
                progress = after_sale_observation(binding, result)
                if previous and (timestamp(progress['observed_at']) < timestamp(previous['observed_at'])
                                 or progress['rank'] < previous['rank']):
                    continue
                text = '订单 ' + label(binding['order_id']) + '：' + progress['content']
                if progress['is_demo']:
                    text += ' 本次使用演示业务数据。'
                lines.append(text)
            except (ValueError, KeyError, TypeError):
                continue
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
    if field=='details' and isinstance(question.get('prompt'),str):
        text=prefix+label(question['prompt'],500)
    options = question.get("options", [])
    if options:
        text += "\n" + "；".join(f"{i + 1}. {label(o.get('label') or o.get('value'), 80)}"
                               for i, o in enumerate(options))
    return text
