const statusLabels = {
  queued: '待处理', ready: '即将处理', running: '处理中', waiting_input: '等待补充',
  waiting_confirmation: '等待确认', paused: '已暂停', completed: '已完成', cancelled: '已取消', needs_human: '待人工核实',
}

export default function TaskPanel({ tasks = [] }) {
  if (!tasks.length) return null
  const visible = [...tasks.filter(t => !['completed', 'cancelled'].includes(t.status)),
    ...tasks.filter(t => ['completed', 'cancelled'].includes(t.status)).slice(-10)]
  return <section className="task-panel" aria-label="历史事项记录">
    <header><strong>历史事项记录</strong><small>仅供核对</small></header>
    {visible.map(task => <article className={`task-item task-${task.status}`} key={task.task_id}>
      <div className="task-heading"><strong>{task.title}</strong><span>{statusLabels[task.status] || task.status}</span></div>
      <p>{task.request_text}</p>
      {Object.keys(task.slots || {}).length > 0 && <small>{Object.values(task.slots).join(' · ')}</small>}
      {task.question && <p>{task.question.prompt}</p>}
    </article>)}
  </section>
}
