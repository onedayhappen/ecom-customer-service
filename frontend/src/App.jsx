import { useEffect, useState } from 'react'
import { ArrowUpRight, Bot, CircleAlert, Headphones, ShieldCheck, Sparkles, UserRound, Zap } from 'lucide-react'
import SupportConsole from './SupportConsole'
import TaskConversation from './TaskConversation'
import { post, request } from './supportClient'

function AuthScreen({ onSave }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [mode, setMode] = useState('login')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  async function submit(event) {
    event.preventDefault(); setError(''); setLoading(true)
    if (mode === 'register' && password !== confirmPassword) { setError('两次输入的密码不一致'); setLoading(false); return }
    try { await request(`/api/v1/auth/${mode}`, { method: 'POST', body: JSON.stringify({ username, password }) }); onSave() }
    catch (err) { setError(err.message) } finally { setLoading(false) }
  }
  return <main className="auth-page"><div className="auth-orb auth-orb-one" /><div className="auth-orb auth-orb-two" /><section className="auth-shell">
    <div className="auth-showcase"><div className="brand-lockup"><span className="brand-mark"><Bot size={23} /></span><span>小夕客服</span></div><div className="showcase-copy"><span className="eyebrow"><Sparkles size={14} /> 新一代智能服务台</span><h1>让每一次咨询，<br /><em>都有及时回应。</em></h1><p>订单、物流、售后与商品问题，交给小夕快速处理。需要人工协助时，我们也会无缝接力。</p></div><div className="showcase-features"><div><span className="feature-icon"><Zap size={16} /></span><span><strong>快速响应</strong><small>平均 10 秒内给出方案</small></span></div><div><span className="feature-icon"><ShieldCheck size={16} /></span><span><strong>安全可靠</strong><small>你的会话受到加密保护</small></span></div></div><div className="showcase-footer"><span className="live-dot" />服务系统运行正常 <span>·</span> 7 × 24 小时在线</div></div>
    <div className="auth-card"><div className="auth-card-heading"><span className="auth-card-icon"><Headphones size={20} /></span><div><h2>{mode === 'login' ? '欢迎回来' : '创建账号'}</h2><p>{mode === 'login' ? '登录你的客服工作台' : '注册后即可进入客服工作台'}</p></div></div><form onSubmit={submit}><label htmlFor="login-username">用户名</label><div className="input-wrap"><UserRound size={17} /><input id="login-username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="字母开头，3–32 位" autoComplete="username" required /></div><label htmlFor="login-password">密码</label><div className="input-wrap"><ShieldCheck size={17} /><input id="login-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder={mode === 'register' ? '至少 12 位' : '请输入密码'} autoComplete={mode === 'register' ? 'new-password' : 'current-password'} minLength={mode === 'register' ? 12 : 1} required /></div>{mode === 'register' && <><label htmlFor="register-confirm">确认密码</label><div className="input-wrap"><ShieldCheck size={17} /><input id="register-confirm" type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} placeholder="再次输入密码" autoComplete="new-password" required /></div></>}{error && <div className="form-error"><CircleAlert size={16} />{error}</div>}<button className="auth-submit" type="submit" disabled={loading}>{loading ? '请稍候…' : <><span>{mode === 'login' ? '进入工作台' : '注册并进入'}</span><ArrowUpRight size={17} /></>}</button></form><button className="auth-switch" type="button" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); setPassword(''); setConfirmPassword('') }}>{mode === 'login' ? '没有账号？立即注册' : '已有账号？返回登录'}</button></div>
  </section></main>
}

export default function App() {
  const [identity, setIdentity] = useState(null)
  const [staffView, setStaffView] = useState(false)
  async function loadIdentity() {
    try { setIdentity(await request('/api/v1/support/me')) }
    catch { setIdentity(null) }
  }
  useEffect(() => { loadIdentity() }, [])
  async function logout() {
    try { await post('/api/v1/auth/logout') }
    finally { setIdentity(null); setStaffView(false) }
  }
  if (!identity) return <AuthScreen onSave={loadIdentity} />
  if (staffView && identity.role !== 'customer') return <SupportConsole role={identity.role} onBack={() => setStaffView(false)} />
  return <TaskConversation key={identity.user_id} identity={identity} onLogout={logout} onStaff={() => setStaffView(true)} />
}
