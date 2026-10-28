"""Bounded customer-facing projections of the core tool contract."""
from app.tasks.reply_facts import label

STATES={'pending_payment':'待支付','paid':'已支付','fulfilling':'履约中','shipped':'已发货',
    'received':'已收货','after_sale':'售后中','pending_receipt':'待收货',
    'in_transit':'运输中','delivered':'已签收','completed':'已完成','cancelled':'已取消','closed':'已关闭',
    'pending_review':'待审核','approved':'已批准','rejected':'已拒绝','processing':'处理中','pending':'待处理',
    'succeeded':'记录成功','failed':'记录失败','available':'可用','reserved':'已占用','used':'已使用',
    'expired':'已过期','invalid':'已失效','unknown':'未知','eligible':'符合已核实规则',
    'ineligible':'不符合规则','active':'有效','inactive':'停用','queued':'排队中','in_progress':'处理中',
    'waiting_customer':'等待用户','personal':'个人','company':'企业','refund_only':'仅退款','return_refund':'退货退款',
    'exchange':'换货','repair':'维修','price_protection':'价格保护','registered':'已登记','accepted':'处理方已受理',
    'issued':'已开票','voided':'已作废','pass':'通过','fail':'不通过'}


def restriction(value):
    """Translate known legacy source wording without clipping its conditions."""
    translations = {
        '会员券配置为planned时尚未开放领取，不能视为已持有或可使用的优惠券':
        '会员券处于筹备状态时尚未开放领取，不能视为已持有或可使用的优惠券',
    }
    return label(translations.get(value, value), len(value) + 30)

def render(name, result, *, query="", include_context=True):
    """Render bounded business sentences; never recursively enumerate fields."""
    from app.tasks.customer_facts import amount, describe, rows, scalar
    if not result.get('success'):
        return []
    data = result.get('data', {})
    if not isinstance(data, dict):
        return []
    if data.get('next_state') == 'needs_confirmation':
        return ['已准备办理内容，请查看确认卡片；尚未执行。']
    lines = []
    conditions = data.get('applied_conditions')
    if include_context and name == 'search_products' and isinstance(conditions, dict):
        lines.append('按「' + scalar(conditions.get('query'), 200) + '」查询')
        cap = conditions.get('max_price')
        if isinstance(cap, dict) and (price := amount(cap.get('amount'), cap.get('currency'))):
            lines.append('本次筛选价格不超过' + price + '。')
        filters = [scalar(s.get('name')) + '为' + scalar(s.get('value')) for s in rows(conditions.get('specs'))
                   if scalar(s.get('name')) and scalar(s.get('value'))]
        if filters:
            lines.append('本次要求' + '、'.join(filters) + '；是否满足仍以匹配记录为准。')
    sentences, partial = describe(name, data, query)
    lines.extend(sentences)
    limitations = result.get('limitations') or []
    if include_context and isinstance(limitations, list):
        # Preserve complete constraints rather than clipping a negation.
        lines.extend(restriction(v) for v in limitations if isinstance(v, str))
    if result.get('next_cursor') or data.get('has_more') or data.get('truncated') or partial:
        lines.append('以上为部分结果，可以继续查询其他记录。')
    return list(dict.fromkeys(lines))
