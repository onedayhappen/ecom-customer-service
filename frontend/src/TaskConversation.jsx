import { useEffect, useRef, useState } from 'react'
import { post, request, Transcript, useConversation } from './supportClient'
import { acknowledge, restorePending } from './conversationState'
import OperationConfirmation from './OperationConfirmation'

export default function TaskConversation({ identity, onLogout, onStaff }) {
  const sessionKey = `ecom-task-session:${identity.user_id}`
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(sessionKey) || crypto.randomUUID())
  const [input, setInput] = useState(''), [error, setError] = useState(''), [progress, setProgress] = useState(null)
  const [accepted, setAccepted] = useState(false)
  const outboxKey = `ecom-task-outbox:${identity.user_id}:${sessionId}`
  const [pending, setPending] = useState(() => restorePending(sessionStorage, outboxKey, sessionId))
  const items = useRef(pending), active = useRef(sessionId)
  const conversation = useConversation(sessionId)
  useEffect(() => { localStorage.setItem(sessionKey, sessionId) }, [sessionKey, sessionId])
  useEffect(() => { active.current = sessionId; return () => { active.current = null } }, [sessionId])
  function save(next) {
    items.current = next; setPending(next)
    try { sessionStorage.setItem(outboxKey, JSON.stringify(next)) }
    catch { setError('浏览器无法保存待发送消息，请保持页面打开并核对发送结果。') }
  }
  useEffect(() => {
    const next = acknowledge(items.current, conversation.messages)
    if (next.length !== items.current.length) { save(next); setAccepted(true) }
  }, [conversation.messages])
  useEffect(() => {
    let cancelled = false, timer
    async function refresh() {
      try {
        const data = await request(`/api/v1/task-messages/${sessionId}`)
        if (!cancelled) {
          setProgress(data)
          const next = acknowledge(items.current, data.messages)
          if (next.length !== items.current.length) { save(next); setAccepted(true) }
        }
      } catch { /* SSE reports connectivity; an unused conversation has no intake yet. */ }
      if (!cancelled) timer = setTimeout(refresh, 2000)
    }
    refresh()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [sessionId])
  async function deliver(payload) {
    if (items.current.find(x => x.payload.request_id === payload.request_id)?.status === 'sending') return
    save(items.current.map(x => x.payload.request_id === payload.request_id ? { ...x, status: 'sending' } : x))
    try {
      await post('/api/v1/task-messages', payload)
      if (active.current !== payload.session_id) return
      save(items.current.filter(x => x.payload.request_id !== payload.request_id))
      setAccepted(true); setProgress(previous => ({ ...previous, processing: 'processing' })); setError('')
    } catch (e) {
      if (active.current !== payload.session_id) return
      save(items.current.map(x => x.payload.request_id === payload.request_id ? { ...x, status: 'failed' } : x))
      setError('尚未确认是否收到，请用下方重试按钮核实：' + e.message)
    }
  }
  function send(event) {
    event.preventDefault()
    const message = input.trim()
    if (!message || items.current.length >= 20) return
    const payload = { session_id: sessionId, request_id: crypto.randomUUID(), message }
    save([...items.current, { payload, status: 'queued' }])
    setInput(''); setError('')
    deliver(payload)
  }
  function startNew() {
    const sid = crypto.randomUUID()
    active.current = sid; setSessionId(sid); setInput(''); setError(''); setAccepted(false); setProgress(null)
    // Pending requests remain recoverable in the previous conversation's outbox.
    items.current = []; setPending([])
  }
  async function changeControl(action) {
    try {
      const state = await post(`/api/v1/sessions/${sessionId}/${action}`)
      if (active.current === sessionId) conversation.setState(state)
    } catch (e) { setError(e.message) }
  }
  const state = conversation.state
  const status = progress?.processing === 'processing' ? '正在处理，你可以继续补充。'
    : progress?.processing === 'waiting_user' ? '等待你的补充。' : ''
  return <main className="support-shell customer-shell">
    <header className="support-top"><div><small>小夕客服</small>
      <h1>{state?.mode === 'human' ? '人工客服' : state?.mode === 'handoff_pending' ? '等待人工接入' : '智能咨询'}</h1></div>
      <div className="support-toolbar">{identity.role !== 'customer' && <button onClick={onStaff}>客服工作台</button>}
        <button onClick={startNew} disabled={pending.length > 0}>新建会话</button><button onClick={onLogout}>退出</button></div>
    </header>
    <section className="support-detail"><header><span>会话 #{sessionId.slice(0, 8)}</span><span>{identity.user_id}</span></header>
      {(error || conversation.error) && <div className="support-alert" role="alert">{error || conversation.error}</div>}
      <Transcript {...conversation} />
      <div className="support-toolbar">
        <span role="status">{accepted || progress?.received_sequence ? '消息已接收。' : ''}{state?.mode === 'bot' ? status : state ? '人工客服将继续处理，你可以补充信息。' : ''}</span>
        {(!state || state.mode === 'bot') ? <button onClick={() => changeControl('handoff')}>转人工客服</button>
          : <button onClick={() => changeControl('resume-bot')}>返回机器人</button>}
      </div>
      {pending.map(item => <div className="support-banner" key={item.payload.request_id}>
        <span>{item.payload.message} · {item.status === 'sending' ? '正在发送' : '等待核实接收结果'}</span>
        {item.status !== 'sending' && <button onClick={() => deliver(item.payload)}>重试这条消息</button>}
      </div>)}
      <OperationConfirmation operation={null} />
      <form className="support-compose" onSubmit={send}>
        <textarea aria-label="咨询内容" maxLength={4000} value={input} onChange={e => setInput(e.target.value)}
          placeholder="可以连续补充订单、原因和其他问题" />
        <button disabled={!input.trim() || pending.length >= 20}>发送消息</button>
      </form>
    </section>
  </main>
}
