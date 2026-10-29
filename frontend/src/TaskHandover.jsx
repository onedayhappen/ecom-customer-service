import { useState } from 'react'
import { post } from './api/client'

const fields = {
  reason: '问题原因',
  quantity: '数量',
  order_id: '订单号',
  item_id: '商品编号',
  sku_id: '规格编号',
  store_id: '店铺',
}

const evidenceKinds = {
  candidates: '候选记录（尚未选定）',
  business_observation: '历史业务查询',
  operation_preparation: '待确认操作（尚未提交）',
}

function EditTask({ task, ticketId, onSaved }) {
  const [goal, setGoal] = useState(task.goal)
  const [field, setField] = useState('reason')
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(null)
  async function save(event) {
    event.preventDefault()
    if (!goal.trim()) return
    let details
    if (value.trim()) {
      const parsed = field === 'quantity' ? Number(value) : value.trim()
      if (field === 'quantity' && (!Number.isInteger(parsed) || parsed < 1)) {
        setError('数量应为大于零的整数。')
        return
      }
      details = { [field]: parsed }
    }
    const payload = {
      expected_version: task.version,
      ...(details ? { details } : {}),
      ...(goal !== task.goal ? { goal: goal.trim() } : {}),
    }
    if (!details && !payload.goal) {
      setError('请填写需要更新的信息。')
      return
    }
    const signature = JSON.stringify(payload)
    const body =
      pending?.signature === signature
        ? pending.body
        : { ...payload, request_id: crypto.randomUUID() }
    setPending({ signature, body })
    setBusy(true)
    setError('')
    try {
      await post(`/api/v1/support/tickets/${ticketId}/tasks/${task.task_id}`, body)
      await onSaved()
      setValue('')
      setPending(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <form onSubmit={save} className="support-compose">
      <p>更新事项记录，恢复机器人后按新记录重新核实；这不会提交退款或授予额外权益。</p>
      <label>
        处理目标
        <input
          aria-label="人工更新处理目标"
          maxLength={2000}
          value={goal}
          disabled={busy}
          onChange={(e) => setGoal(e.target.value)}
        />
      </label>
      <label>
        补充字段
        <select
          aria-label="人工补充字段"
          value={field}
          disabled={busy}
          onChange={(e) => setField(e.target.value)}
        >
          {Object.entries(fields).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <input
        aria-label="人工补充内容"
        maxLength={2000}
        value={value}
        disabled={busy}
        onChange={(e) => setValue(e.target.value)}
        placeholder="填写已核对的诉求信息"
      />
      <button disabled={busy}>{busy ? '保存中…' : '保存事项更新'}</button>
      {error && <p role="alert">{error}</p>}
    </form>
  )
}

export default function TaskHandover({ entries = [], ticketId, editable, onSaved }) {
  return entries.map((entry) => {
    const value = entry.handover,
      task = value.task
    if (!task) return null
    return (
      <section className="support-card" key={task.task_id} aria-label="事项交接记录">
        <h3>{task.goal}</h3>
        <p>
          {task.details.order_id ? `订单 ${task.details.order_id} · ` : ''}
          {entry.active ? '人工接管中' : '已交接恢复'}
        </p>
        <p>当前事项信息：{JSON.stringify(task.details)}</p>
        {task.waiting_for && <p>待补充或待跟进：{JSON.stringify(task.waiting_for)}</p>}
        <details>
          <summary>用户原话</summary>
          {value.customer_statements.map((item) => (
            <p key={`${item.session_id}:${item.sequence}`} style={{ whiteSpace: 'pre-wrap' }}>
              {item.content}
              <br />
              <small>{item.created_at}</small>
            </p>
          ))}
        </details>
        <details>
          <summary>客服公开沟通</summary>
          {(value.staff_statements || []).map((item) => (
            <p key={`${item.session_id}:${item.sequence}`} style={{ whiteSpace: 'pre-wrap' }}>
              {item.content}
              <br />
              <small>
                {item.actor} · {item.created_at} · {item.scope}
              </small>
            </p>
          ))}
        </details>
        <details>
          <summary>查询依据与操作回执</summary>
          <p>{value.note}</p>
          {(value.observed_evidence || []).map((item) => (
            <div key={item.event_id}>
              <p>
                {evidenceKinds[item.evidence_kind] || '历史业务查询'} · {item.tool} ·
                {item.success ? '当时查询成功' : '查询未成功'} · {item.observed_at}
              </p>
              <p>{item.note}</p>
              <p>查询范围：{JSON.stringify(item.query_scope || item.object || {})}</p>
              <p>
                {item.facts && Object.keys(item.facts).length
                  ? JSON.stringify(item.facts)
                  : '摘要未载入可展示事实，不代表没有业务记录；请核对原始回执。'}
              </p>
              <p>
                来源：{JSON.stringify(item.sources || [])} · 业务读取时间：
                {item.result_observed_at || '未记录'} · 数据同步时间：
                {item.data_as_of || '未记录'}
              </p>
              <p>
                来源事件：{item.session_id} / {item.sequence} · 事项版本：{item.task_version}
              </p>
              {item.limitations?.length > 0 && <p>查询限制：{JSON.stringify(item.limitations)}</p>}
              {item.missing_fields?.length > 0 && (
                <p>待核实字段：{JSON.stringify(item.missing_fields)}</p>
              )}
              {(item.has_more ||
                item.completeness === 'partial' ||
                item.summary_omitted_paths?.length > 0) && (
                <p>本摘要仅覆盖部分结果，请核对分页、截断和缺失范围。</p>
              )}
            </div>
          ))}
          {(value.operations || []).map((item) => (
            <p key={item.operation_id}>
              {item.tool} · {item.status}
              <br />
              {JSON.stringify(item.result)}
              <br />
              {item.note}
            </p>
          ))}
        </details>
        {value.image_observations?.length > 0 && (
          <details>
            <summary>图片观察（待业务核验）</summary>
            <p>图片线索不代表已核实的订单、金额、责任或办理授权。</p>
            {value.image_observations.map((item, index) => (
              <div key={`${item.attachment_id || 'omitted'}:${index}`}>
                <p>
                  图片：{item.attachment_id || '部分图片未载入'} · {item.status}
                </p>
                {item.note && <p>{item.note}</p>}
                {(item.observations || []).map((observation) => (
                  <p key={observation.observation_id}>
                    {observation.result?.summary}
                    <br />
                    不确定性：{JSON.stringify(observation.result?.uncertainties || [])}
                    <br />
                    {observation.created_at} · 来源消息：{item.session_id} / {item.sequence}
                  </p>
                ))}
              </div>
            ))}
          </details>
        )}
        {editable && entry.active && (
          <EditTask key={task.version} task={task} ticketId={ticketId} onSaved={onSaved} />
        )}
      </section>
    )
  })
}
