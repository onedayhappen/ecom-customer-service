import { useEffect, useState } from 'react'
import { post, request } from '../api/client'
import { ticketStatusLabels as labels } from './constants'
import Transcript from '../chat/Transcript'
import { useConversation } from '../chat/useConversation'
import TaskPanel from '../TaskPanel'
import TaskHandover from '../TaskHandover'
import Reconcile from './Reconcile'

export default function Ticket({ ticketId, role, onChanged }) {
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState('')
  const [text, setText] = useState('')
  const [internal, setInternal] = useState(false)
  const [agents, setAgents] = useState([])
  const [assignee, setAssignee] = useState('')
  const [busy, setBusy] = useState(false)
  const [pendingMessage, setPendingMessage] = useState(null)
  const conversation = useConversation(detail?.ticket.session_id, true, ticketId)
  async function refresh() {
    setDetail(await request(`/api/v1/support/tickets/${ticketId}`))
  }
  useEffect(() => {
    let active = true
    request(`/api/v1/support/tickets/${ticketId}`)
      .then((d) => {
        if (active) setDetail(d)
      })
      .catch((e) => {
        if (active) setError(e.message)
      })
    request('/api/v1/support/staff')
      .then((d) => {
        if (active) setAgents(d.filter((a) => a.enabled))
      })
      .catch((e) => {
        if (active) setError(e.message)
      })
    return () => {
      active = false
    }
  }, [ticketId])
  async function action(action) {
    setBusy(true)
    setError('')
    try {
      await post(`/api/v1/support/tickets/${ticketId}/transition`, {
        action,
        version: conversation.state?.ticket?.version || detail.ticket.version,
        assignee,
      })
      await refresh()
      await onChanged()
    } catch (e) {
      setError(e.message)
      await refresh().catch(() => {})
    } finally {
      setBusy(false)
    }
  }
  async function send(e) {
    e.preventDefault()
    if (!text.trim()) return
    setBusy(true)
    setError('')
    const message =
      pendingMessage?.content === text && pendingMessage?.internal === internal
        ? pendingMessage
        : { content: text, internal, client_message_id: crypto.randomUUID() }
    setPendingMessage(message)
    try {
      await post(`/api/v1/support/tickets/${ticketId}/messages`, message)
      setText('')
      setPendingMessage(null)
      await refresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }
  if (!detail) return <div className="support-card">{error || '加载工单…'}</div>
  const streamed = conversation.state
  const newerStream =
    streamed &&
    ((streamed.control_version || 0) > (detail.state.control_version || 0) ||
      ((streamed.control_version || 0) === (detail.state.control_version || 0) &&
        (streamed.cursor || 0) >= (detail.state.cursor || 0)))
  const state = newerStream ? streamed : detail.state
  const current = state.ticket?.ticket_id === ticketId && state.mode === 'human'
  const active = current && ['in_progress', 'waiting_customer'].includes(state.ticket?.status)
  return (
    <section className="support-detail">
      <header>
        <div>
          <small>工单 #{ticketId.slice(0, 8)}</small>
          <h2>{labels[state.ticket?.status || detail.ticket.status]}</h2>
        </div>
        <span>负责人：{state.ticket?.assignee || detail.ticket.assignee || '未分配'}</span>
      </header>
      {(error || conversation.error) && (
        <div role="alert" className="support-alert">
          {error || conversation.error}
        </div>
      )}
      <details className="support-card">
        <summary>交接摘要与业务操作</summary>
        <button onClick={() => refresh().catch((e) => setError(e.message))}>刷新业务结果</button>
        <p style={{ whiteSpace: 'pre-wrap' }}>{detail.ticket.summary}</p>
        <small>触发原因：{detail.ticket.reason}</small>
        {detail.operations.map((op) => (
          <div className="support-operation" key={op.operation_id}>
            <strong>
              {op.tool} · {op.status}
            </strong>
            <small>{op.operation_id}</small>
            {op.result && <pre>{op.result}</pre>}
            {op.status === 'running' && (
              <p>操作可能仍在下游执行。先核实进程与业务结果，不能直接重试。</p>
            )}
            {role === 'admin' && ['running', 'unknown'].includes(op.status) && (
              <Reconcile operation={op} onDone={refresh} />
            )}
          </div>
        ))}
        {detail.evidence?.map((e, i) => (
          <details key={i}>
            <summary>{e.tool} · 查询依据</summary>
            <pre>{JSON.stringify(e, null, 2)}</pre>
          </details>
        ))}
      </details>
      <Transcript {...conversation} sessionId={detail?.ticket.session_id} ticketId={ticketId} />
      <TaskHandover
        entries={detail.task_handovers}
        ticketId={ticketId}
        editable={active}
        onSaved={refresh}
      />
      <TaskPanel tasks={conversation.tasks.length ? conversation.tasks : detail.tasks} />
      <div className="support-toolbar">
        <button disabled={busy || !active} onClick={() => action('waiting_customer')}>
          等待用户
        </button>
        <button disabled={busy || !active} onClick={() => action('resolved')}>
          标记解决
        </button>
        <button disabled={busy || !active} onClick={() => action('requeue')}>
          退回队列
        </button>
        {role === 'admin' && (
          <>
            <select
              aria-label="转派客服"
              value={assignee}
              onChange={(e) => setAssignee(e.target.value)}
            >
              <option value="">选择客服</option>
              {agents.map((a) => (
                <option key={a.user_id}>{a.user_id}</option>
              ))}
            </select>
            <button
              disabled={busy || !assignee || !['human', 'handoff_pending'].includes(state.mode)}
              onClick={() => action('assign')}
            >
              转派
            </button>
          </>
        )}
      </div>
      <form onSubmit={send} className="support-compose">
        <label>
          <input
            type="checkbox"
            checked={internal}
            onChange={(e) => setInternal(e.target.checked)}
          />
          内部备注（用户不可见）
        </label>
        <textarea
          aria-label="客服回复"
          maxLength={4000}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="输入回复或内部备注"
        />
        <button disabled={busy || !current || !text.trim() || state.ticket?.status === 'resolved'}>
          {busy ? '提交中…' : internal ? '保存备注' : '发送回复'}
        </button>
      </form>
    </section>
  )
}
