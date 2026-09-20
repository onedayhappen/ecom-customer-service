import { useEffect, useRef, useState } from 'react'
import {
  ArrowUpRight, Bot, Check, CheckCircle2, ChevronDown, CircleAlert,
  Headphones, LifeBuoy, LogOut, MessageSquarePlus, Paperclip, RotateCcw,
  SendHorizontal, Settings2, ShieldCheck, Sparkles, UserRound, Zap,
} from 'lucide-react'

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const SESSION_KEY = 'ecom-agent-session-id'
const suggestedQuestions = [
  { title: '订单进度', text: '帮我查一下最近一笔订单的物流进度', icon: '01' },
  { title: '售后服务', text: '商品有问题，想申请退换货', icon: '02' },
  { title: '优惠活动', text: '最近有什么优惠券可以使用？', icon: '03' },
]

function csrfToken() {
  const item = document.cookie.split('; ').find((entry) => entry.startsWith('ecom_agent_csrf='))
  return item ? decodeURIComponent(item.slice('ecom_agent_csrf='.length)) : ''
}

function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const csrf = !['GET', 'HEAD', 'OPTIONS'].includes(method) ? csrfToken() : ''
  return fetch(`${API_BASE}${path}`, {
    ...options, credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}), ...(options.headers || {}) },
  }).then(async (response) => {
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || '请求失败，请稍后重试')
    return body
  })
}

function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)) }
async function waitForChatJob(jobId) {
  const deadline = Date.now() + 120000
  while (Date.now() < deadline) {
    const data = await request(`/api/v1/chat/${jobId}`)
    if (data.status === 'completed') return data.result
    if (data.status === 'failed') throw new Error(data.error || '客服任务执行失败')
    await wait(250)
  }
  throw new Error('客服任务等待超时，请稍后查看会话')
}

function Message({ item }) {
  const isUser = item.role === 'user'
  return <div className={`message-row ${isUser ? 'message-user' : 'message-agent'}`}>
    <div className="avatar" aria-hidden="true">{isUser ? <UserRound size={17} /> : <Bot size={18} />}</div>
    <div className="message-content"><div className="message-meta"><strong>{isUser ? '你' : '小夕'}</strong><span>{isUser ? '刚刚' : '智能客服 · 在线'}</span></div><div className="message-bubble">{item.content}</div></div>
  </div>
}

function AuthScreen({ onSave }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  async function submit(event) {
    event.preventDefault(); setError(''); setLoading(true)
    try { await request('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }); onSave() }
    catch (err) { setError(err.message) } finally { setLoading(false) }
  }
  return <main className="auth-page"><div className="auth-orb auth-orb-one" /><div className="auth-orb auth-orb-two" /><section className="auth-shell">
    <div className="auth-showcase"><div className="brand-lockup"><span className="brand-mark"><Bot size={23} /></span><span>小夕客服</span></div><div className="showcase-copy"><span className="eyebrow"><Sparkles size={14} /> 新一代智能服务台</span><h1>让每一次咨询，<br /><em>都有及时回应。</em></h1><p>订单、物流、售后与商品问题，交给小夕快速处理。需要人工协助时，我们也会无缝接力。</p></div><div className="showcase-features"><div><span className="feature-icon"><Zap size={16} /></span><span><strong>快速响应</strong><small>平均 10 秒内给出方案</small></span></div><div><span className="feature-icon"><ShieldCheck size={16} /></span><span><strong>安全可靠</strong><small>你的会话受到加密保护</small></span></div></div><div className="showcase-footer"><span className="live-dot" />服务系统运行正常 <span>·</span> 7 × 24 小时在线</div></div>
    <div className="auth-card"><div className="auth-card-heading"><span className="auth-card-icon"><Headphones size={20} /></span><div><h2>欢迎回来</h2><p>登录你的客服工作台</p></div></div><form onSubmit={submit}><label htmlFor="login-username">用户名</label><div className="input-wrap"><UserRound size={17} /><input id="login-username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="请输入用户名" autoComplete="username" required /></div><label htmlFor="login-password">密码</label><div className="input-wrap"><ShieldCheck size={17} /><input id="login-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="请输入密码" autoComplete="current-password" required /></div>{error && <div className="form-error"><CircleAlert size={16} />{error}</div>}<button className="auth-submit" type="submit" disabled={loading}>{loading ? '正在登录…' : <><span>进入工作台</span><ArrowUpRight size={17} /></>}</button></form><p className="auth-hint">登录即表示你同意服务条款与隐私政策</p></div>
  </section></main>
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(false)
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(SESSION_KEY) || '')
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState('')
  const [handoff, setHandoff] = useState(false)
  const [confirmationToken, setConfirmationToken] = useState('')
  const endRef = useRef(null)
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, loading])
  useEffect(() => { let cancelled = false; request('/api/v1/me').then(() => { if (!cancelled) setAuthenticated(true) }).catch(() => { if (!cancelled) { localStorage.removeItem(SESSION_KEY); setAuthenticated(false); setSessionId('') } }); return () => { cancelled = true } }, [])
  useEffect(() => { if (!authenticated || !sessionId) return; request(`/api/v1/sessions/${sessionId}`).then((data) => setMessages(data.messages || [])).catch(() => localStorage.removeItem(SESSION_KEY)) }, [authenticated, sessionId])
  function startNew() { localStorage.removeItem(SESSION_KEY); setSessionId(''); setMessages([]); setHandoff(false); setConfirmationToken(''); setNotice('已创建新的咨询上下文。') }
  async function send() {
    const message = input.trim(); if (!message || loading) return
    const activeSessionId = sessionId || crypto.randomUUID()
    if (!sessionId) { localStorage.setItem(SESSION_KEY, activeSessionId); setSessionId(activeSessionId) }
    setInput(''); setLoading(true); setNotice(''); setMessages((current) => [...current, { role: 'user', content: message }])
    try {
      const accepted = await request('/api/v1/chat', { method: 'POST', body: JSON.stringify({ message, session_id: activeSessionId, request_id: crypto.randomUUID(), ...(confirmationToken ? { confirmation_token: confirmationToken } : {}) }) })
      const data = accepted.job_id ? await waitForChatJob(accepted.job_id) : accepted
      localStorage.setItem(SESSION_KEY, data.session_id); setSessionId(data.session_id); setHandoff(Boolean(data.requires_human)); setConfirmationToken(data.confirmation_token || '')
      if (data.confirmation_token) setNotice('请核对客服列出的操作内容；回复“确认”后才会执行。')
      setMessages((current) => [...current, { role: 'assistant', content: data.reply }])
    } catch (err) { setMessages((current) => current.slice(0, -1)); setNotice(err.message) } finally { setLoading(false) }
  }
  async function reset() { if (!sessionId) return startNew(); try { await request(`/api/v1/sessions/${sessionId}/reset`, { method: 'POST' }); startNew() } catch (err) { setNotice(err.message) } }
  async function logout() { try { await request('/api/v1/auth/logout', { method: 'POST' }) } catch { /* 本地状态仍需清理 */ } localStorage.removeItem(SESSION_KEY); setAuthenticated(false); setSessionId(''); setMessages([]); setConfirmationToken('') }
  function useSuggestion(text) { setInput(text); window.setTimeout(() => document.querySelector('.composer textarea')?.focus(), 0) }
  if (!authenticated) return <AuthScreen onSave={() => setAuthenticated(true)} />
  return <main className="app-shell">
    <aside className="sidebar"><div className="sidebar-top"><div className="sidebar-brand"><span className="brand-mark small"><Bot size={18} /></span><span>小夕客服</span><span className="brand-beta">PRO</span></div><button className="new-chat" onClick={startNew}><MessageSquarePlus size={17} />新建咨询<span>⌘ K</span></button><div className="nav-label">工作台</div><div className="nav-item active"><span><MessageSquarePlus size={17} />当前会话</span><span className="nav-count">{messages.length ? messages.length : ''}</span></div><div className="nav-item"><span><LifeBuoy size={17} />服务指南</span><ArrowUpRight size={14} /></div></div><div className="sidebar-middle"><div className="nav-label">当前上下文</div><div className="session-card"><div className="session-card-title"><span className="session-pulse" />服务进行中 <ChevronDown size={14} /></div><strong>{sessionId ? `#${sessionId.slice(0, 8)}` : '等待新的咨询'}</strong><span className="session-caption">{messages.length ? `${messages.length} 条消息 · 刚刚更新` : '输入问题开始对话'}</span></div></div><div className="sidebar-bottom"><div className="workspace-tip"><Sparkles size={15} /><span><strong>小贴士</strong><small>描述得越具体，解决得越快</small></span></div><button className="side-action" onClick={reset}><RotateCcw size={16} />重置会话</button><button className="side-action" onClick={logout}><LogOut size={16} />退出工作台</button><div className="profile"><span className="profile-avatar">客</span><span><strong>客服用户</strong><small>已验证身份</small></span><Settings2 size={16} /></div></div></aside>
    <section className="workspace"><header className="workspace-header"><div className="mobile-brand"><span className="brand-mark small"><Bot size={17} /></span><strong>小夕客服</strong></div><div className="breadcrumbs"><span>工作台</span><span>/</span><strong>智能咨询</strong></div><div className="header-actions"><div className="header-status"><span className="live-dot" />服务在线</div><button className="icon-button" title="服务指南"><LifeBuoy size={18} /></button></div></header><div className="workspace-body"><section className="chat-panel"><div className="conversation-head"><div><span className="eyebrow"><Sparkles size={13} /> AI 智能助手</span><h1>今天有什么可以帮你？</h1><p>小夕会理解你的问题，并给出清晰的处理方案。</p></div><div className="secure-label"><ShieldCheck size={15} />会话已加密</div></div><section className="conversation" aria-live="polite">{!messages.length && <div className="empty-state"><div className="empty-icon"><Bot size={30} /></div><h2>你好，我是小夕</h2><p>从下面选择一个常见问题，或直接告诉我你遇到的情况。</p><div className="suggestions">{suggestedQuestions.map((item) => <button key={item.title} className="suggestion" onClick={() => useSuggestion(item.text)}><span className="suggestion-index">{item.icon}</span><span><strong>{item.title}</strong><small>{item.text}</small></span><ArrowUpRight size={15} /></button>)}</div></div>}{messages.map((item, index) => <Message key={`${item.role}-${index}`} item={item} />)}{loading && <div className="typing-row"><div className="avatar"><Bot size={18} /></div><div className="typing"><span /><span /><span /><small>小夕正在思考…</small></div></div>}<div ref={endRef} /></section>{handoff && <div className="handoff"><CheckCircle2 size={17} /><span><strong>已为你安排人工跟进</strong><small>客服团队会在当前会话中继续处理</small></span></div>}{notice && <div className="notice"><CircleAlert size={17} /><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示">×</button></div>}<footer className="composer-wrap"><div className="composer"><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send() } }} placeholder="描述你遇到的问题…" rows="1" /><div className="composer-actions"><button className="attach-button" title="添加附件"><Paperclip size={17} /></button><button className="send-button" onClick={send} disabled={!input.trim() || loading} title="发送消息"><SendHorizontal size={18} /></button></div></div><div className="composer-meta"><span>Enter 发送 · Shift + Enter 换行</span><span>{input.length}/2000</span></div></footer></section><aside className="insights-panel"><div className="insights-heading"><span>服务概览</span><button title="更多设置"><Settings2 size={16} /></button></div><div className="service-card"><div className="service-icon"><Headphones size={19} /></div><div><strong>智能客服</strong><span><i /> 正在为你服务</span></div><Check size={17} /></div><div className="insight-block"><span className="insight-label">当前会话</span><strong>{sessionId ? `#${sessionId.slice(0, 8)}` : '尚未开始'}</strong><span className="insight-muted">所有消息会自动保存</span></div><div className="insight-divider" /><div className="insight-block"><span className="insight-label">你可以这样问</span><ul><li>“我的订单什么时候送到？”</li><li>“商品尺寸不合适怎么办？”</li><li>“如何修改收货地址？”</li></ul></div><div className="insight-support"><LifeBuoy size={17} /><span><strong>需要更多帮助？</strong><small>随时可以请求人工客服</small></span><ArrowUpRight size={15} /></div></aside></div></section>
  </main>
}
