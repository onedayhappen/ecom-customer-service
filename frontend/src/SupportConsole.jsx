import { useEffect, useState } from 'react'
import { labels, post, request, Transcript, useConversation } from './supportClient'

function Ticket({ ticketId, role, onChanged }) {
  const [detail, setDetail] = useState(null), [error, setError] = useState('')
  const [text, setText] = useState(''), [internal, setInternal] = useState(false)
  const [agents, setAgents] = useState([]), [assignee, setAssignee] = useState(''), [busy, setBusy] = useState(false)
  const [pendingMessage, setPendingMessage] = useState(null)
  const conversation = useConversation(detail?.ticket.session_id, true, ticketId)
  async function refresh() { setDetail(await request(`/api/v1/support/tickets/${ticketId}`)) }
  useEffect(() => { let active = true; request(`/api/v1/support/tickets/${ticketId}`).then(d => { if (active) setDetail(d) }).catch(e => { if (active) setError(e.message) }); request('/api/v1/support/staff').then(d => { if (active) setAgents(d.filter(a => a.enabled)) }).catch(e => { if (active) setError(e.message) }); return () => { active = false } }, [ticketId])
  async function action(action) {
    setBusy(true); setError('')
    try { await post(`/api/v1/support/tickets/${ticketId}/transition`, { action, version: conversation.state?.ticket?.version || detail.ticket.version, assignee }); await refresh(); await onChanged() }
    catch (e) { setError(e.message); await refresh().catch(() => {}) } finally { setBusy(false) }
  }
  async function send(e) {
    e.preventDefault(); if (!text.trim()) return
    setBusy(true); setError('')
    const message = pendingMessage?.content === text && pendingMessage?.internal === internal ? pendingMessage : { content: text, internal, client_message_id: crypto.randomUUID() }
    setPendingMessage(message)
    try { await post(`/api/v1/support/tickets/${ticketId}/messages`, message); setText(''); setPendingMessage(null); await refresh() }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  if (!detail) return <div className="support-card">{error || '加载工单…'}</div>
  const state = conversation.state || detail.state
  const current = state.ticket?.ticket_id === ticketId && state.mode === 'human'
  const active = current && ['in_progress', 'waiting_customer'].includes(state.ticket?.status)
  return <section className="support-detail">
    <header><div><small>工单 #{ticketId.slice(0, 8)}</small><h2>{labels[state.ticket?.status || detail.ticket.status]}</h2></div><span>负责人：{state.ticket?.assignee || detail.ticket.assignee || '未分配'}</span></header>
    {(error || conversation.error) && <div role="alert" className="support-alert">{error || conversation.error}</div>}
    <details className="support-card"><summary>交接摘要与业务操作</summary><button onClick={() => refresh().catch(e => setError(e.message))}>刷新业务结果</button><p style={{ whiteSpace: 'pre-wrap' }}>{detail.ticket.summary}</p><small>触发原因：{detail.ticket.reason}</small>{detail.operations.map(op => <div className="support-operation" key={op.operation_id}><strong>{op.tool} · {op.status}</strong><small>{op.operation_id}</small>{op.result && <pre>{op.result}</pre>}{op.status === 'running' && <p>操作可能仍在下游执行。先核实进程与业务结果，不能直接重试。</p>}{role === 'admin' && ['running', 'unknown'].includes(op.status) && <Reconcile operation={op} onDone={refresh} />}</div>)}{detail.evidence?.map((e, i) => <details key={i}><summary>{e.tool} · 查询依据</summary><pre>{JSON.stringify(e, null, 2)}</pre></details>)}</details>
    <Transcript messages={conversation.messages} />
    <div className="support-toolbar"><button disabled={busy || !active} onClick={() => action('waiting_customer')}>等待用户</button><button disabled={busy || !active} onClick={() => action('resolved')}>标记解决</button><button disabled={busy || !active} onClick={() => action('requeue')}>退回队列</button>{role === 'admin' && <><select aria-label="转派客服" value={assignee} onChange={e => setAssignee(e.target.value)}><option value="">选择客服</option>{agents.map(a => <option key={a.user_id}>{a.user_id}</option>)}</select><button disabled={busy || !assignee || !['human', 'handoff_pending'].includes(state.mode)} onClick={() => action('assign')}>转派</button></>}</div>
    <form onSubmit={send} className="support-compose"><label><input type="checkbox" checked={internal} onChange={e => setInternal(e.target.checked)} />内部备注（用户不可见）</label><textarea aria-label="客服回复" maxLength={4000} value={text} onChange={e => setText(e.target.value)} placeholder="输入回复或内部备注" /><button disabled={busy || !current || !text.trim() || state.ticket?.status === 'resolved'}>{busy ? '提交中…' : internal ? '保存备注' : '发送回复'}</button></form>
  </section>
}

function Reconcile({ operation, onDone }) {
  const [evidence, setEvidence] = useState(''), [error, setError] = useState(''), [stopped, setStopped] = useState(false), [busy, setBusy] = useState(false)
  async function submit(status) {
    setBusy(true)
    try { await post(`/api/v1/support/operations/${operation.operation_id}/${operation.status === 'running' ? 'mark-unknown' : 'reconcile'}`, { status, evidence, execution_stopped: stopped }); setEvidence(''); await onDone() }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return <div><textarea aria-label="核实依据" value={evidence} onChange={e => setEvidence(e.target.value)} placeholder="填写进程终止或下游业务查询回执等核实依据（至少10字）" />{operation.status === 'running' ? <><label><input type="checkbox" checked={stopped} onChange={e => setStopped(e.target.checked)} />已确认原执行进程停止，且不会继续提交业务操作</label><button disabled={busy || !stopped || evidence.trim().length < 10} onClick={() => submit()}>转为结果待核实</button></> : <><button disabled={busy || evidence.trim().length < 10} onClick={() => submit('succeeded')}>已核实成功</button><button disabled={busy || evidence.trim().length < 10} onClick={() => submit('failed')}>已核实未执行</button></>}{error && <p role="alert">{error}</p>}</div>
}

function StaffSettings() {
  const [user, setUser] = useState(''), [role, setRole] = useState('agent'), [enabled, setEnabled] = useState(true), [notice, setNotice] = useState('')
  async function save(e) { e.preventDefault(); try { await post(`/api/v1/support/staff/${encodeURIComponent(user)}`, { role, enabled }); setNotice('权限已更新；该用户仍需拥有已配置的登录账号。') } catch (e) { setNotice(e.message) } }
  return <details className="support-card"><summary>客服权限管理</summary><form className="support-staff-form" onSubmit={save}><input aria-label="客服用户名" placeholder="已有登录用户名" value={user} onChange={e => setUser(e.target.value)} required /><select aria-label="角色" value={role} onChange={e => setRole(e.target.value)}><option value="agent">客服</option><option value="admin">管理员</option></select><label><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用</label><button>保存权限</button><p role="status">{notice}</p></form></details>
}

export default function SupportConsole({ role, onBack }) {
  const [view, setView] = useState('queue'), [items, setItems] = useState([]), [selected, setSelected] = useState(''), [error, setError] = useState(''), [offset, setOffset] = useState(0)
  async function refresh() { setItems(await request(`/api/v1/support/tickets?view=${view}&offset=${offset}`)) }
  useEffect(() => { let active = true; async function load() { try { const data = await request(`/api/v1/support/tickets?view=${view}&offset=${offset}`); if (active) setItems(data) } catch (e) { if (active) setError(e.message) } } load(); const timer = setInterval(load, 5000); return () => { active = false; clearInterval(timer) } }, [view, offset])
  async function claim(ticket) { try { await post(`/api/v1/support/tickets/${ticket.ticket_id}/transition`, { action: 'claim', version: ticket.version }); setSelected(ticket.ticket_id); setError(''); await refresh() } catch (e) { setError(e.message); await refresh().catch(() => {}) } }
  return <main className="support-shell"><header className="support-top"><div><small>小夕客服</small><h1>人工服务工作台</h1></div><button onClick={onBack}>返回用户端</button></header>{error && <div className="support-alert" role="alert">{error}</div>}<div className="support-columns"><aside className="support-queue"><nav>{['queue', 'mine', ...(role === 'admin' ? ['all'] : [])].map(v => <button className={view === v ? 'selected' : ''} key={v} onClick={() => { setView(v); setOffset(0) }}>{({ queue: '待接单', mine: '我的工单', all: '全部' })[v]}</button>)}</nav>{items.length === 0 && <p className="support-muted">当前没有工单</p>}{items.map(t => <article className="support-ticket" key={t.ticket_id}><strong>#{t.ticket_id.slice(0, 8)} · {labels[t.status]}</strong><small>{t.reason} · {new Date(t.created_at).toLocaleString()}</small><span>等待/创建于 {Math.max(0, Math.floor((Date.now() - Date.parse(t.created_at)) / 60000))} 分钟前</span>{t.first_reply_at && <small>首次人工回复：{Math.round((Date.parse(t.first_reply_at) - Date.parse(t.created_at)) / 1000)} 秒</small>}<button onClick={() => t.status === 'queued' ? claim(t) : setSelected(t.ticket_id)}>{t.status === 'queued' ? '领取工单' : '打开'}</button></article>)}<div className="support-toolbar"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button><button disabled={items.length < 30} onClick={() => setOffset(offset + 30)}>下一页</button></div>{role === 'admin' && <StaffSettings />}</aside>{selected ? <Ticket key={selected} ticketId={selected} role={role} onChanged={refresh} /> : <section className="support-card support-empty"><h2>选择工单开始处理</h2><p>领取后可以查看对话、交接摘要和业务执行状态。</p></section>}</div></main>
}
