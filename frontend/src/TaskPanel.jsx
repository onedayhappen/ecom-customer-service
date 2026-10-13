import { useState } from 'react'

const statusLabels = {
  queued: '待处理', ready: '即将处理', running: '处理中', waiting_input: '等待补充',
  waiting_confirmation: '等待确认', paused: '已暂停', completed: '已完成', cancelled: '已取消', needs_human: '待人工核实',
}

function Question({ question, task, onSend, disabled }) {
  const [answer, setAnswer] = useState('')
  function submit(value) {
    if (!value.trim()) return
    onSend(value, { task_action: 'answer', question_id: question.question_id,
      ...(task ? { task_id: task.task_id, task_version: task.version } : {}) })
  }
  return <section className="task-question" aria-label="客服提问">
    <strong>{question.prompt}</strong>
    <div className="support-toolbar">{question.options?.map(option => <button key={option.value} disabled={disabled} onClick={() => submit(option.value)}>{option.label}</button>)}</div>
    <form onSubmit={e => { e.preventDefault(); submit(answer) }}>
      <input aria-label="补充信息" value={answer} maxLength={2000} onChange={e => setAnswer(e.target.value)} placeholder="也可以直接输入答案" disabled={disabled} />
      <button disabled={disabled || !answer.trim()}>提交补充</button>
    </form>
  </section>
}

export default function TaskPanel({ tasks = [], question, onSend, mode, readOnly = false, busy = false }) {
  if (!tasks.length && !question) return null
  const actionable = !readOnly && mode === 'bot'
  const pending = tasks.filter(t => !['completed', 'cancelled'].includes(t.status))
  const visible = [...pending, ...tasks.filter(t => ['completed', 'cancelled'].includes(t.status)).slice(-10)]
  function action(task, verb) {
    onSend(`${({ resume: '继续', pause: '暂停', cancel: '取消' })[verb]}${task.title}`, {
      task_action: verb, task_id: task.task_id, task_version: task.version,
    })
  }
  return <section className="task-panel" aria-label="待办诉求">
    <header><strong>待办诉求</strong><small>{pending.length} 项待处理</small></header>
    {question && !readOnly && <Question key={question.question_id} question={question} onSend={onSend} disabled={!actionable || busy} />}
    {visible.map(task => <article className={`task-item task-${task.status}`} key={task.task_id}>
      <div className="task-heading"><strong>{task.title}</strong><span>{statusLabels[task.status]}</span></div>
      <p>{task.request_text}</p>
      {Object.entries(task.slots).length > 0 && <small>{Object.values(task.slots).join(' · ')}</small>}
      {(task.status === 'waiting_input' || readOnly) && task.question && <Question key={task.question.question_id} question={task.question} task={task} onSend={onSend} disabled={!actionable || busy} />}
      {!['completed', 'cancelled'].includes(task.status) && actionable && <div className="support-toolbar">
        {['queued', 'paused', 'needs_human', 'waiting_confirmation'].includes(task.status) && <button disabled={busy} onClick={() => action(task, 'resume')}>{task.status === 'waiting_confirmation' ? '重新核对' : '继续'}</button>}
        {!['paused', 'queued', 'needs_human'].includes(task.status) && <button onClick={() => action(task, 'pause')}>暂停</button>}
        <button onClick={() => action(task, 'cancel')}>取消此任务</button>
      </div>}
    </article>)}
  </section>
}
