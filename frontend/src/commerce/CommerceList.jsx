import { Package, RefreshCw, Ticket } from 'lucide-react'
import { orderLabels, couponTypeLabels } from './constants'
import { useCommerceList } from './useCommerceList'
import OrderCard from './OrderCard'
import CouponCard from './CouponCard'

export default function CommerceList({ kind, status = 'all', couponType = 'all' }) {
  const listPath = `/api/commerce/${kind}?status=${status}${kind === 'coupons' ? `&coupon_type=${couponType}` : ''}`
  const { result, loading, error, refresh, loadMore } = useCommerceList(listPath)
  const noun = kind === 'orders' ? '订单' : '优惠券'
  return (
    <section className="commerce-results" aria-label={`${noun}数据`} aria-busy={loading}>
      <div className="list-summary">
        <span role="status">
          {loading && !result.items.length
            ? '正在加载…'
            : `共 ${result.total} ${kind === 'orders' ? '笔订单' : '张优惠券'}`}
        </span>
        <button className="refresh-button" disabled={loading} onClick={refresh}>
          <RefreshCw size={14} />
          {loading && result.items.length > 0 ? '刷新中…' : '刷新'}
        </button>
      </div>
      {error && (
        <div className="commerce-error" role="alert">
          <span>{error}</span>
          <button disabled={loading} onClick={refresh}>
            重新加载
          </button>
        </div>
      )}
      {loading && !result.items.length && (
        <div className="list-skeleton" aria-hidden="true">
          {[0, 1, 2].map((index) => (
            <div className="list-skeleton-card" key={index}>
              <i />
              <span />
              <span />
            </div>
          ))}
        </div>
      )}
      {!loading && !error && !result.items.length && (
        <div className="commerce-empty">
          {kind === 'orders' ? <Package size={38} /> : <Ticket size={38} />}
          <h2>
            {kind === 'orders' && status !== 'all'
              ? `暂无${orderLabels[status]}订单`
              : kind === 'coupons' && couponType !== 'all'
                ? `暂无${couponTypeLabels[couponType]}`
                : `暂无${noun}`}
          </h2>
          <p>{kind === 'orders' ? '你的订单会显示在这里。' : '这里仅展示尚未过期的已有优惠券。'}</p>
        </div>
      )}
      <div className={kind === 'orders' ? 'order-list' : 'coupon-list'}>
        {result.items.map((item) =>
          kind === 'orders' ? (
            <OrderCard key={`${item.platform_code}:${item.order_id}`} order={item} />
          ) : (
            <CouponCard key={item.user_coupon_id} coupon={item} />
          ),
        )}
      </div>
      {result.next_cursor && (
        <button className="load-more" disabled={loading} onClick={loadMore}>
          {loading ? '加载中…' : '加载更多'}
        </button>
      )}
    </section>
  )
}
