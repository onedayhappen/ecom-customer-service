import { Award, Crown, UserRound } from 'lucide-react'
import { formatMoney as money, formatDate as date } from './shared/format'

const statuses = {
  active: '生效中',
  expired: '已过期',
  scheduled: '未生效',
  suspended: '已暂停',
  unavailable: '暂不可用',
  cancelled: '已取消',
  inactive: '未启用',
  not_opened: '未开通',
  unknown: '待核实',
}
const plans = { monthly: '月度 VIP', annual: '年度 VIP' }
function Benefits({ member, facts }) {
  const benefits = (facts.benefit_definitions || []).filter(
    (b) => b.program_id === member.program_id,
  )
  if (!benefits.length) return <p className="membership-muted">暂无权益信息</p>
  return (
    <ul className="membership-benefits">
      {benefits.map((benefit) => {
        const rule = benefit.rules || {}
        const available = benefit.status === 'active' && benefit.membership_active
        const entitlement = (facts.benefit_entitlements || []).find(
          (e) =>
            e.program_id === benefit.program_id &&
            e.benefit_family === benefit.benefit_family &&
            e.effective_status === 'active',
        )
        const title =
          rule.threshold_amount != null && rule.discount_amount != null
            ? `满 ${money(rule.threshold_amount, rule.currency)} 减 ${money(rule.discount_amount, rule.currency)}`
            : '会员专属权益'
        return (
          <li key={`${benefit.benefit_id}:${benefit.version}`}>
            <strong>{title}</strong>
            <span>
              {benefit.period_type === 'month'
                ? '每月'
                : benefit.period_type === 'year'
                  ? '每年'
                  : '额度'}{' '}
              {benefit.quota} 份 ·{' '}
              {available ? '已开放' : benefit.status === 'planned' ? '暂未开放' : '当前不可用'}
            </span>
            {available && entitlement && (
              <small>
                本期已领取 {entitlement.claimed_count} 份，剩余 {entitlement.remaining_count} 份
              </small>
            )}
          </li>
        )
      })}
    </ul>
  )
}

function MembershipCard({ member, facts }) {
  return (
    <article className="membership-card">
      <header>
        <span>
          <Award size={19} />
          成长会员
        </span>
        <span className={`commerce-badge status-${member.effective_status}`}>
          {statuses[member.effective_status] || '待核实'}
        </span>
      </header>
      <h3>{member.name || '等级待确认'}</h3>
      <dl className="membership-facts">
        <div>
          <dt>成长值</dt>
          <dd>{member.growth_value ?? '未提供'}</dd>
        </div>
        <div>
          <dt>有效期</dt>
          <dd>
            {date(member.starts_at)} 至 {date(member.expires_at)}
          </dd>
        </div>
        {member.next_evaluate_at && (
          <div>
            <dt>下次评定</dt>
            <dd>{date(member.next_evaluate_at)}</dd>
          </div>
        )}
      </dl>
      <Benefits member={member} facts={facts} />
    </article>
  )
}

function MembershipGroup({ facts }) {
  const vip = facts.vip || { status: 'not_opened', records: [] }
  const sub = vip.subscription || (vip.status !== 'unknown' ? vip.subscription_history?.[0] : null)
  const vipMember = (vip.records || []).find((m) => m.membership_id === sub?.membership_id)
  return (
    <section className="membership-group" aria-label="会员信息">
      <h2>会员信息</h2>
      <div className="membership-grid">
        {facts.growth_membership ? (
          <MembershipCard member={facts.growth_membership} facts={facts} />
        ) : (
          <article className="membership-card">
            <header>
              <span>
                <Award size={19} />
                成长会员
              </span>
            </header>
            <h3>暂无成长会员记录</h3>
          </article>
        )}
        <article className="membership-card vip-card">
          <header>
            <span>
              <Crown size={19} />
              VIP 会员
            </span>
            <span className={`commerce-badge status-${vip.status}`}>
              {statuses[vip.status] || '待核实'}
            </span>
          </header>
          <h3>{sub ? plans[sub.plan_code] || 'VIP 订阅' : statuses[vip.status] || '待核实'}</h3>
          {sub && (
            <dl className="membership-facts">
              <div>
                <dt>有效期</dt>
                <dd>
                  {date(sub.starts_at)} 至 {date(sub.ends_at)}
                </dd>
              </div>
            </dl>
          )}
          {vip.status === 'active' && vipMember ? (
            <Benefits member={vipMember} facts={facts} />
          ) : (
            <p className="membership-muted">
              {vip.status === 'unknown'
                ? '会员状态需要核实，请咨询客服。'
                : '当前没有生效中的 VIP 权益。'}
            </p>
          )}
        </article>
      </div>
    </section>
  )
}

export default function CustomerProfile({ profile, loading, error, refresh }) {
  return (
    <main className="commerce-page profile-page" aria-label="个人信息">
      {error && (
        <div className="commerce-error" role="alert">
          <span>{error}</span>
          <button disabled={loading} onClick={refresh}>
            重新加载
          </button>
        </div>
      )}
      {loading && (
        <p className="membership-muted" role="status">
          正在加载个人信息…
        </p>
      )}
      {!loading && !error && profile && (
        <>
          <section className="account-overview" aria-label="账号资料">
            <span className="account-avatar">
              <UserRound size={28} />
            </span>
            <div>
              <h2>{profile.username || '暂未提供'}</h2>
            </div>
            <div className="account-joined">
              <span>注册时间</span>
              <strong>{date(profile.created_at)}</strong>
            </div>
          </section>
          {(profile.memberships || []).map((facts) => (
            <MembershipGroup key={facts.platform_code} facts={facts} />
          ))}
          {!profile.memberships?.length && (
            <div className="commerce-empty">
              <Award size={36} />
              <h2>暂无会员信息</h2>
              <p>当前账号尚无会员记录。</p>
            </div>
          )}
        </>
      )}
    </main>
  )
}
