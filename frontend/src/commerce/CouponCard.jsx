import { Ticket } from 'lucide-react'
import { formatMoney as money, formatDateTime as date } from '../shared/format'
import { couponLabels, couponTypeLabels } from './constants'

export default function CouponCard({ coupon }) {
  const rule = coupon.template
  const amount =
    rule.kind === 'amount_off'
      ? money(rule.discount_amount, rule.currency)
      : rule.kind === 'percent_off' && rule.discount_bps != null
        ? `${Number(((10000 - rule.discount_bps) / 1000).toFixed(2))} 折`
        : '优惠券'
  return (
    <article className={`coupon-card coupon-${coupon.status}`}>
      <div className="coupon-value">
        <Ticket size={22} />
        <strong>{amount}</strong>
        <span>
          {rule.threshold_amount == null
            ? '使用门槛待确认'
            : Number(rule.threshold_amount) === 0
              ? '无门槛'
              : `满 ${money(rule.threshold_amount, rule.currency)} 可用`}
        </span>
      </div>
      <div className="coupon-details">
        <div className="coupon-title">
          <h3>{rule.name?.replace(/（模拟）/g, '').trim() || '优惠券'}</h3>
          <span className={`commerce-badge status-${coupon.status}`}>
            {couponLabels[coupon.status] || '状态待确认'}
          </span>
        </div>
        <p>
          {couponTypeLabels[coupon.coupon_type] || '通用券'} · {coupon.scope_description}
        </p>
        {rule.cap_amount != null && <p>最高优惠 {money(rule.cap_amount, rule.currency)}</p>}
        <small>有效期至 {date(coupon.expires_at)}</small>
        {coupon.rules_basis === 'current_template' && <small>按当前优惠规则展示</small>}
      </div>
    </article>
  )
}
