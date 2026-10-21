export default function OperationConfirmation({ operation, enabled = false, busy = false, onConfirm, onCancel, onRefresh }) {
  if (!operation) return null
  const review = operation.review || {}
  const statuses = { prepared: '等待确认', running: '正在提交，请勿重复办理', succeeded: '已收到业务回执',
    failed: '本次操作未通过业务校验', cancelled: '本次操作已取消', unknown: '结果待核实，不能重复提交' }
  const money = Number.isSafeInteger(review.amount_minor) ? (review.amount_minor / 100).toFixed(2) : null
  return <section className="support-banner" aria-label="操作确认">
    <div>
      <strong>{review.title} · {operation.result?.verification_source === 'human'
        ? `人工核实：${operation.status === 'succeeded' ? '操作成功' : '操作失败'}`
        : statuses[operation.status] || '状态待核实'}</strong>
      <p>订单 {review.order_id}{review.is_demo ? ' · 演示数据' : ''}</p>
      <ul>{(review.items || []).map((item, index) => <li key={index}>{item.name} × {item.quantity}</li>)}</ul>
      <p>共 {review.quantity} 件{review.reason ? `；原因：${review.reason}` : ''}</p>
      {money !== null && <p>{review.amount_kind === 'order_reference' ? '订单金额（参考，非承诺退款金额）' : '本地演示开票金额'}：¥{money}</p>}
      {review.fields && <p>抬头：{review.fields.title}；类型：{review.fields.invoice_type}
        {review.fields.tax_id ? `；税号：${review.fields.tax_id}` : ''}</p>}
      {review.original_invoice_id && <p>原发票：{review.original_invoice_id}</p>}
      <p>{review.impact}</p>
      {operation.status === 'prepared' && <p>确认有效期至 {new Date(operation.expires_at * 1000).toLocaleTimeString()}</p>}
      {operation.error && <p role="status">{operation.error}</p>}
      {operation.status === 'prepared' && <div className="support-toolbar">
        <button disabled={!enabled || busy || !operation.can_confirm || !onConfirm}
          onClick={() => onConfirm(operation)}>确认这项操作</button>
        <button disabled={busy || !onCancel} onClick={() => onCancel(operation)}>暂不执行</button>
      </div>}
      {['running', 'unknown'].includes(operation.status) && <div>
        <p>操作已经开始，当前不能保证撤回。可查询原回执，或请人工客服核实补救方式。</p>
        <button disabled={busy || !onRefresh} onClick={() => onRefresh(operation)}>核实操作结果</button>
      </div>}
    </div>
  </section>
}
