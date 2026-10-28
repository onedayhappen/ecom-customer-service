"""Local checks for critical claims in composed replies, using validated receipts.

This is deliberately a bounded business guard, not a general semantic judge.
Neither conversation text nor task status can authorize a business assertion.
No I/O, model calls, or recursive searches through arbitrary tool prose.
"""
import re
from decimal import Decimal, InvalidOperation


STATES = {
    'paid': {'paid'}, '已支付': {'paid'},
    'shipped': {'shipped'}, '已发货': {'shipped'},
    'pending_receipt': {'shipped'}, 'in_transit': {'shipped', 'in_transit'},
    '运输中': {'shipped', 'in_transit'}, 'out_for_delivery': {'shipped'},
    'delivered': {'shipped', 'delivered'}, '已签收': {'shipped', 'delivered'},
    'received': {'shipped', 'delivered'}, '已收货': {'shipped', 'delivered'},
    'cancelled': {'cancelled'}, '已取消': {'cancelled'},
    'not_shipped': {'unshipped'}, '未发货': {'unshipped'},
}
CLAIMS = {
    'unshipped': re.compile(r'(?:还|尚|并|暂)?(?:没有|没|未)发货'),
    'shipped': re.compile(r'(?:已经|已|都已|确实|已经全部)发货'),
    'delivered': re.compile(r'(?:已经|已|都已)(?:签收|收货|送达)'),
    'in_transit': re.compile(r'(?:还在|正在|仍在|状态[是为：:]?)?运输(?:中|途中)'),
    'paid': re.compile(r'(?:已经|已)(?:支付|付款)'),
    'cancelled': re.compile(r'(?:已经|已)(?:被)?取消'),
    'refund_succeeded': re.compile(r'(?:已经|已)(?:为您|给您|帮您)?退款|退款(?:已)?成功|(?:已经|已)退回'),
}
MONEY = re.compile(r'[¥￥]\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)|(\d+(?:,\d{3})*(?:\.\d{1,2})?)\s*元')
UNCERTAIN = re.compile(r'无法(?:确认|确定|判断|核实)|不能(?:确认|确定|判断|据此)|不(?:能|代表|等于|意味着|一定)|未必|尚待核实|是否|能否|有没有')
ORDER_ID = re.compile(r'订单(?:号)?\s*[:：]?\s*([a-zA-Z0-9][a-zA-Z0-9_-]{2,})')
IDENTITY = re.compile(r'(?:鞋盒|包裹|快递).{0,60}(?:不是|就是|并非|不属于|另一个|别的|其他订单)|(?:不是|就是|并非).{0,20}(?:它|这双|这件|这单)')
PHYSICAL_BOX = re.compile(r'家人|家里人|收到的|拿到的|拿过|代收|鞋盒')
BANK = re.compile(r'(?:已|已经|成功|肯定|确定).{0,8}(?:到账|入账)|(?:退|打|转)(?:到|回|入).{0,8}(?:银行卡|账户)|(?:到账|入账)(?:了|成功)')
ETA = re.compile(r'(?:今天|明天|后天|周[一二三四五六日天]|\d{4}-\d{2}-\d{2}|\d+月\d+日|\d+号).{0,14}(?:能到|会到|送到|送达|到货|收到)|(?:保证|肯定|一定|确保).{0,10}(?:送达|到货|收到)')
CORRECTIONS = {
    'unshipped': '目前的记录不足以确认是否已经发货；没有物流记录不代表实际未发货。',
    'parcel_identity': '仅凭物流记录，无法确认家人收到的包裹是否就是这件商品。',
    'bank_credit': '渠道退款记录不代表银行卡已入账，实际到账情况仍需核实。',
    'delivery_promise': '现有证据不足以确认具体送达时间。',
    'amount': '这项金额尚未得到对应订单或业务流水的证实，需要进一步核实。',
    'status': '现有证据不足以确认这项业务状态，需要进一步核实。',
}


def _amount(value):
    if type(value) not in (int, float, str) or len(str(value)) > 64:
        return None
    try:
        number = Decimal(str(value).replace(',', ''))
        return str(number.normalize()) if number.is_finite() and 0 <= number <= 1e9 else None
    except (InvalidOperation, ValueError):
        return None


def _rows(value, limit=50):
    return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []


def _order_ref(value):
    # References come from already validated tool receipts, never from model prose.
    from app.services.commerce.common import BusinessError, unref
    try:
        kind, platform, oid = unref(value, {'order'})
        return platform + ':' + oid, oid
    except (BusinessError, TypeError, ValueError):
        return None, None


def evidence_index(receipts, details):
    """Compact, JSON-serializable index included in the publication fingerprint.

    Only typed current records authorize states/amounts. Descriptions, historical
    events, source_status and status_before_after_sale are not current state.
    """
    from app.tasks.reply_search_grounding import observation
    objects, searches = {}, []

    def obj(row=None, fallback=None):
        row = row or {}
        oid = row.get('order_id')
        key = (str(row.get('platform_code', '')) + ':' + str(oid)) if oid else fallback
        if not key:
            return None
        if key not in objects:
            objects[key] = dict(key=key, aliases=[str(oid or key.split(':')[-1])],
                                states=[], amounts={}, estimates=[])
        return objects[key]

    def state(item, value):
        if item is not None and isinstance(value, str):
            item['states'] = sorted(set(item['states']) | STATES.get(value, set()))

    def amount(item, kind, value, currency='CNY'):
        value = _amount(value)
        if item is not None and value is not None and currency == 'CNY':
            values = item['amounts'].setdefault(kind, [])
            if value not in values:
                values.append(value)

    for receipt in receipts[:30]:
        search = observation(receipt)
        if search is not None:
            searches.append(search)
        result, tool = receipt.get('result', {}), receipt.get('tool')
        if result.get('success') is not True:
            continue
        data, args = result.get('data', {}), receipt.get('arguments', {})
        if not isinstance(data, dict):
            continue
        fallback, _ = _order_ref(args.get('target_ref') or args.get('order_ref') or data.get('order_ref'))
        if not fallback and (args.get('order_id') or details.get('order_id')):
            fallback = str(details.get('platform_code', '')) + ':' + str(args.get('order_id') or details['order_id'])
        if tool in {'get_order', 'find_orders', 'query_order'}:
            rows = data.get('items', []) if tool == 'find_orders' else [result.get('order', {}) if tool == 'query_order' else data]
            for row in _rows(rows):
                item = obj(row)
                if item is None:
                    continue
                state(item, row.get('status'))
                for key, kind in [('total', 'order'), ('payable_amount', 'order'), ('paid_amount', 'paid'),
                                  ('goods_amount', 'goods'), ('shipping_amount', 'shipping')]:
                    amount(item, kind, row.get(key), row.get('currency', 'CNY'))
                for product in _rows(row.get('purchase_summary') or row.get('items'), 20):
                    name = product.get('name') or product.get('title_snapshot')
                    if isinstance(name, str) and name:
                        item['aliases'].append(name[:200])
        elif tool in {'get_logistics', 'get_shipments', 'get_shipment_tracking', 'query_logistics'}:
            rows = ([result.get('logistics', {})] if tool == 'query_logistics' else
                    [data['shipment']] if isinstance(data.get('shipment'), dict) else data.get('items', []))
            for row in _rows(rows):
                item = obj(row, fallback)
                state(item, row.get('status'))
                estimate = row.get('estimated_delivery')
                if item is not None and isinstance(estimate, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', estimate):
                    item['estimates'].extend([estimate, f'{int(estimate[5:7])}月{int(estimate[8:10])}日'])
            obj(fallback=fallback)  # An empty lookup adds no positive or negative fact.
        elif tool in {'get_refunds', 'get_after_sale_status'}:
            sale = data.get('after_sale') or {}
            item = obj(sale, fallback)
            amount(item, 'requested', sale.get('requested_amount'), sale.get('currency', 'CNY'))
            if tool == 'get_refunds':
                for row in _rows(data.get('items')):
                    target = obj(row, fallback)
                    if target is not None and row.get('status') == 'succeeded':
                        target['states'].append('refund_succeeded')
                        amount(target, 'refund', row.get('amount'), row.get('currency', 'CNY'))
            # Currency and channel outcome remain separate from requested amounts.
            totals = data.get('refund_summary', data.get('amounts_by_currency_and_status', {}))
            total = _amount(totals.get('CNY', {}).get('succeeded'))
            if item is not None and total is not None and Decimal(total) > 0:
                item['states'].append('refund_succeeded')
                amount(item, 'refund', total)
        elif tool == 'get_payments':
            item = obj(fallback=fallback)
            total = data.get('recorded_success_amounts', {}).get('CNY')
            amount(item, 'paid', total)
            if item is not None and _amount(total) is not None and Decimal(_amount(total)) > 0:
                item['states'].append('paid')
        elif tool in {'search_products', 'get_product_details', 'query_product'}:
            rows = (data.get('items', []) if tool == 'search_products' else
                    [result.get('product', {})] if tool == 'query_product' else [data.get('product', {})])
            for row in _rows(rows):
                ident = row.get('product_id') or row.get('id')
                if not ident:
                    continue
                item = obj(fallback='product:' + str(ident))
                if row.get('name'):
                    item['aliases'].append(str(row['name'])[:200])
                prices = data.get('items', []) if tool == 'get_product_details' else row.get('sku_summary', [row])
                for sku in _rows(prices):
                    amount(item, 'order', sku.get('list_price', sku.get('price')), sku.get('currency', 'CNY'))
    for item in objects.values():
        if details.get('order_id') in item['aliases'] or len(objects) == 1:
            name = details.get('item_name')
            if isinstance(name, str) and name:
                item['aliases'].append(name[:200])
        item['aliases'] = sorted(set(item['aliases']))
        item['states'] = sorted(set(item['states']))
    return list(objects.values()) + searches


def _qualified(clause, match):
    """Qualifiers apply to this clause, not an earlier sentence's disclaimer."""
    prefix = clause[:match.start()]
    suffix = clause[match.end():]
    return bool(UNCERTAIN.search(prefix) or suffix.startswith(('吗', '么', '没有', '与否', '？', '?'))
                or (re.search(r'未查到|没有查到|没有查询到|未记录', prefix) and '记录' in suffix))


def check_reply(text, evidence):
    """Replace unsupported critical sentences and return auditable reason codes.

    Unknown/ambiguous object scope cannot borrow another order's state or amount.
    Fixed confirmation/clarification notices are appended by the caller afterwards.
    """
    from app.tasks.reply_search_grounding import SearchGuard
    search_guard = SearchGuard(evidence)
    objects = [item for group in evidence for item in group if item.get('kind') != 'product_search']
    by_id, by_name = {}, {}
    for item in objects:
        ident = item['key'].split(':')[-1]
        by_id.setdefault(ident, []).append(item)
        for name in item['aliases']:
            if name != ident:
                by_name.setdefault(name, []).append(item)
    # Scan each clause once for known identifiers/names instead of compiling and
    # running one identifier regex per object per clause in a long merged reply.
    identifiers = re.compile(r'(?<![a-zA-Z0-9_-])(?:' + '|'.join(re.escape(k) for k in sorted(by_id, key=len, reverse=True))
                             + r')(?![a-zA-Z0-9_-])') if by_id else None
    names = re.compile('|'.join(re.escape(k) for k in sorted(by_name, key=len, reverse=True))) if by_name else None
    scope = objects
    reasons, output, search_corrections = [], [], set()
    for sentence in re.findall(r'[^。！？\n]+[。！？]*|[。！？\n]+', text):
        if sentence.isspace():
            scope = objects
            output.append(sentence)
            continue
        issues, search_groups = [], []
        for clause in re.split(r'[，,；;]|但是|不过|然而|而是', sentence):
            search_issues, groups = search_guard.check(clause)
            issues.extend(search_issues)
            for group in groups if search_issues else []:
                if group not in search_groups:
                    search_groups.append(group)
            # Order IDs take precedence over product names shared by several orders.
            explicit = [item for m in identifiers.finditer(clause) for item in by_id[m[0]]] if identifiers else []
            named = explicit or ([item for m in names.finditer(clause) for item in by_name[m[0]]] if names else [])
            if any(m[1] not in by_id for m in ORDER_ID.finditer(clause)):
                scope = []
            elif named:
                scope = named
            for pattern, code in ((BANK, 'bank_credit'), (ETA, 'delivery_promise')):
                for match in pattern.finditer(clause):
                    if not _qualified(clause, match) and (code != 'bank_credit' or re.search(r'退款|银行卡|银行账户|到账', sentence)):
                        if (code == 'delivery_promise' and scope and re.search(r'预计|预估', clause)
                                and not re.search(r'保证|肯定|一定|确保', clause)
                                and all(any(date in clause for date in item['estimates']) for item in scope)):
                            continue
                        # A scheduled/estimated date isn't a guaranteed delivery time.
                        issues.append(code)
            if PHYSICAL_BOX.search(sentence) and IDENTITY.search(clause) and not UNCERTAIN.search(clause):
                issues.append('parcel_identity')
            for kind, pattern in CLAIMS.items():
                for match in pattern.finditer(clause):
                    if _qualified(clause, match):
                        continue
                    if kind == 'cancelled' and re.search(r'事项|任务|自动处理', clause) and '订单' not in clause:
                        continue
                    if kind == 'unshipped' and clause[match.end():].startswith('记录'):
                        continue
                    if not scope or not all(kind in item['states'] for item in scope):
                        issues.append('unshipped' if kind == 'unshipped' else 'status')
            for match in MONEY.finditer(clause):
                if _qualified(clause, match) or re.search(r'预算|希望|愿意|假设', clause):
                    continue
                kind = ('requested' if re.search(r'申请.{0,4}(?:退款|金额)|申请金额', clause) else
                        'refund' if re.search(r'退款|退回|已退', clause) else
                        'shipping' if '运费' in clause else 'goods' if '商品金额' in clause else
                        'paid' if re.search(r'实付|已付|支付|付款', clause) else 'order')
                value = _amount(match.group(1) or match.group(2))
                if not scope or not all(value in item['amounts'].get(kind, []) for item in scope):
                    issues.append('amount')
        if issues:
            unique = list(dict.fromkeys(issues))
            reasons.extend(unique)
            correction = ''
            if any(code.startswith('product_') for code in unique):
                candidate = search_guard.correction(search_groups)
                if candidate not in search_corrections:
                    correction = candidate
                    search_corrections.add(candidate)
            output.append(''.join(CORRECTIONS[code] for code in unique if code in CORRECTIONS) + correction)
        else:
            output.append(sentence)
    return ''.join(output), sorted(set(reasons))
