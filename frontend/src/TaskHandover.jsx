import { useState } from 'react'
import { post } from './supportClient'

const fields = { reason: '问题原因', quantity: '数量', order_id: '订单号', item_id: '商品编号', sku_id: '规格编号', store_id: '店铺' }

function EditTask({ task, ticketId, onSaved }) {
  const [goal, setGoal] = useState(task.goal), [field, setField] = useState('reason'), [value, setValue] = useState('')
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [pending, setPending] = useState(null)
  async function save(event) {
    event.preventDefault()
    if (!goal.trim()) return
    let details
    if (value.trim()) {
      const parsed = field === 'quantity' ? Number(value) : value.trim()
      if (field === 'quantity' && (!Number.isInteger(parsed) || parsed < 1)) { setError('数量应为大于零的整数。'); return }
      details = { [field]: parsed }
    }
    const payload = { expected_version: task.version, ...(details ? { details } : {}), ...(goal !== task.goal ? { goal: goal.trim() } : {}) }
    if (!details && !payload.goal) { setError('请填写需要更新的信息。'); return }
    const signature = JSON.stringify(payload)
    const body = pending?.signature === signature ? pending.body : { ...payload, request_id: crypto.randomUUID() }
    setPending({ signature, body }); setBusy(true); setError('')
    try { await post(`/api/v1/support/tickets/${ticketId}/tasks/${task.task_id}`, body); await onSaved(); setValue(''); setPending(null) }
    catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return <form onSubmit={save} className="support-compose">
    <p>更新事项记录，恢复机器人后按新记录重新核实；这不会提交退款或授予额外权益。</p>
    <label>处理目标<input aria-label="人工更新处理目标" maxLength={2000} value={goal} disabled={busy} onChange={e => setGoal(e.target.value)} /></label>
    <label>补充字段<select aria-label="人工补充字段" value={field} disabled={busy} onChange={e => setField(e.target.value)}>{Object.entries(fields).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
    <input aria-label="人工补充内容" maxLength={2000} value={value} disabled={busy} onChange={e => setValue(e.target.value)} placeholder="填写已核对的诉求信息" />
    <button disabled={busy}>{busy ? '保存中…' : '保存事项更新'}</button>
    {error && <p role="alert">{error}</p>}
  </form>
}

export default function TaskHandover({ entries = [], ticketId, editable, onSaved }) {
  return entries.map(entry => {
    const value = entry.handover, task = value.task
    if (!task) return null
    return <section className="support-card" key={task.task_id} aria-label="事项交接记录">
      <h3>{task.goal}</h3><p>{task.details.order_id ? `订单 ${task.details.order_id} · ` : ''}{entry.active ? '人工接管中' : '已交接恢复'}</p>
      <p>当前事项信息：{JSON.stringify(task.details)}</p>
      {task.waiting_for && <p>待补充或待跟进：{JSON.stringify(task.waiting_for)}</p>}
      <details><summary>用户原话</summary>{value.customer_statements.map(item => <p key={`${item.session_id}:${item.sequence}`} style={{ whiteSpace: 'pre-wrap' }}>{item.content}<br /><small>{item.created_at}</small></p>)}</details>
      <details><summary>客服公开沟通</summary>{(value.staff_statements || []).map(item => <p key={`${item.session_id}:${item.sequence}`} style={{ whiteSpace: 'pre-wrap' }}>{item.content}<br /><small>{item.actor} · {item.created_at} · {item.scope}</small></p>)}</details>
      <details><summary>查询依据与操作回执</summary><p>{value.note}</p>
        {value.observed_evidence.map(item => <p key={item.event_id}>{item.success ? '当时查询成功' : '查询未成功'} · {item.observed_at}<br />{JSON.stringify(item.facts)}</p>)}
        {value.operations.map(item => <p key={item.operation_id}>{item.tool} · {item.status}<br />{JSON.stringify(item.result)}</p>)}
      </details>
      {editable && entry.active && <EditTask key={task.version} task={task} ticketId={ticketId} onSaved={onSaved} />}
    </section>
  })
}
