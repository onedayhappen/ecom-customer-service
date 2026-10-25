import { Package } from 'lucide-react'
import { formatMoney as money, formatDateTime as date } from '../shared/format'
import { orderLabels } from './constants'

export default function OrderCard({ order }) {
  return (
    <article className="order-card">
      <header>
        <time dateTime={order.created_at}>{date(order.created_at)}</time>
        <span className={`commerce-badge status-${order.status}`}>
          {orderLabels[order.status] || '状态待确认'}
        </span>
      </header>
      <div className="order-products">
        {order.items.length ? (
          order.items.map((item) => (
            <div className="order-product" key={item.order_item_id}>
              <span className="product-symbol">
                <Package size={23} />
              </span>
              <div>
                <h3>{item.title_snapshot || '商品信息待补充'}</h3>
                <p>{Object.values(item.spec_snapshot || {}).join(' / ') || '标准规格'}</p>
              </div>
              <span>× {item.quantity}</span>
            </div>
          ))
        ) : (
          <p className="support-muted">暂无商品明细</p>
        )}
      </div>
      <footer>
        <span>
          共 {order.items.reduce((sum, item) => sum + Number(item.quantity || 0), 0)} 件商品
        </span>
        <span>
          <strong>{money(order.payable_amount, order.currency)}</strong>
        </span>
      </footer>
    </article>
  )
}
