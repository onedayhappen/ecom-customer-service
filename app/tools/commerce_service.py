"""Commerce tools backed by the configured local MySQL business database."""
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

from app.config.settings import settings
from app.data.commerce_dao import CommerceRejection, MySqlCommerceDAO
from app.tools.commerce_models import (
    AfterSaleRecord, Coupon, DeliveryPolicy, Discount, History, Offer,
    OrderPolicy, PaymentRecord,
)
from app.tools.commerce_calculations import now
from app.tools.request_context import get_current_user_id


@lru_cache(maxsize=4)
def _dao(dsn):
    return MySqlCommerceDAO(dsn)


def database():
    if not settings.order_db_dsn:
        raise CommerceRejection('未配置 MySQL 数据源 ORDER_DB_DSN', 'evidence_unavailable')
    return _dao(settings.order_db_dsn)


def user():
    identity = get_current_user_id()
    if not identity:
        raise CommerceRejection('需要已验证的用户身份')
    return identity


def response(rows=(), **data):
    demo = any(r.get('is_demo') for r in rows)
    return {'success': True, 'source': 'mysql_demo' if demo else 'mysql',
            'is_demo': demo, 'observed_at': now().isoformat(), **data}


def required(rows, message):
    if not rows:
        raise CommerceRejection(message, 'evidence_unavailable')
    return rows


def current_policy(payload):
    start, end = (datetime.fromisoformat(payload[k]) for k in ('effective_from', 'effective_until'))
    if start.utcoffset() is None or end.utcoffset() is None or not start <= now() < end:
        raise CommerceRejection('本地政策尚未生效或已过期', 'evidence_unavailable')
    if datetime.fromisoformat(payload['observed_at']) > now():
        raise CommerceRejection('政策观测时间在未来', 'evidence_conflict')


def query_offers(q):
    rows, offers = [], []
    for channel in dict.fromkeys(q.channels):
        for row in database().records('offers', object_id=q.sku_id, channel=channel, region=q.region):
            offer = Offer.model_validate(row['payload'])
            if (offer.sku_id, offer.channel, offer.region, offer.quantity) != (q.sku_id, channel, q.region, q.quantity):
                continue
            if offer.observed_at <= now() < offer.valid_until:
                rows.append(row)
                offers.append(offer.model_dump(mode='json'))
    required(rows, '未找到条件相符且有效的报价；没有使用目录标价替代')
    return response(rows, offers=offers, missing_channels=[c for c in q.channels if c not in {o['channel'] for o in offers}])


def query_promotions(q):
    rows, rules = [], []
    for row in database().records('promotions', channel=q.channel):
        rule = Discount.model_validate(row['payload'])
        if rule.channel == q.channel and (not rule.sku_ids or set(q.sku_ids) & set(rule.sku_ids)) and rule.starts_at <= now() < rule.ends_at:
            rows.append(row)
            rules.append(rule.model_dump(mode='json'))
    return response(rows, rules=rules)


def query_user_coupons(q):
    identity = user()
    rows = database().records('coupons', user_id=identity, record_id=q.coupon_id, channel=q.channel)
    coupons = [Coupon.model_validate(r['payload']).model_dump(mode='json') for r in rows]
    return response(rows, coupons=coupons)


def query_delivery_quote(q):
    rows = required(database().records('delivery', channel=q.channel, region=q.region), '缺少该地区配送报价')
    for row in rows:
        row['payload'] = DeliveryPolicy.model_validate(row['payload']).model_dump(mode='json')
    matches = [r for r in rows if set(i.sku_id for i in q.items) <= set(r['payload']['sku_ids'])]
    required(matches, '没有覆盖全部商品的配送报价')
    if len(matches) != 1:
        raise CommerceRejection('存在多个适用配送报价', 'evidence_conflict')
    p = matches[0]['payload']
    current_policy(p)
    if p['deliverable'] is not True:
        return response(matches, deliverable=False, reason=p['reason'])
    # This v1 local policy only supports fixed + per-unit freight, not guessed weights.
    if p['fee_mode'] != 'fixed_plus_unit':
        raise CommerceRejection('不支持该配送计费规则', 'evidence_unavailable')
    for k in ('base_minor', 'per_unit_minor', 'min_days', 'max_days'):
        if type(p[k]) is not int or p[k] < 0:
            raise ValueError('配送规则包含无效数值')
    if p['min_days'] > p['max_days']:
        raise ValueError('配送时效区间无效')
    amount = p['base_minor'] + p['per_unit_minor'] * sum(i.quantity for i in q.items)
    return response(matches, deliverable=True, currency=p['currency'], shipping_minor=amount,
                    estimated_days={'min': p['min_days'], 'max': p['max_days'], 'starts_at': 'dispatch'},
                    policy_version=p['version'], valid_until=p['effective_until'])


def query_price_history(q):
    if q.end > now():
        raise CommerceRejection('历史查询不能包含未来时段', 'validation_error')
    rows = database().records('history', object_id=q.sku_id, channel=q.channel, start=q.start, end=q.end)
    rows = [r for r in rows if r['payload']['price_type'] == q.price_type]
    required(rows, '指定区间没有该口径的历史价格')
    pairs = {(r['payload']['specification'], r['payload']['currency']) for r in rows}
    if len(pairs) != 1:
        raise CommerceRejection('历史记录的规格或币种不一致', 'evidence_conflict')
    specification, currency = pairs.pop()
    history = History.model_validate({
        'source': 'mysql_demo' if any(r['is_demo'] for r in rows) else 'mysql',
        'observed_at': now(), 'sku_id': q.sku_id, 'specification': specification,
        'currency': currency, 'channel': q.channel, 'price_type': q.price_type,
        'start': q.start, 'end': q.end, 'coverage_complete': False,
        'points': [r['payload']['point'] for r in rows],
    })
    return response(rows, history=history.model_dump(mode='json'))


def query_after_sale(q):
    identity, db = user(), database()
    order = db.owned_order(identity, q.order_id)
    rows = db.records('after_sales', user_id=identity, object_id=q.order_id, record_id=q.after_sale_id)
    for row in rows:
        row['payload'] = AfterSaleRecord.model_validate(row['payload']).model_dump(mode='json')
        if row['payload']['order_id'] != q.order_id:
            raise ValueError('售后记录关联订单不一致')
    if q.kind:
        rows = [r for r in rows if r['payload']['kind'] == q.kind]
    # Existing apply_refund writes the original order table. Return that live
    # state separately from imported historical after-sale snapshots.
    current_refund = None
    if order.get('refund_status') and q.kind in (None, 'refund', 'return'):
        current_refund = {'source': 'mysql_order_fields', 'status': order['refund_status'],
                          'requested_at': str(order.get('refund_requested_at') or ''),
                          'note': '订单退款字段，不表示资金已到账'}
    return response(rows, records=[r['payload'] for r in rows], current_order_refund=current_refund)


def query_payment_status(q):
    identity, db = user(), database()
    db.owned_order(identity, q.order_id)
    rows = db.records('payments', user_id=identity, object_id=q.order_id)
    required(rows, '未找到支付交易记录；订单状态不等于支付通道事实')
    for row in rows:
        row['payload'] = PaymentRecord.model_validate(row['payload']).model_dump(mode='json')
        if row['payload']['order_id'] != q.order_id:
            raise ValueError('支付记录关联订单不一致')
    return response(rows, payments=[r['payload'] for r in rows])


def invoice_eligibility(snapshot):
    order, policy, invoices, version = snapshot
    p = OrderPolicy.model_validate(policy['payload']).model_dump(mode='json')
    current_policy(p)
    eligible = (order['status'] in p['allowed_statuses'] and not order.get('refund_status'))
    amount = Decimal(str(order['total'])) * 100
    if amount < 0 or amount != amount.to_integral_value():
        raise ValueError('订单金额无法转换为整数分')
    return eligible, int(amount), version


def query_invoice(q):
    identity, db = user(), database()
    db.owned_order(identity, q.order_id)
    if q.mode == 'lookup':
        rows = db.records('invoices', user_id=identity, object_id=q.order_id, record_id=q.invoice_id)
        return response(rows, invoices=[r['payload'] for r in rows])
    snapshot = db.preview_invoice(identity, q.order_id)
    eligible, amount, version = invoice_eligibility(snapshot)
    if q.invoice_id and not any(r['record_id'] == q.invoice_id for r in snapshot[2]):
        raise CommerceRejection('未找到订单关联发票', 'evidence_unavailable')
    fields = ['title', 'invoice_type']
    if q.fields and q.fields.invoice_type != 'personal':
        fields.append('tax_id')
    return response([snapshot[1], *snapshot[2]], can_issue=eligible,
                    reason=None if eligible else '订单状态或退款状态不满足本地开票政策',
                    amount_minor=amount, currency='CNY', preview_version=version,
                    required_fields=fields, conditional_fields={'company_or_vat_special': ['tax_id']},
                    existing_invoices=[r['payload'] for r in snapshot[2]],
                    limitation='本地发票台账，仅模拟办理，不连接税务系统')


def write_invoice(q, action, request_id):
    identity = user()
    def create(snapshot):
        eligible, amount, _ = invoice_eligibility(snapshot)
        if not eligible:
            raise CommerceRejection('订单当前不可开票')
        active = [r for r in snapshot[2] if r['payload']['status'] == 'issued_local']
        original = getattr(q, 'invoice_id', None)
        if action == 'issue_invoice' and active:
            raise CommerceRejection('已有有效本地发票，请查询原票或明确请求重开')
        if action == 'reissue_invoice' and not any(r['record_id'] == original for r in active):
            raise CommerceRejection('原票不存在、已重开或不属于该订单')
        return {'order_id': q.order_id, 'fields': q.fields.model_dump(),
                'amount_minor': amount, 'currency': 'CNY', 'status': 'issued_local',
                'original_invoice_id': original, 'issued_at': now().isoformat(),
                'is_demo': True, 'source': 'mysql_demo'}, original
    return database().write_invoice(identity, action, q.model_dump(mode='json'), request_id, create)


def evaluate_order_action(q):
    order = database().owned_order(user(), q.order_id)
    rows = required(database().records('policies', record_id=q.action), '缺少该动作的本地业务规则')
    p = OrderPolicy.model_validate(rows[0]['payload']).model_dump(mode='json')
    current_policy(p)
    allowed = order['status'] in p['allowed_statuses'] and not order.get('refund_status')
    from app.data.commerce_dao import fingerprint
    return response(rows, can_act=allowed,
                    reason=None if allowed else '当前订单状态不符合本地政策',
                    order_version=fingerprint(order), policy_version=p['version'],
                    valid_until=p['effective_until'], action=q.action,
                    fulfillment_channel=('carrier' if order['status'] == 'shipped' else 'merchant')
                    if q.action == 'expedite' else None,
                    proposal_validated=False, note='仅核验当前状态；地址可达性和费用需另行校验，写入时重新核验')


def query_handoff_status(q):
    from app.support.runtime import current_scope
    scope = current_scope()
    if not scope or scope[2] != user():
        raise CommerceRejection('需要当前用户的已验证会话上下文')
    state = scope[0].state(scope[1], scope[2])
    return {'success': True, 'source': 'support_store', 'observed_at': now().isoformat(),
            'mode': state['mode'], 'ticket': state['ticket']}


def query_mutation_status(q):
    identity = user()
    if q.request_id:
        result = database().mutation_status(identity, q.request_id)
        return response([{'is_demo': bool((result.get('result') or {}).get('is_demo'))}], **result)
    from app.support.runtime import current_scope
    scope = current_scope()
    if not scope or scope[2] != identity:
        raise CommerceRejection('需要当前用户的已验证会话上下文')
    return {'success': True, 'source': 'support_store', 'observed_at': now().isoformat(),
            **scope[0].operation_status(scope[1], identity, q.operation_id)}
