import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Bot, CircleAlert, Headphones, ShieldCheck, Sparkles, UserRound, Zap } from 'lucide-react'
import SupportConsole from './SupportConsole'
import TaskPanel from './TaskPanel'
import { labels, post, request, Transcript, useConversation, nextConversationUpdate } from './supportClient'

const SESSION_KEY = 'ecom-agent-session-id'
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
  const [identity, setIdentity] = useState(null), [authenticated, setAuthenticated] = useState(false)
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(SESSION_KEY) || '')
  const [staffView, setStaffView] = useState(false), [input, setInput] = useState(''), [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false), [confirmation, setConfirmation] = useState(''), [retry, setRetry] = useState(null)
  const conversation = useConversation(authenticated ? sessionId : null)
  const control = useRef(null), activeSession = useRef(sessionId)
  const sendVersion = useRef(0)
  activeSession.current = sessionId; control.current = conversation.state
  useEffect(() => {
    if (conversation.state && (conversation.state.mode !== 'bot' || confirmation?.controlVersion < conversation.state.control_version)) setConfirmation('')
    if (conversation.state && conversation.state.mode !== 'bot') setBusy(false)
  }, [conversation.state?.mode, conversation.state?.control_version, confirmation?.controlVersion])
  async function loadIdentity() {
    try { const me = await request('/api/v1/support/me'); setIdentity(me); setAuthenticated(true) }
    catch { setAuthenticated(false); setIdentity(null) }
  }
  useEffect(() => { loadIdentity() }, [])
  function ensureId() { const sid = sessionId || crypto.randomUUID(); activeSession.current = sid; localStorage.setItem(SESSION_KEY, sid); setSessionId(sid); return sid }
  function startNew() { sendVersion.current++; localStorage.removeItem(SESSION_KEY); setSessionId(''); setConfirmation(''); setInput(''); setNotice(''); setRetry(null); setBusy(false) }
  async function handoff() {
    sendVersion.current++
    const sid = ensureId()
    try { const state = await post(`/api/v1/sessions/${sid}/handoff`); if (activeSession.current === sid) { conversation.setState(state); setConfirmation(''); setBusy(false); setNotice('已进入人工队列，可以继续补充信息。') } }
    catch (e) { setNotice(e.message) }
  }
  async function resume() {
    try { conversation.setState(await post(`/api/v1/sessions/${sessionId}/resume-bot`)); setConfirmation(''); setNotice('已返回机器人，之前的业务操作不会自动重试。') }
    catch (e) { setNotice(e.message) }
  }
  async function send(e, confirmed = false, suppliedMessage = '', taskInput = {}) {
    e?.preventDefault()
    const message = suppliedMessage || (confirmed ? '确认' : input.trim())
    if (!message) return
    const generation = ++sendVersion.current
    const sid = ensureId(), version = control.current?.control_version ?? 0
    const candidate = { message, session_id: sid, ...taskInput, ...(confirmed && confirmation ? { confirmation_token: confirmation.token } : {}) }
    const { request_id: previousId, ...previous } = retry || {}
    const payload = JSON.stringify(previous) === JSON.stringify(candidate) ? retry : { ...candidate, request_id: crypto.randomUUID() }
    setRetry(payload); setBusy(true); setNotice(''); setConfirmation('')
    try {
      let data = await post('/api/v1/chat', payload)
      if (activeSession.current !== sid || generation !== sendVersion.current) return
      setInput(previous => previous.trim() === message ? '' : previous); setRetry(null)
      const expectedVersion = data.control_version ?? version
      if (data.job_id) {
        const deadline = Date.now() + 730000
        while (Date.now() < deadline) {
          if (activeSession.current !== sid || generation !== sendVersion.current || (control.current && (control.current.mode !== 'bot' || control.current.control_version > expectedVersion))) return
          const update = nextConversationUpdate(sid)
          try {
            const status = await request(`/api/v1/chat/${data.job_id}`)
            if (status.status === 'completed') { data = status.result; break }
            if (['failed', 'expired', 'interrupted', 'superseded'].includes(status.status)) throw new Error(status.error || '任务未完成，可转人工核实')
            await update.promise
          } finally { update.cancel() }
        }
        if (data.job_id) throw new Error('等待任务超时，可转人工核实处理结果')
      }
      if (activeSession.current !== sid || generation !== sendVersion.current) return
      if (data.support) conversation.setState(data.support)
      if (!data.suppressed && data.confirmation_token && (!control.current || control.current.control_version <= expectedVersion) && control.current?.mode === 'bot') setConfirmation({ token: data.confirmation_token, controlVersion: expectedVersion })
    } catch (e) { if (activeSession.current === sid && generation === sendVersion.current) setNotice(e.message) }
    finally { if (activeSession.current === sid && generation === sendVersion.current) setBusy(false) }
  }
  async function logout() { try { await post('/api/v1/auth/logout') } finally { startNew(); setIdentity(null); setAuthenticated(false); setStaffView(false) } }
  if (!authenticated) return <AuthScreen onSave={loadIdentity} />
  if (staffView && identity?.role !== 'customer') return <SupportConsole role={identity.role} onBack={() => setStaffView(false)} />
  const state = conversation.state
  return <main className="support-shell customer-shell"><header className="support-top"><div><small>小夕客服</small><h1>{state?.mode === 'human' ? '人工客服' : state?.mode === 'handoff_pending' ? '等待人工接入' : '智能咨询'}</h1></div><div className="support-toolbar">{identity?.role !== 'customer' && <button onClick={() => setStaffView(true)}>客服工作台</button>}<button onClick={startNew}>新建咨询</button><button onClick={logout}>退出</button></div></header>
    <section className="support-detail"><header><span>会话 {sessionId ? `#${sessionId.slice(0, 8)}` : '尚未开始'}</span><span>{identity?.user_id}</span></header>
      {state?.ticket && <div className="support-banner"><strong>工单 #{state.ticket.ticket_id.slice(0, 8)} · {labels[state.ticket.status]}</strong><span>{state.ticket.assignee ? `客服：${state.ticket.assignee}` : '消息已保存，等待客服领取。你可以继续补充信息。'}</span></div>}
      {(notice || conversation.error) && <div role="alert" className="support-alert">{notice || conversation.error}</div>}
      <Transcript {...conversation} />
      <TaskPanel tasks={conversation.tasks} question={conversation.taskQuestion} mode={state?.mode} busy={busy} onSend={(message, data) => send(null, false, message, data)} />
      <div className="support-toolbar">{(!state || state.mode === 'bot') ? <button onClick={handoff}>转人工客服</button> : <button onClick={resume}>{state.mode === 'handoff_pending' ? '取消排队，返回机器人' : '结束人工服务，返回机器人'}</button>}{busy && <span>正在处理… 可随时转人工</span>}</div>
      {confirmation && <div className="support-banner"><span>请核对上方操作内容，只有点击确认才会提交。</span><button onClick={e => send(e, true)} disabled={busy}>确认执行</button><button onClick={() => setConfirmation('')}>暂不执行</button></div>}
      <form className="support-compose" onSubmit={send}><textarea aria-label="咨询内容" maxLength={4000} value={input} onChange={e => setInput(e.target.value)} placeholder={state?.mode === 'bot' || !state ? '输入问题，也可以随时改口、暂停或取消任务' : '向人工客服补充信息'} /><button disabled={!input.trim()}>{retry ? '重试发送' : '发送消息'}</button></form>
    </section></main>
}
