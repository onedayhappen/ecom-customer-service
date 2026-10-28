"""Code-owned conclusions from receipts, before any final model composition.

Missing records are observations, never negative real-world facts. Every claim
keeps its subject, receipt and quality constraints; none are deduplicated across
subjects. The model may select these sentences, but cannot rewrite them.
"""
from datetime import datetime
from hashlib import sha256
import json

from app.tasks.reply_facts import facts, label, money
from app.tasks.tool_facts import STATES


def _subject(row, fallback):
    for key in ("target_ref", "order_ref", "shipment_id", "return_shipment_id"):
        if row.get(key):
            return str(row[key])
    if row.get("order_id"):
        return json.dumps([row.get("platform_code"), row["order_id"]], ensure_ascii=False)
    return fallback


def _order_label(row):
    items = row.get("purchase_summary") or row.get("items") or []
    names = []
    for item in items[:3]:
        name = item.get("title_snapshot") or item.get("name")
        if name:
            specs = item.get("spec_snapshot") or item.get("specs") or {}
            spec = "、".join(label(v, 30) for v in list(specs.values())[:4])
            names.append("「" + label(name, 80) + ("（" + spec + "）" if spec else "") + "」")
    # Identifiers distinguish multiple purchases of the same product. They are
    # displayed only as supporting labels, never requested from the customer.
    return "、".join(names) + "订单 " + label(row.get("order_id"))


def _quality(result):
    return {key: result.get(key) for key in (
        "completeness", "data_as_of", "observed_at", "limitations", "missing_fields", "next_cursor")}


def _qualification(result):
    limitations = result.get("limitations") or []
    if (not isinstance(limitations, list) or any(not isinstance(v, str) for v in limitations)
            or sum(map(len, limitations)) > 3000):
        return None  # Never clip away a condition/negation to fit a reply.
    parts = []
    if result.get("data_as_of"):
        parts.append("数据截至 " + label(result["data_as_of"], 50) + "。")
    else:
        parts.append("数据同步时间未记录，查询时间不代表实际进展时间。")
    if result.get("completeness") != "complete" or result.get("next_cursor"):
        parts.append("以上仅依据本次查到的记录。")
    # Keep source-specific restrictions inseparable from the conclusion.
    parts.extend(label(v, len(v)) for v in limitations
                 if v != "读取时刻不代表上游同步时刻；数据同步时间未记录")
    return " ".join(parts)


def _event_time(event):
    try:
        value = datetime.fromisoformat(event["event_time"].replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo else float("-inf")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return float("-inf")


def _parcel_text(row, events):
    kind = {"original": "原包裹", "exchange": "换货补发包裹"}.get(row.get("shipment_kind"), "包裹")
    if row.get("return_shipment_id"):
        kind = "寄回包裹"
    specs = []
    for item in row.get("items", [])[:3]:
        values = item.get("actual_spec_snapshot") or {}
        specs.extend(label(v, 30) for v in list(values.values())[:4])
    subject = "订单 " + label(row.get("order_id") or "对应售后") + "的" + kind
    subject += "（" + label(row.get("shipment_id") or row.get("return_shipment_id"))
    subject += ("，" + "、".join(specs) if specs else "") + "）"
    status = row.get("status")
    state = STATES.get(status, "未知")
    text = subject + "：物流记录显示" + state + "。"
    if status == "delivered" and row.get("delivered_at"):
        text += "记录签收时间为 " + label(row["delivered_at"], 50) + "。"
    elif row.get("delivered_at"):
        text += "状态与签收时间记录不一致，实际签收情况仍需核实。"
    else:
        text += "暂未查到签收时间记录，不能据此认定实际尚未签收。"
    key = "return_shipment_id" if row.get("return_shipment_id") else "shipment_id"
    events = [event for event in events[:100] if event.get(key) in (None, row.get(key))]
    latest = max(events, key=_event_time, default={})
    if _event_time(latest) != float("-inf"):
        text += "本次查到的最新轨迹时间为 " + label(latest["event_time"], 50) + "。"
    eta = row.get("estimated_delivery")
    text += ("记录预计送达时间为 " + label(eta, 50) + "，实际送达仍需核实。" if eta
             else "暂未记录预计送达时间，无法确认具体哪天到。")
    text += "仅凭这些记录无法确认实际是否已送到小区，也无法判断家人拿到的包裹是否就是这一件。"
    summary = subject + "：物流记录显示" + state + "。"
    if row.get("delivered_at"):
        summary += "记录签收时间 " + label(row["delivered_at"], 50) + "。"
    if _event_time(latest) != float("-inf"):
        summary += "轨迹时间 " + label(latest["event_time"], 50) + "。"
    return summary, text


def build_claims(record, receipts):
    """Pure bounded projection; no DB, network, model, or execution draft access."""
    claims = []
    subjects = {}
    for receipt in receipts[:30]:
        if receipt["result"].get("success") is not True:
            continue
        data = receipt["result"].get("data") or {}
        rows = ([data] if receipt["tool"] == "get_order" else
                data.get("items", [])[:10] if receipt["tool"] == "find_orders" else [])
        for row in rows:
            if row.get("order_ref"):
                subjects[row["order_ref"]] = _order_label(row)
    for receipt in receipts[:30]:
        result, tool = receipt["result"], receipt["tool"]
        if result.get("success") is not True:
            continue
        data = result.get("data") or {}
        args = receipt.get("arguments") or {}
        scope = str(args.get("target_ref") or args.get("order_ref") or args.get("order_id")
                    or args.get("tracking_number") or receipt["event_id"])
        quality = _quality(result)
        qualification = _qualification(result)
        if qualification is None:
            continue

        def add(kind, subject, text, summary=None):
            if not text:
                return
            summary = summary or text
            text += "\n" + qualification
            if record.status == "waiting_external":
                text += "\n后续结果仍需核实，有新进展可以继续咨询。"
            # Do not cut off a constraint when a projection is unexpectedly large.
            if len(text) > 5500:
                return
            claim = dict(task_id=record.task_id, evidence_id=receipt["event_id"],
                         subject_ref=subject, scope_ref=scope, query_scope=args,
                         kind=kind, text=text, quality=quality,
                         selection_hint=summary[:1200])
            claim["claim_id"] = sha256(json.dumps(claim, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
            claims.append(claim)

        if tool in {"get_order", "find_orders"}:
            rows = [data] if tool == "get_order" else data.get("items", [])[:10]
            for row in rows:
                if not row.get("order_id"):
                    continue
                text = _order_label(row) + "：订单记录显示" + STATES.get(row.get("status"), "状态未知") + "。"
                if row.get("created_at"):
                    text += "记录下单时间为 " + label(row["created_at"], 50) + "。"
                if row.get("currency") == "CNY" and (amount := money(row.get("paid_amount"))) is not None:
                    text += "记录已付金额为" + amount + "。"
                add("order_record" if tool == "get_order" else "order_candidate", _subject(row, scope), text)
            if not rows:
                add("search_no_match", scope, "本次按「" + label(args.get("query") or "指定条件")
                    + "」查询暂未找到匹配订单记录，不能据此判断订单不存在。")
        elif tool == "get_logistics":
            shipment = data.get("shipment")
            rows = [shipment] if shipment else data.get("items", [])[:10]
            if not rows:
                # Even a complete empty local result does not prove non-dispatch.
                add("dispatch_unknown", scope,
                    subjects.get(scope, "查询对象「" + label(scope) + "」")
                    + "：本次物流查询暂未查到包裹或发货记录，实际是否已经交给快递仍需核实；暂时无法确认送达时间。")
            for row in rows:
                if not (row.get("shipment_id") or row.get("return_shipment_id")):
                    continue
                summary, text = _parcel_text(row, data.get("items", []) if shipment else row.get("events", []))
                add("parcel_record", _subject(row, scope), text, summary)
        elif tool == "search_products":
            from app.tasks.reply_search_grounding import observation, SearchGuard
            search = observation(receipt)
            if search is not None:
                # Scope and uncertainty travel with each search, including
                # empty results and a later successful rewording.
                text = SearchGuard.correction([[search]])
                projected = facts(record, [receipt])
                add("product_search", scope, text + ("\n" + projected if projected else ""))
        else:
            # Existing typed projections cover policies, payments, refunds,
            # products and legacy tools. Preserve each receipt as a distinct
            # observation; do not let the model infer extra business conclusions.
            text = facts(record, [receipt])
            if text:
                text = "查询记录：\n" + text
                if tool in {"get_refunds", "query_after_sale"}:
                    text += "\n渠道退款成功记录不等于已核实银行卡到账。"
                add("record", _subject(data, scope), text)
    return claims


def fallback_text(claims):
    """Prefer a useful qualified answer over an apology or all search matches."""
    preferred = [c for c in claims if c["kind"] in {"dispatch_unknown", "parcel_record"}]
    if preferred:
        # Don't collapse different orders/packages into one anonymous status.
        source = preferred[0]["evidence_id"]
        selected = [c for c in preferred if c["evidence_id"] == source]
    else:
        selected = ([c for c in claims if c["kind"] == "order_record"] or claims)[:2]
    lines, size = [], 0
    for claim in selected:
        if size + len(claim["text"]) + 2 > 6000:
            break
        lines.append(claim["text"])
        size += len(claim["text"]) + 2
    return "\n\n".join(lines)


def render_plan(value, context):
    """The final publication gate: no model-authored assertions can pass it."""
    if not isinstance(value, str) or len(value) > 6000:
        raise ValueError("invalid_reply_plan")
    plan = json.loads(value)
    if not isinstance(plan, dict) or set(plan) != {"claim_ids"}:
        raise ValueError("invalid_reply_plan_fields")
    selected = plan["claim_ids"]
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 24
            or any(not isinstance(c, str) for c in selected) or len(set(selected)) != len(selected)):
        raise ValueError("invalid_reply_claim_ids")
    catalogue, required = {}, set()
    for matter in context["matters"]:
        if matter.get("claims"):
            required.add(matter["task_id"])
        for claim in matter.get("claims", []):
            if claim["task_id"] != matter["task_id"] or claim["evidence_id"] not in matter["evidence_ids"]:
                raise ValueError("unbound_reply_claim")
            catalogue[claim["claim_id"]] = claim
    if any(c not in catalogue for c in selected):
        raise ValueError("unsupported_reply_claim")
    if {catalogue[c]["task_id"] for c in selected} != required:
        raise ValueError("uncovered_reply_matter")
    text = "\n\n".join(catalogue[c]["text"] for c in selected)
    if len(text) > 6000:
        raise ValueError("reply_plan_too_large")
    return text
