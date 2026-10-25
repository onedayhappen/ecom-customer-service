export default function OperationConfirmation({
  operation,
  enabled = false,
  busy = false,
  onConfirm,
  onCancel,
  onRefresh,
}) {
  if (!operation) return null
  const review = operation.review || {}
  const statuses = {
    prepared: '等待确认',
    running: '正在提交，请勿重复办理',
    succeeded: '已收到业务回执',
    failed: '本次操作未通过业务校验',
    cancelled: '本次操作已取消',
    unknown: '结果待核实，不能重复提交',
  }
  const money =
    typeof review.amount === 'string' && /^\d+\.\d{2}$/.test(review.amount)
      ? review.amount
      : Number.isSafeInteger(review.amount_minor)
        ? (review.amount_minor / 100).toFixed(2)
        : null
  return (
    <section className="support-banner" aria-label="操作确认">
      <div>
        <strong>
          {review.title} ·{' '}
          {operation.result?.verification_source === 'human'
            ? `人工核实：${operation.status === 'succeeded' ? '操作成功' : '操作失败'}`
            : statuses[operation.status] || '状态待核实'}
        </strong>
        <p>
          订单 {review.order_id}
          {review.is_demo ? ' · 演示数据' : ''}
        </p>
        <ul>
          {(review.items || []).map((item, index) => (
            <li key={index}>
              {item.name || item.sku_id} × {item.quantity}
            </li>
          ))}
        </ul>
        {review.quantity != null && <p>共 {review.quantity} 件</p>}
        {review.reason && <p>原因：{review.reason}</p>}
        {money !== null && review.amount_kind !== 'service_request' && (
          <p>
            {{
              refund_estimate: '申请退款金额（待审核）',
              order_reference: '订单金额（参考，非承诺退款金额）',
              purchase_total: '订单应付金额（含运费）',
              price_protection: '申请价保金额（待审核）',
            }[review.amount_kind] || '开票参考金额'}
            ：{review.currency || 'CNY'} {money}
          </p>
        )}
        {review.shipping_amount != null && (
          <p>
            其中运费：{review.currency} {review.shipping_amount}
          </p>
        )}
        {review.payable_amount != null && (
          <p>
            修改后应付金额：{review.currency} {review.payable_amount}
          </p>
        )}
        {review.refund_amount != null && (
          <p>
            申请原路退款金额（待审核）：{review.currency} {review.refund_amount}； 扣除运费：
            {review.currency} {review.shipping_deduction}
          </p>
        )}
        {review.changes && (
          <div>
            {review.changes.phone != null && <p>收货人电话：{review.changes.phone}</p>}
            {review.changes.recipient_name != null && (
              <p>收货人：{review.changes.recipient_name}</p>
            )}
            {review.changes.note != null && <p>备注：{review.changes.note || '（清空）'}</p>}
            {review.changes.invoice && (
              <p>
                发票信息：{review.changes.invoice.invoice_type === 'company' ? '企业' : '个人'}；
                抬头：{review.changes.invoice.title}
                {review.changes.invoice.tax_number
                  ? `；税号：${review.changes.invoice.tax_number}`
                  : ''}
                （仅保存信息）
              </p>
            )}
          </div>
        )}
        {review.replacement_sku_id && (
          <p>
            换货规格：{review.replacement_sku_id}；价差：{review.currency} {review.price_difference}
          </p>
        )}
        {review.tracking_number && (
          <p>
            退货运单：{review.carrier_code} / {review.tracking_number}
          </p>
        )}
        {review.issued_invoice_id && <p>原正式发票编号：{review.issued_invoice_id}</p>}
        {review.invoice_title && (
          <p>
            抬头：{review.invoice_title}；类型：
            {review.invoice_type === 'company' ? '企业' : '个人'}
            {review.tax_number ? `；税号：${review.tax_number}` : ''}
          </p>
        )}
        {review.address && (
          <p>
            目标收货地址：{review.address.region?.join(' / ')} {review.address.detail}； 收件人：
            {review.address.recipient_name}；联系电话：{review.address.phone}
          </p>
        )}
        {review.fields && (
          <p>
            抬头：{review.fields.title}；类型：{review.fields.invoice_type}
            {review.fields.tax_id ? `；税号：${review.fields.tax_id}` : ''}
          </p>
        )}
        {review.original_invoice_id && <p>原发票：{review.original_invoice_id}</p>}
        <p>{review.impact}</p>
        {operation.status === 'prepared' && (
          <p>确认有效期至 {new Date(operation.expires_at * 1000).toLocaleTimeString()}</p>
        )}
        {operation.error && <p role="status">{operation.error}</p>}
        {operation.status === 'prepared' && (
          <div className="support-toolbar">
            <button
              disabled={!enabled || busy || !operation.can_confirm || !onConfirm}
              onClick={() => onConfirm(operation)}
            >
              确认这项操作
            </button>
            <button disabled={busy || !onCancel} onClick={() => onCancel(operation)}>
              暂不执行
            </button>
          </div>
        )}
        {['running', 'unknown'].includes(operation.status) && (
          <div>
            <p>操作已经开始，当前不能保证撤回。可查询原回执，或请人工客服核实补救方式。</p>
            <button disabled={busy || !onRefresh} onClick={() => onRefresh(operation)}>
              核实操作结果
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
