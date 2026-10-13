import { useEffect, useRef, useState } from 'react'

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8010').replace(/\/+$/, '')
export async function request(path, options = {}) {
  const cookie = document.cookie.split('; ').find(x => x.startsWith('ecom_agent_csrf='))
  const token = cookie ? decodeURIComponent(cookie.slice('ecom_agent_csrf='.length)) : ''
  const response = await fetch(`${API_BASE}${path}`, {
    ...options, credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}), ...options.headers },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || '服务暂时不可用')
  return data
}
export const post = (path, body = {}) => request(path, { method: 'POST', body: JSON.stringify(body) })
export const labels = { queued: '等待接单', in_progress: '处理中', waiting_customer: '等待用户', resolved: '已解决', cancelled: '已取消' }

export function useConversation(sessionId, staff = false, ticketId = '') {
  const [messages, setMessages] = useState([])
  const [state, setState] = useState(null)
  const [error, setError] = useState('')
  const [tasks, setTasks] = useState([]), [taskQuestion, setTaskQuestion] = useState(null)
  useEffect(() => {
    setMessages([]); setState(null); setError(''); setTasks([]); setTaskQuestion(null)
    if (!sessionId) return
    let cancelled = false, source, cursor = 0
    const scope = `staff=${staff}${ticketId ? `&ticket_id=${ticketId}` : ''}`
    const apply = data => {
      if (cancelled) return
      cursor = data.cursor
      setState(data.state)
      setTasks(previous => {
        const merged = new Map(previous.map(t => [t.task_id, t]))
        data.events.filter(e => e.kind === 'task_state').forEach(e => merged.set(e.payload.task.task_id, e.payload.task))
        return [...merged.values()]
      })
      data.events.filter(e => e.kind === 'task_choice').forEach(e => setTaskQuestion(e.payload.question))
      setMessages(previous => {
        const merged = new Map(previous.map(m => [m.sequence, m]))
        data.events.filter(e => e.kind === 'message').forEach(e => merged.set(e.sequence, { ...e.payload, sequence: e.sequence, actor: e.actor, at: e.created_at }))
        return [...merged.values()].sort((a, b) => a.sequence - b.sequence)
      })
    }
    async function start() {
      try {
        if (!staff) await request(`/api/v1/sessions/${sessionId}/support-state`)
        while (!cancelled) {
          const data = await request(`/api/v1/sessions/${sessionId}/messages?after=${cursor}&${scope}`)
          apply(data)
          if (data.events.length < 100) break
        }
        if (cancelled) return
        source = new EventSource(`${API_BASE}/api/v1/sessions/${sessionId}/support-events?after=${cursor}&${scope}`, { withCredentials: true })
        source.addEventListener('update', e => { apply(JSON.parse(e.data)); setError('') })
        source.addEventListener('revoked', () => { source.close(); setMessages([]); setTasks([]); setTaskQuestion(null); setError('会话权限已变更，请返回列表刷新') })
        source.onerror = () => { if (!cancelled) setError('连接中断，正在恢复消息…') }
        source.onopen = () => { if (!cancelled) setError('') }
      } catch (e) { if (!cancelled) setError(e.message) }
    }
    start()
    return () => { cancelled = true; source?.close() }
  }, [sessionId, staff, ticketId])
  return { messages, state, error, setState, tasks, taskQuestion }
}

export function Transcript({ messages }) {
  const container = useRef(null)
  useEffect(() => { const node = container.current; if (node) node.scrollTop = node.scrollHeight }, [messages.length])
  return <div ref={container} className="support-transcript" aria-live="polite">{messages.length === 0 && <p className="support-muted">暂无消息</p>}{messages.map(m => <article key={m.sequence} className={`support-message ${m.role === 'user' ? 'from-user' : ''} ${m.internal ? 'internal-note' : ''}`}>
    <small>{m.role === 'user' ? '用户' : m.role === 'agent' ? `客服 ${m.actor}` : 'AI 客服'}{m.internal ? ' · 内部备注' : ''} · {new Date(m.at).toLocaleTimeString()}</small>
    <p>{m.content}</p>
  </article>)}</div>
}
