"""Small typed customer answers, never a recursive dump of a tool payload.

Only explicit business fields enter prose. Raw receipts and identifiers remain
in the evidence store for binding, audit and confirmation, not in this view.
"""
from decimal import Decimal, InvalidOperation
import re

from app.tasks.reply_facts import label


SPEC_NAMES = {
    "颜色", "色系", "尺码", "码数", "容量", "内存", "规格", "尺寸", "型号", "材质", "面料", "重量",
    "color", "size", "capacity", "memory", "material", "weight", "model",
}


def scalar(value, limit=100):
    return label(value, limit) if isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool) else ""


def amount(value, currency=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return ""
    if len(str(value)) > 64:
        return ""
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 1000000000:
            return ""
    except InvalidOperation:
        return ""
    unit = {"CNY": "元", "USD": "美元", "EUR": "欧元", "GBP": "英镑"}.get(currency) if isinstance(currency, str) else None
    # An absent/unknown currency is not implicitly RMB.
    return format(number, ".2f") + (unit or "（币种未核实）")


def rows(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def specs(row):
    value = row.get("spec_values") or row.get("spec_snapshot") or row.get("specs") or {}
    if not isinstance(value, dict):
        return []
    return list(dict.fromkeys(scalar(value[k], 45) for k in sorted(SPEC_NAMES, key=lambda k: (k not in {"颜色", "color", "色系"}, k))
                             if scalar(value.get(k), 45)))[:4]


def subject(row, default="这件商品"):
    name = scalar(row.get("product_name") or row.get("title_snapshot") or row.get("name"))
    return ("「" + name + "」" if name else default) + ("（" + "、".join(specs(row)) + "）" if specs(row) else "")


def state(value):
    from app.tasks.tool_facts import STATES
    return STATES.get(value, "暂未核实") if isinstance(value, str) else "暂未核实"


def _products(name, data, query):
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    products = rows(data.get("items")) if name == "search_products" else [product or data]
    lines, partial = [], len(products) > 3
    for product in products[:3]:
        prefix = subject(product)
        if product.get("merchant_name"):
            prefix += "（" + scalar(product["merchant_name"]) + "）"
        variants = rows(product.get("sku_summary")) if name == "search_products" else rows(data.get("items"))
        if not variants and ("list_price" in product or "inventory" in product):
            variants = [product]
        # Put explicitly mentioned specs first, without asserting unmatched
        # conditions were satisfied or choosing a SKU for a business action.
        variants = sorted(variants, key=lambda r: -sum(v.casefold() in query.casefold() for v in specs(r)))
        pieces = []
        for sku in variants[:2]:
            parts = ["、".join(specs(sku)) or "该规格"]
            price = amount(sku.get("list_price"), sku.get("currency"))
            if price:
                parts.append("标价" + price)
            inventory = sku.get("inventory")
            stock = inventory.get("available_qty") if isinstance(inventory, dict) else None
            if type(stock) is int and stock >= 0:
                parts.append("库存记录" + str(stock) + "件" if re.search("多少|数量|几件", query)
                             else "库存记录有货" if stock > 0 else "库存记录为零")
            else:
                parts.append("库存暂未核实")
            pieces.append("，".join(parts))
        lines.append(prefix + ("：" + "；".join(pieces) if pieces else "：暂未查到可核实的规格报价") + "。")
        if name == "get_product_details":
            attrs = product.get("specs") or {}
            if isinstance(attrs, dict):
                for key in ("材质", "面料", "重量", "尺寸"):
                    if key in query:
                        aliases = {"材质": ("材质", "面料", "material"), "面料": ("面料", "材质", "material"),
                                   "重量": ("重量", "weight"), "尺寸": ("尺寸", "size")}[key]
                        # Catalog imports keep customer parameters in one known
                        # nested container. Read only named business attributes,
                        # never recurse through arbitrary metadata.
                        nested = attrs.get("参数")
                        sources = [attrs] + ([nested] if isinstance(nested, dict) else [])
                        value = next((scalar(source.get(k)) for source in sources for k in aliases
                                      if scalar(source.get(k))), "")
                        lines.append("资料记录的" + key + "是" + value + "。" if value else "资料未记录" + key + "，暂时无法确认。")
                if attrs.get("specification_verification") == "source_example_unverified":
                    lines.append("这些参数来自示例商品资料，尚未核实当前在售商品是否一致。")
        partial |= len(variants) > 2 or bool(product.get("skus_truncated"))
        if product.get("unapplied_conditions"):
            lines.append("部分规格条件未能应用，不能认为这些商品已满足所有要求。")
    if not products:
        lines.append("本次条件下暂未找到匹配商品记录，不能据此判断商品不存在。")
    if data.get("unapplied_conditions"):
        lines.append("部分规格条件未能应用，仍需核实是否符合要求。")
    return lines, partial


def _money_fields(data, fields):
    return [title + value + "。" for key, title in fields
            if (value := amount(data.get(key), data.get("currency") or data.get("order_currency")))]


def describe(name, data, query=""):
    """Return sentences and whether displayed rows are a partial selection."""
    if name in {"search_products", "get_product_details"}:
        return _products(name, data, query)
    items = rows(data.get("items"))
    lines, partial = [], False
    if name in {"get_refunds", "get_payments"}:
        if name == "get_refunds":
            totals = data.get("amounts_by_currency_and_status")
            if isinstance(totals, dict):
                for currency, statuses in sorted(totals.items()):
                    if isinstance(statuses, dict):
                        for status, value in sorted(statuses.items()):
                            if status in {"succeeded", "processing", "pending", "failed"} and (text := amount(value, currency)):
                                lines.append("渠道退款记录：" + state(status) + "的金额为" + text + "。")
            if not lines and not items and data.get("total") == 0:
                lines.append("本次暂未查到退款流水，不能据此认定没有退款或已经到账。")
            if not lines:
                lines += _money_fields(data, [("recorded_success_amount", "渠道报告退款成功金额为")])
            if not lines:
                for item in items[:3]:
                    value = amount(item.get("amount"), item.get("currency"))
                    lines.append("一笔退款记录为" + state(item.get("status")) + ("，金额" + value if value else "，金额暂未核实") + "。")
                partial = len(items) > 3
            if not lines:
                lines.append("本次暂未查到可核实的退款金额。")
            lines.append("渠道成功不等于已核实银行卡到账；本次记录也不能单独证明已经全额退款。")
        else:
            totals = data.get("recorded_success_amounts")
            if isinstance(totals, dict):
                lines += ["渠道报告支付成功金额为" + text + "。" for currency, value in sorted(totals.items()) if (text := amount(value, currency))]
            lines += _money_fields(data, [("order_paid_amount", "订单记录的已付金额为")])
            lines.append("这些是支付记录，不能据此确认银行卡余额或未同步的扣款。")
    elif name == "get_dispatch_status":
        order = scalar(data.get("order_id"))
        prefix = "订单 " + order + "：" if order else ""
        for row in items[:3]:
            when = scalar(row.get("created_at"))
            lines.append(prefix + (when + "登记的" if when else "这笔")
                         + "催单记录状态为" + state(row.get("status")) + "。")
        if not items and data.get("total") == 0:
            lines.append(prefix + "本次暂未查到催单登记记录。")
        lines.append("催单受理或处理完成不等于已经发货，实际发货情况仍需查询物流。")
        partial = len(items) > 3
    elif name in {"check_coupon", "assess_order_action", "assess_after_sale", "assess_price_protection"}:
        lines.append("本次核验结果：" + state(data.get("eligibility")) + "。")
        lines += _money_fields(data, [("requested_amount", "可申请金额为"), ("discount_amount", "已核实优惠金额为"), ("threshold_gap", "距离使用门槛还差")])
        if name in {"assess_after_sale", "assess_price_protection"}:
            lines += _money_fields(data, [("amount", "本次核验的可申请金额为")])
            if type(data.get("quantity")) is int:
                lines.append("本次核验数量为" + str(data["quantity"]) + "件，不代表整单数量。")
        for reason in data.get("reasons", []) if isinstance(data.get("reasons"), list) else []:
            if isinstance(reason, str):
                lines.append(label(reason, len(reason)))
        rules = {"threshold": "金额门槛", "scope": "适用商品范围", "instance_status": "优惠券状态",
                 "currency": "币种", "platform": "购买平台"}
        for check in rows(data.get("checks")):
            if check.get("status") in {"fail", "unknown"} and check.get("rule") in rules:
                lines.append(rules[check["rule"]] + ("不满足要求。" if check["status"] == "fail" else "尚未核实。"))
        if name == "assess_after_sale":
            lines.append("这是资格核验，尚未提交售后申请或退款。")
    elif name in {"estimate_purchase", "quote_checkout", "quote_delivery"}:
        lines += _money_fields(data, [("goods_amount", "商品金额为"), ("discount_amount", "优惠金额为"),
            ("goods_after_discount", "优惠后商品金额为"), ("shipping_amount", "运费为"), ("payable_amount", "应付金额为")])
        if data.get("shipping_amount") is None:
            lines.append("运费尚未核实，暂时不能确认最终实付金额。")
        delivery = data.get("delivery_days")
        if isinstance(delivery, dict):
            low, high = delivery.get("min"), delivery.get("max")
            if type(low) is int and type(high) is int and 0 <= low <= high:
                lines.append(f"配送规则预计需要{low}至{high}天，不代表送达承诺。")
        lines.append("本次仅为试算，尚未下单或付款。")
    elif name in {"get_membership", "list_points"}:
        balance = data.get("ledger_balance")
        if type(balance) is int:
            lines.append("已入账积分余额为" + str(balance) + "分。")
        membership = data.get("membership")
        if isinstance(membership, dict):
            title = scalar(membership.get("name") or membership.get("level_name"))
            if title:
                lines.append("会员等级为" + title + "。")
        vip = data.get("vip")
        if isinstance(vip, dict):
            status = {"active": "有效", "expired": "已过期", "not_opened": "未开通", "suspended": "已暂停"}.get(vip.get("status"), "暂未核实")
            lines.append("付费会员状态为" + status + "。")
            subscription = vip.get("subscription")
            if isinstance(subscription, dict) and scalar(subscription.get("ends_at")):
                lines.append("当前付费会员订阅记录的到期时间为" + scalar(subscription["ends_at"]) + "。")
        if name == "list_points":
            for row in items[:3]:
                if type(row.get("delta")) is int:
                    lines.append((scalar(row.get("created_at")) or "一笔积分记录") + "：积分变动" + str(row["delta"]) + "分。")
            partial = len(items) > 3
    elif name in {"get_coupon", "find_my_coupons", "list_claimable_coupons", "list_promotions", "claim_coupon"}:
        # Detail `items` are scope/redemption records, NOT coupon instances.
        # Lists expose a template on each row; details expose it at the root.
        instance = data.get("instance") if isinstance(data.get("instance"), dict) else {}
        coupon_rows = ([dict(instance, template=data["template"])]
                       if isinstance(data.get("template"), dict) else items if "items" in data else [data])
        for row in coupon_rows[:3]:
            rule = row.get("template") if isinstance(row.get("template"), dict) else row
            text = scalar(rule.get("name")) or "这项优惠"
            minimum, discount = amount(rule.get("threshold_amount"), rule.get("currency")), amount(rule.get("discount_amount"), rule.get("currency"))
            if minimum:
                text += "，使用门槛" + minimum
            if discount:
                text += "，减免" + discount
            if row.get("status"):
                text += "，记录状态为" + state(row["status"])
            if row.get("expires_at"):
                text += "，到期时间" + scalar(row["expires_at"])
            lines.append(text + "。")
        lines.append("范围相关或可领取不等于本次购物一定可用，还需核验商品和门槛。")
        partial = len(coupon_rows) > 3
    elif name in {"get_order_items", "get_invoices"}:
        for row in (items if "items" in data else [data])[:3]:
            if name == "get_order_items":
                text = subject(row)
                if type(row.get("quantity")) is int:
                    text += "，购买数量" + str(row["quantity"]) + "件"
                value = amount(row.get("unit_price"), row.get("currency") or data.get("currency"))
                if value:
                    text += "，购买单价" + value
            else:
                kind = "正式发票" if data.get('collection') == 'issued' else "开票申请"
                text = kind + ("「" + scalar(row["title"]) + "」" if row.get("title") else "") + "的记录状态为" + state(row.get("status"))
            lines.append(text + "。")
        if name == "get_invoices":
            lines.append("文件是否可下载仍需核实。" if data.get('collection') == 'issued'
                         else "申请记录不等于已取得正式发票，文件是否就绪仍需核实。")
        partial = len(items) > 3
    elif name in {"get_shipment_items", "get_after_sale_items"}:
        quantity_label = "包裹记录数量" if name == "get_shipment_items" else "售后申请数量"
        for row in items[:3]:
            text = "原购买商品" + subject(row)
            if name == "get_shipment_items" and row.get("actual_spec_snapshot"):
                actual = specs({'spec_snapshot': row['actual_spec_snapshot']})
                if actual:
                    text += "，包裹记录规格为" + "、".join(actual)
            if type(row.get("quantity")) is int:
                text += "，" + quantity_label + str(row["quantity"]) + "件"
            lines.append(text + "。")
        lines.append("明细记录不代表已发货、签收或退款完成。")
        partial = len(items) > 3
    elif name == "get_restock_plan":
        for row in items[:3]:
            date = scalar(row.get("expected_available_at") or row.get("expected_received_at"))
            if date:
                lines.append("记录的补货计划时间为" + date + "，不是承诺到货时间。")
        partial = len(items) > 3
        if not items:
            lines.append("本次暂未查到补货计划，不能据此认定不会补货，也无法确认到货时间。")
    elif name == "get_price_history":
        minimums = data.get("recorded_minimum")
        if isinstance(minimums, dict):
            for key, value in list(minimums.items())[:3]:
                price_type, _, currency = str(key).partition(":")
                title = {"list": "标价", "sale": "售价", "promotion": "活动价"}.get(price_type, "价格")
                if text := amount(value, currency):
                    lines.append("本次记录中的最低" + title + "为" + text + "，不代表完整历史最低价。")
            partial = len(minimums) > 3
    elif name == "get_merchant":
        lines.append((scalar(data.get("name")) or "该商家") + "的记录状态为" + state(data.get("status")) + "。")
    elif name == "get_invoice_document":
        ready = data.get("document_available") is True or (isinstance(data.get("download_url"), str)
            and data["download_url"].startswith("/api/commerce/invoices/"))
        lines.append("正式发票文件已就绪，请从发票详情页下载。" if ready
                     else "暂未取得可下载的正式发票文件。")
        if re.search("票号|发票号码", query) and scalar(data.get("invoice_number")):
            lines.append("发票号码是" + scalar(data["invoice_number"]) + "。")
    elif name in {"list_addresses", "get_return_instructions"}:
        addresses = items if name == "list_addresses" else [data.get("address")]
        for address in rows(addresses)[:3]:
            parts = [scalar(address.get(k), 300) for k in ("province_name", "city_name", "district_name", "detail")]
            location = "".join(p for p in parts if p and p != "***")
            title = "退货地址" if name == "get_return_instructions" else scalar(address.get("label")) or "保存的地址"
            lines.append(title + ("：" + location if location else "的具体地址暂未展示，请在地址详情页核对") + "。")
            if name == "get_return_instructions":
                for field, title in (("recipient", "收件人"), ("phone", "联系电话")):
                    if scalar(address.get(field)):
                        lines.append(title + "：" + scalar(address[field]) + "。")
        if name == "get_return_instructions":
            carriers = [scalar(v.get("name")) for v in rows(data.get("carriers")) if scalar(v.get("name"))]
            if carriers:
                lines.append("授权可选快递为" + "、".join(carriers) + "。")
            if data.get("deadline"):
                lines.append("请在" + scalar(data["deadline"]) + "前按授权要求寄回。")
            requirements = data.get("requirements")
            if isinstance(requirements, list):
                lines.extend(label(v, len(v)) for v in requirements if isinstance(v, str))
        partial = len(items) > 3
    elif name == "get_my_account":
        profile = data.get("profile") or {}
        title = scalar(profile.get("display_name")) if isinstance(profile, dict) else ""
        title = title or scalar(data.get("username"))
        if title:
            lines.append("当前账号显示名称为" + title + "。")
        contacts = items if data.get('collection') == 'contacts' else rows(data.get('contacts'))
        for contact in contacts[:3]:
            channel = {"phone": "手机号", "email": "邮箱"}.get(contact.get("contact_type"), "联系方式")
            lines.append(channel + ("已验证，完整内容已隐藏。" if contact.get("verified_at") else "暂未查到验证记录。"))
        platforms = items if data.get('collection') == 'platform_accounts' else rows((data.get('platform_accounts') or {}).get('items'))
        for account in platforms[:3]:
            lines.append("平台「" + scalar(account.get('platform_code')) + "」绑定记录状态为" + state(account.get('status')) + "。")
        partial = len(contacts) > 3 or len(platforms) > 3
    elif name == "read_task_context":
        for row in rows(data.get("handoffs"))[:3]:
            lines.append("人工处理记录的状态为" + state(row.get("status")) + "；这不是业务办理成功的凭据。")
    # Unknown shapes never fall back to printing keys, JSON, identifiers or
    # arbitrary nested values. Keep a truthful, explicit gap instead.
    if not lines:
        lines = ["本次记录暂时不足以给出可核实的具体答复，需要进一步核实。"]
    return lines, partial
