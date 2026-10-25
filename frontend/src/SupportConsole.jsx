import { useEffect, useState } from 'react'
import { post, request } from './api/client'
import { ticketStatusLabels as labels } from './support/constants'
import Ticket from './support/Ticket'
import StaffSettings from './support/StaffSettings'

export default function SupportConsole({ role, onBack }) {
  const [view, setView] = useState('queue')
  const [items, setItems] = useState([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [offset, setOffset] = useState(0)
  async function refresh() {
    setItems(await request(`/api/v1/support/tickets?view=${view}&offset=${offset}`))
  }
  useEffect(() => {
    let active = true
    async function load() {
      try {
        const data = await request(`/api/v1/support/tickets?view=${view}&offset=${offset}`)
        if (active) setItems(data)
      } catch (e) {
        if (active) setError(e.message)
      }
    }
    load()
    const timer = setInterval(load, 5000)
    return () => {
      active = false
      clearInterval(timer)
    }
  }, [view, offset])
  async function claim(ticket) {
    try {
      await post(`/api/v1/support/tickets/${ticket.ticket_id}/transition`, {
        action: 'claim',
        version: ticket.version,
      })
      setSelected(ticket.ticket_id)
      setError('')
      await refresh()
    } catch (e) {
      setError(e.message)
      await refresh().catch(() => {})
    }
  }
  return (
    <main className="support-shell">
      <header className="support-top">
        <div>
          <small>优优客服</small>
          <h1>人工服务工作台</h1>
        </div>
        <button onClick={onBack}>返回用户端</button>
      </header>
      {error && (
        <div className="support-alert" role="alert">
          {error}
        </div>
      )}
      <div className="support-columns">
        <aside className="support-queue">
          <nav>
            {['queue', 'mine', ...(role === 'admin' ? ['all'] : [])].map((v) => (
              <button
                className={view === v ? 'selected' : ''}
                key={v}
                onClick={() => {
                  setView(v)
                  setOffset(0)
                }}
              >
                {{ queue: '待接单', mine: '我的工单', all: '全部' }[v]}
              </button>
            ))}
          </nav>
          {items.length === 0 && <p className="support-muted">当前没有工单</p>}
          {items.map((t) => (
            <article className="support-ticket" key={t.ticket_id}>
              <strong>
                #{t.ticket_id.slice(0, 8)} · {labels[t.status]}
              </strong>
              <small>
                {t.reason} · {new Date(t.created_at).toLocaleString()}
              </small>
              <span>
                等待/创建于{' '}
                {Math.max(0, Math.floor((Date.now() - Date.parse(t.created_at)) / 60000))} 分钟前
              </span>
              {t.first_reply_at && (
                <small>
                  首次人工回复：
                  {Math.round((Date.parse(t.first_reply_at) - Date.parse(t.created_at)) / 1000)} 秒
                </small>
              )}
              <button onClick={() => (t.status === 'queued' ? claim(t) : setSelected(t.ticket_id))}>
                {t.status === 'queued' ? '领取工单' : '打开'}
              </button>
            </article>
          ))}
          <div className="support-toolbar">
            <button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 30))}>
              上一页
            </button>
            <button disabled={items.length < 30} onClick={() => setOffset(offset + 30)}>
              下一页
            </button>
          </div>
          {role === 'admin' && <StaffSettings />}
        </aside>
        {selected ? (
          <Ticket key={selected} ticketId={selected} role={role} onChanged={refresh} />
        ) : (
          <section className="support-card support-empty">
            <h2>选择工单开始处理</h2>
            <p>领取后可以查看对话、交接摘要和业务执行状态。</p>
          </section>
        )}
      </div>
    </main>
  )
}
