"""Receipt-scoped statements for fulfillment and refunds, without I/O or an LLM.

Absence in a partial read is an observation, not a negative real-world fact.
Critical replies use complete code-owned blocks: checking a few status words in
free prose cannot establish scope, negation, time, or parcel identity.
"""
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import json

from app.services.commerce.common import BusinessError, ref, unref
from app.tasks.reply_facts import label
from app.tasks.tool_facts import STATES


LOGISTICS = {"get_logistics", "get_shipments", "get_shipment_tracking"}
REFUNDS = {"get_refunds", "get_after_sale_status", "get_after_sale"}
SEARCHES = {"find_orders", "find_after_sales"}
INVOICES = {'get_invoices','get_invoice_document'}
TOOLS = LOGISTICS | REFUNDS | SEARCHES | INVOICES | {"get_order", "query_order", "query_logistics", "query_after_sale"}


def _target(receipt):
    args = receipt.get("arguments", {})
    for key in ("target_ref", "order_ref"):
        if args.get(key):
            try:
                return unref(args[key], {"order", "shipment", "return_shipment", "after_sale"})
            except (BusinessError, TypeError):
                return None
    for key, kind in (("order_id", "order"), ("shipment_id", "shipment"),
                      ("after_sale_id", "after_sale"), ("tracking_number", "tracking")):
        if args.get(key):
            return [kind, args[key]]
    return None


def _same_order(data, target):
    return (isinstance(data, dict) and target and target[0] == "order" and str(data.get("order_id")) == target[-1]
            and (len(target) != 3 or data.get("platform_code") == target[1]))


def binding_verified(record, receipt):
    """Cheap typed binding check shared by both publication contracts."""
    tool, result = receipt["tool"], receipt["result"]
    if tool not in LOGISTICS | REFUNDS | {"get_order", "get_dispatch_status", "query_order", "query_logistics", "query_after_sale"}:
        return True
    target = _target(receipt)
    if not target:
        return False
    if target[0] == "order":
        if record.details.get("order_id") not in (None, "", target[-1]):
            return False
        if len(target) == 3 and record.details.get("platform_code") not in (None, "", target[1]):
            return False
    data = result.get("data", {})
    if not isinstance(data, dict):
        return False
    if tool in LOGISTICS:
        parcels = [data["shipment"]] if isinstance(data.get("shipment"), dict) else data.get("items")
        return (isinstance(parcels, list) and all(isinstance(p, dict) for p in parcels)
                and (target[0] != "order" or all(_same_order(p, target) for p in parcels))
                and (target[0] not in {"shipment", "return_shipment"}
                     or all(p.get(target[0] + "_id") == target[-1] for p in parcels)))
    if tool in {"get_order", "get_dispatch_status", "query_order"}:
        return bool(_same_order(result.get("order", {}) if tool == "query_order" else data, target))
    if tool == "query_logistics":
        row = result.get("logistics") or {}
        return isinstance(row, dict) and row.get("order_id") in (None, "", target[-1])
    return True


def _subject(target, orders):
    """Names come from an order receipt matched by platform AND order, never goals."""
    if target[0] == "order":
        order = orders.get(tuple(target), {})
        items = order.get("purchase_summary", order.get("items", []))
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            item = items[0]
            name = item.get("title_snapshot") or item.get("name")
            if name:
                specs = item.get("spec_snapshot", item.get("specs", {}))
                spec = "、".join(label(v, 30) for v in specs.values()) if isinstance(specs, dict) else ""
                return "「" + label(name, 90) + ("（" + spec + "）" if spec else "") + "」"
        return "订单 " + label(target[-1], 80)
    return {"shipment": "包裹 ", "return_shipment": "退回包裹 ",
            "after_sale": "售后申请 ", "tracking": "运单 "}.get(target[0], "查询对象 ") + label(target[-1], 80)


def _date(value):
    # Retain the source timestamp, including its timezone; never replace by 今天.
    return label(value, 50) if value else None


def _event_time(event):
    try:
        parsed = datetime.fromisoformat(event["event_time"].replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _unverified(receipt):
    return dict(evidence_id=receipt["event_id"], target=None, observation={},
                uncertainty={"binding": "unverified"},
                text="这件事项暂时没有可核实的查询结果；业务对象或记录归属尚未确认，不能据此判断发货、签收或退款情况。")


def _logistics(data):
    parcels = [data["shipment"]] if isinstance(data.get("shipment"), dict) else data.get("items", [])
    if not isinstance(parcels, list):
        return [], "包裹数据暂时无法核实。"
    if not parcels:
        return [], "系统本次查询暂未查到包裹记录，不能据此确认实际尚未发货。"
    observations, sentences = [], []
    for parcel in parcels[:5]:
        if not isinstance(parcel, dict):
            continue
        # A tracking page's items are events, not additional parcels.
        events = data.get("items", []) if data.get("shipment") else parcel.get("events", [])
        dated = [e for e in events if isinstance(e, dict) and e.get("event_time")] if isinstance(events, list) else []
        latest = max(dated, key=_event_time, default={})
        status = parcel.get("status") if isinstance(parcel.get("status"), str) else None
        observation = dict(shipment_id=parcel.get("shipment_id", parcel.get("return_shipment_id")),
                           status=status, shipped_at=parcel.get("shipped_at"),
                           delivered_at=parcel.get("delivered_at"),
                           latest_event_time=latest.get("event_time"), latest_event_status=latest.get("status"),
                           estimated_delivery=parcel.get("estimated_delivery"))
        observations.append(observation)
        prefix = ("包裹 " + label(observation["shipment_id"], 80) + "：") if len(parcels) > 1 else ""
        parts = [prefix + "系统记录的包裹状态为“" + STATES.get(status, "未知") + "”"]
        if observation["latest_event_time"]:
            parts.append("本次返回的最新轨迹时间为 " + _date(observation["latest_event_time"]))
        if observation["delivered_at"]:
            parts.append("系统记录的签收时间为 " + _date(observation["delivered_at"]))
        else:
            parts.append("本次未查到签收时间，不能据此确认实际是否已送达，也不能判断家人收到的包裹是否就是这一件")
        if observation["estimated_delivery"]:
            parts.append("记录的预计送达时间为 " + _date(observation["estimated_delivery"]) + "，并非送达承诺")
        else:
            parts.append("暂无可核实的预计送达时间")
        sentences.append("；".join(parts) + "。")
    return observations, "\n".join(sentences) or "包裹数据暂时无法核实。"


def _refunds(data, tool):
    if tool != "get_refunds":
        sale = data.get("after_sale", {})
        status = sale.get("status") if isinstance(sale, dict) else None
        return dict(status=status), ("系统记录的售后申请状态为“" + STATES.get(status, "未知")
            + "”；审核或受理状态不能证明退款已到账，仍需核对退款流水和收款账户。")
    rows = data.get("items", [])
    amounts = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("status") != "succeeded":
            continue
        currency = row.get("currency")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            continue
        try:
            amount = Decimal(str(row.get("amount")))
            if amount.is_finite() and 0 <= amount <= Decimal("1000000000"):
                amounts[currency] = amounts.get(currency, Decimal(0)) + amount
        except InvalidOperation:
            continue
    totals = {currency: format(amount, ".2f") for currency, amount in sorted(amounts.items())}
    text = ("本次返回的退款流水中，渠道报告成功的金额为 "
            + "、".join(amount + " " + currency for currency, amount in totals.items()) + "。"
            if totals else "本次返回的退款流水中暂未查到可核实的成功金额。")
    return dict(success_amounts=totals), text + "渠道成功不代表已核实银行卡入账，也不能据此判断已全额退款。"


def project(record, receipts):
    """Only call with receipts already accepted by ReplyCoordinator._receipts.

    No scans of arbitrary result strings: branch on the tool's typed contract.
    Each block retains its evidence identity, business target and uncertainty.
    """
    orders = {}
    for r in receipts:
        if r.get("result", {}).get("success") is not True:
            continue
        data = r["result"].get("data", {})
        if not isinstance(data, dict):
            continue
        if r["tool"] == "get_order":
            target = _target(r)
            if _same_order(data, target):
                orders[tuple(target)] = data
        elif r["tool"] == "find_orders":
            for order in data.get("items", []) if isinstance(data.get("items"), list) else []:
                if isinstance(order, dict) and order.get("platform_code") and order.get("order_id"):
                    orders.setdefault(("order", order["platform_code"], str(order["order_id"])), order)
    blocks = []
    for receipt in receipts:
        tool, result = receipt["tool"], receipt["result"]
        if tool not in TOOLS or result.get("success") is not True:
            continue
        if tool in INVOICES:
            data=result.get('data', {})
            texts=[]
            for row in data.get('items', [data] if tool=='get_invoice_document' else []):
                if not isinstance(row,dict): continue
                if row.get('order_id') and (record.details.get('order_id') not in (None,'',row['order_id'])
                    or record.details.get('platform_code') not in (None,'',row.get('platform_code'))):
                    continue
                demo=bool(row.get('is_demo')) or result.get('is_demo') is True
                subject='演示票据' if demo else '发票'
                ready=row.get('document_available') or bool(row.get('download_url')) or any(d.get('document_available') for d in row.get('issued_invoices',[]))
                texts.append(subject+' '+label(row.get('invoice_id') or row.get('issued_invoice_id'),80)+('：文件已就绪，可通过本人授权接口下载。' if ready else '：当前状态为“'+label(row.get('status','未核实'),30)+'”，可下载文件尚未核实。'))
            text=' '.join(texts) or '本次查询未载入发票记录，不代表没有开票申请。'
            if result.get('is_demo'):
                from app.services.commerce.simulator import DEMO_NOTICE
                text+=' '+DEMO_NOTICE
            blocks.append(dict(evidence_id=receipt['event_id'],target=None,observation={'invoice_records':len(texts)},uncertainty={'paginated':bool(result.get('next_cursor'))},text=text))
            continue
        if tool in SEARCHES:
            data = result.get("data", {})
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                blocks.append(_unverified(receipt))
                continue
            if not rows:
                applied = data.get("applied_conditions", {})
                query = applied.get("query") if isinstance(applied, dict) else None
                scope = "按“" + label(query, 80) + "”检索时" if query else "该次检索条件下"
                blocks.append(dict(evidence_id=receipt["event_id"], target=None, observation={"matches": 0, "search": tool},
                    uncertainty={"scope": "query_conditions"},
                    text=scope + "暂未找到匹配记录；不能据此认定订单或售后不存在，也不能判断实际发货、签收或退款情况。"))
            for row in rows:
                if not isinstance(row, dict):
                    blocks.append(_unverified(receipt))
                    continue
                if tool == "find_orders" and row.get("order_id") and row.get("platform_code"):
                    derived = dict(receipt, tool="get_order", arguments={"order_ref": ref("order", row["platform_code"], row["order_id"])},
                                   result=dict(result, data=row))
                elif tool == "find_after_sales" and row.get("after_sale_id"):
                    derived = dict(receipt, tool="get_after_sale_status", arguments={"after_sale_id": row["after_sale_id"]},
                                   result=dict(result, data={"after_sale": row}))
                else:
                    blocks.append(_unverified(receipt))
                    continue
                blocks.extend(project(record, [derived]))
            continue
        target = _target(receipt)
        if not target:
            blocks.append(_unverified(receipt))
            continue
        # A bound task cannot borrow a different order's receipt.
        if target[0] == "order" and record.details.get("order_id") not in (None, "", target[-1]):
            blocks.append(_unverified(receipt))
            continue
        if (target[0] == "order" and len(target) == 3
                and record.details.get("platform_code") not in (None, "", target[1])):
            blocks.append(_unverified(receipt))
            continue
        data = result.get("data", {})
        if not isinstance(data, dict):
            blocks.append(_unverified(receipt))
            continue
        if tool in LOGISTICS:
            parcels = [data["shipment"]] if isinstance(data.get("shipment"), dict) else data.get("items", [])
            if (not isinstance(parcels, list) or any(not isinstance(p, dict) for p in parcels)
                    or ("shipment" not in data and "items" not in data)
                    or (target[0] == "order" and any(not _same_order(p, target) for p in parcels))
                    or (target[0] in {"shipment", "return_shipment"}
                        and any(p.get(target[0] + "_id") != target[-1] for p in parcels))):
                blocks.append(_unverified(receipt))
                continue
            observation, text = _logistics(data)
        elif tool in REFUNDS:
            observation, text = _refunds(data, tool)
        elif tool in {"get_order", "query_order"}:
            order = result.get("order", {}) if tool == "query_order" else data
            if not _same_order(order, target):
                blocks.append(_unverified(receipt))
                continue
            status = order.get("status") if isinstance(order.get("status"), str) else "未知"
            observation = dict(status=status)
            text = "系统记录的订单状态为“" + STATES.get(status, label(status, 30)) + "”。"
            if order.get("created_at"):
                text += "记录的下单时间为 " + _date(order["created_at"]) + "。"
            amount = order.get("paid_amount") if tool == "get_order" else order.get("total")
            if amount is not None:
                from app.tasks.reply_facts import money
                formatted = money(amount)
                if formatted:
                    if tool == "query_order":
                        text += "记录的订单金额为 " + formatted + "。"
                    elif order.get("currency"):
                        text += "记录的已付金额为 " + formatted[1:] + " " + label(order["currency"], 3) + "。"
            if status in {"paid", "已支付", "pending_receipt", "待收货"}:
                text += "订单状态不能单独证明实际是否发货或签收。"
        elif tool == "query_logistics":
            logistics = result.get("logistics") or {}
            if not isinstance(logistics, dict) or logistics.get("order_id") not in (None, "", target[-1]):
                blocks.append(_unverified(receipt))
                continue
            observation = dict(status=logistics.get("status"))
            text = "系统记录的物流状态为“" + label(observation["status"] or "未知", 40) + "”；实际配送情况仍需核实。"
        else:
            observation = {}
            text = "已查询售后记录；实际退款到账情况仍需核对退款流水和收款账户。"
        uncertainty = dict(data_as_of=result.get("data_as_of"), completeness=result.get("completeness", "unknown"),
                           paginated=bool(result.get("next_cursor") or data.get("has_more")))
        if not uncertainty["data_as_of"]:
            text += "上游数据同步时间未记录。"
        else:
            text += "数据截至 " + _date(uncertainty["data_as_of"]) + "。"
        if uncertainty["paginated"]:
            text += "本次仅返回部分记录，需要时可继续查询。"
        if result.get('is_demo'):
            from app.services.commerce.simulator import DEMO_NOTICE
            text += DEMO_NOTICE
        blocks.append(dict(evidence_id=receipt["event_id"], target=target, observation=observation,
                           uncertainty=uncertainty, text=_subject(target, orders) + "：" + text))
    return blocks


def choices(context):
    """Code owns assertions; the existing composition call only selects blocks."""
    if not any(m.get("grounding") for m in context.get("matters", [])):
        return []
    parts, seen = [], set()
    for matter in context["matters"]:
        blocks = matter.get("grounding", [])
        resolved = {b["target"][0] for b in blocks if b.get("target")}
        # Failed keyword attempts are not competing conclusions once this task
        # has actual matching objects of the same type. Keep them in the audit.
        blocks = [b for b in blocks if not (
            isinstance(b.get("observation"), dict) and b["observation"].get("matches") == 0
            and {"find_orders": "order", "find_after_sales": "after_sale"}.get(b["observation"].get("search")) in resolved)]
        other = matter.get("verified_facts") or matter.get("fallback")
        texts = [b["text"] for b in blocks] or ([other] if other else [])
        for text in texts:
            key = (matter["task_id"], text)
            if key not in seen:
                parts.append(dict(id="fact-" + str(len(parts)), task_id=matter["task_id"], text=text))
                seen.add(key)
    return parts


def response(context):
    """Complete local fallback on invalid output; never ask the model to repair."""
    blocks = choices(context)
    return "\n\n".join(b["text"] for b in blocks) if blocks else None


def validate(text, context):
    """Fail closed on any free rewrite of a critical block, including omission.

    This intentionally checks whole statements, not regexes for status tokens:
    'probably not that shoebox' has no status token but is still unsupported.
    """
    blocks = choices(context)
    if not blocks:
        return text
    by_id = {b["id"]: b for b in blocks}
    selected = []
    try:
        if text.lstrip().startswith("{"):
            value = json.loads(text)
            if set(value) != {"blocks"} or not isinstance(value["blocks"], list):
                raise ValueError
            selected = [by_id[key] for key in value["blocks"]]
        else:
            # The production composer already renders the selection. Recheck
            # that exact content at the coordinator boundary as well.
            remaining = text.strip()
            while remaining:
                used = {b["id"] for b in selected}
                block = next((b for b in blocks if b["id"] not in used and
                    (remaining == b["text"] or remaining.startswith(b["text"] + "\n\n"))), None)
                if block is None:
                    raise ValueError
                selected.append(block)
                remaining = remaining[len(block["text"]):].removeprefix("\n\n")
        if (not selected or len({b["id"] for b in selected}) != len(selected)
                or {b["task_id"] for b in selected} != {b["task_id"] for b in blocks}):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("ungrounded_composed_reply") from None
    return "\n\n".join(b["text"] for b in selected)
