"""Deterministic commerce helpers; input evidence is not execution authority."""
from datetime import datetime, timezone

from app.tools.commerce_models import (
    Calculation, CouponCheck, HistorySummary, OfferComparison, PolicyCheck,
)


def now():
    return datetime.now(timezone.utc)


def failure(code, message, **details):
    return {'success': False, 'error_code': code, 'error': message, **details}


def success(**data):
    return {'success': True, 'source': 'calculation', 'observed_at': now().isoformat(),
            'input_basis': 'supplied_evidence', **data}


def coupon_checks(q: CouponCheck, at):
    c, r = q.coupon, q.coupon.rule
    eligible_subtotal = sum(i.unit_price_minor * i.quantity for i in q.items
                            if not r.sku_ids or i.sku_id in r.sku_ids)
    checks = {
        'owned': c.owned,
        'available': c.status == 'available',
        'time': r.starts_at <= at < r.ends_at,
        'channel': q.channel == r.channel,
        'currency': q.currency == r.currency,
        'sku': eligible_subtotal > 0,
        'minimum': eligible_subtotal >= r.minimum_minor,
        'stacking': not r.exclusive_group or r.exclusive_group not in q.used_exclusive_groups,
    }
    return checks


def check_coupon_eligibility(q: CouponCheck):
    checks = coupon_checks(q, now())
    return success(eligible=all(checks.values()), checks=checks,
                   failure_reasons=[k for k, ok in checks.items() if not ok],
                   coupon_id=q.coupon.coupon_id, rule_version=q.coupon.rule.version)


def compare_offers(q: OfferComparison):
    at = now()
    # Every candidate must describe the same commercial object and conditions.
    fields = ('sku_id', 'specification', 'condition', 'included_items',
              'quantity', 'currency', 'region', 'eligibility_basis')
    basis = tuple(getattr(q.offers[0], k) for k in fields)
    accepted, excluded = [], []
    if len({o.offer_id for o in q.offers}) != len(q.offers):
        return failure('validation_error', '报价 ID 重复')
    for offer in q.offers:
        reason = None
        if tuple(getattr(offer, k) for k in fields) != basis:
            reason = 'incomparable_conditions'
        elif not offer.observed_at <= at < offer.valid_until:
            reason = 'stale_or_future'
        elif offer.eligibility != 'eligible':
            reason = 'eligibility_' + offer.eligibility
        if reason:
            excluded.append({'offer_id': offer.offer_id, 'reason': reason})
        else:
            accepted.append(offer)
    if len(accepted) < 2:
        return failure('evidence_unavailable', '不足两个可比且有效的报价', excluded=excluded)
    accepted.sort(key=lambda o: (o.total_minor, o.offer_id))
    cheapest = accepted[0].total_minor
    return success(currency=accepted[0].currency,
                   scope='仅比较所提供且通过核验的报价', excluded=excluded,
                   rankings=[{**o.model_dump(mode='json'), 'difference_minor': o.total_minor - cheapest}
                             for o in accepted])


def summarize_price_history(q: HistorySummary):
    h = q.history
    if h.end > now() or any(p.observed_at > now() for p in h.points):
        return failure('validation_error', '历史价格不能包含未来记录')
    if not h.points:
        return failure('evidence_unavailable', '查询区间内没有价格记录')
    points = sorted(h.points, key=lambda p: p.observed_at)
    by_time = {}
    for p in points:
        if p.observed_at in by_time and by_time[p.observed_at] != p.amount_minor:
            return failure('evidence_conflict', '同一时刻的历史价格相互冲突')
        by_time[p.observed_at] = p.amount_minor
    lowest = min(p.amount_minor for p in points)
    return success(sku_id=h.sku_id, currency=h.currency, price_type=h.price_type,
                   lowest_observed_minor=lowest,
                   lowest_at=sorted({p.observed_at.isoformat() for p in points if p.amount_minor == lowest}),
                   first_observed_at=points[0].observed_at.isoformat(),
                   last_observed_at=points[-1].observed_at.isoformat(),
                   sample_count=len(by_time), coverage_complete=h.coverage_complete,
                   change_minor=points[-1].amount_minor - points[0].amount_minor,
                   limitation='仅代表已观测样本，不预测未来价格')


def validate_policy_evidence(q: PolicyCheck):
    at = now()
    valid, rejected = [], []
    for p in q.policies:
        if (p.topic, p.channel, p.category) != (q.topic, q.channel, q.category):
            rejected.append({'policy_id': p.policy_id, 'reason': 'not_applicable'})
        elif not p.effective_from <= at < p.effective_until or p.observed_at > at:
            rejected.append({'policy_id': p.policy_id, 'reason': 'not_effective'})
        else:
            valid.append(p)
    if not valid:
        return failure('evidence_unavailable', '没有适用且有效的政策依据', rejected=rejected)
    # Different clauses may coexist; competing versions/text of one clause may not.
    by_id = {}
    for p in valid:
        value = (p.version, p.text)
        if p.policy_id in by_id and by_id[p.policy_id] != value:
            return failure('evidence_conflict', '同一政策存在多个有效版本或矛盾文本')
        by_id[p.policy_id] = value
    return success(policies=[p.model_dump(mode='json') for p in valid], rejected=rejected)


def calc_promotion(q: Calculation):
    """Apply a selected rule plan in declared order; never optimize or invent eligibility.

    Fixed/percentage reductions use eligible original or remaining item amounts.
    Discounts are allocated with largest remainders and SKU ties, so split rules
    cannot consume discounts from ineligible items or create rounding drift.
    """
    at = now()
    original = {i.sku_id: i.unit_price_minor * i.quantity for i in q.items}
    remaining = dict(original)
    groups, applied = set(), []
    for rule in sorted(q.rules, key=lambda r: (r.priority, r.rule_id)):
        if q.eligibility.get(rule.rule_id) != 'eligible':
            return failure('evidence_unavailable', '所选方案存在未核验或不满足的优惠资格', rule_id=rule.rule_id)
        if rule.currency != q.currency or rule.channel != q.channel:
            return failure('validation_error', '优惠规则币种或渠道不匹配', rule_id=rule.rule_id)
        if not rule.starts_at <= at < rule.ends_at or rule.observed_at > at:
            return failure('business_rejection', '所选活动未生效或已过期', rule_id=rule.rule_id)
        if rule.exclusive_group and rule.exclusive_group in groups:
            return failure('business_rejection', '所选优惠不可叠加', rule_id=rule.rule_id)
        skus = sorted(s for s in original if not rule.sku_ids or s in rule.sku_ids)
        basis = original if rule.base == 'original' else remaining
        subtotal = sum(basis[s] for s in skus)
        available = sum(remaining[s] for s in skus)
        if not skus or subtotal < rule.minimum_minor:
            return failure('business_rejection', '适用商品或门槛不满足', rule_id=rule.rule_id)
        amount = rule.amount_minor if rule.kind == 'fixed' else subtotal * rule.discount_bps // 10000
        amount = min(amount, available, rule.cap_minor if rule.cap_minor is not None else amount)
        if available:
            allocation = {s: amount * remaining[s] // available for s in skus}
            residue = amount - sum(allocation.values())
            ranked = sorted(skus, key=lambda s: (-(amount * remaining[s] % available), s))
            for s in ranked[:residue]:
                allocation[s] += 1
            for s in skus:
                remaining[s] -= allocation[s]
        if rule.exclusive_group:
            groups.add(rule.exclusive_group)
        applied.append({'rule_id': rule.rule_id, 'version': rule.version, 'discount_minor': amount})
    goods = sum(original.values())
    discount = goods - sum(remaining.values())
    return success(currency=q.currency, goods_minor=goods, discount_minor=discount,
                   shipping_minor=q.shipping_minor, fees_minor=q.fees_minor,
                   total_minor=goods - discount + q.shipping_minor + q.fees_minor,
                   applied_rules=applied, rounding='percentage_floor_then_largest_remainder',
                   status='estimate', note='仅计算输入固定方案，不锁价、不领券、不下单')
