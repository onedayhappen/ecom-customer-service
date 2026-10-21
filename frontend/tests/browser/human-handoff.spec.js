import { test, expect } from '@playwright/test'

test('staff handover update retries with the same id; resolving does not enable the robot', async ({ page }, testInfo) => {
  const sid = '584de576-eeba-4d9d-9234-e5837d8e2e1f', tid = '464de576-eeba-4d9d-9234-e5837d8e2e1f'
  const task = { task_id: '374de576-eeba-4d9d-9234-e5837d8e2e1f', goal: '耳机退货', version: 1, details: { order_id: 'ORD-A' }, waiting_for: null }
  const ticket = { ticket_id: tid, session_id: sid, status: 'queued', version: 1, created_at: new Date().toISOString(), reason: 'customer_requested', summary: '客户希望核实耳机售后。' }
  const state = { mode: 'handoff_pending', control_version: 1, ticket, cursor: 1 }
  const calls = []
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const json = data => route.fulfill({ json: data })
    if (path.endsWith('/support/me')) return json({ user_id: 'agent1', role: 'agent' })
    if (path.endsWith('/support/staff')) return json([{ user_id: 'agent1', enabled: true }])
    if (path === '/api/v1/support/tickets') return json([ticket])
    if (path.endsWith('/transition')) {
      const action = request.postDataJSON().action
      ticket.status = action === 'claim' ? 'in_progress' : action
      ticket.assignee = 'agent1'; ticket.version++; state.mode = 'human'; state.cursor++
      return json(state)
    }
    if (path === `/api/v1/support/tickets/${tid}/tasks/${task.task_id}`) {
      calls.push(request.postDataJSON())
      if (calls.length === 1) { task.details.reason = calls[0].details.reason; task.version++; state.cursor++; return route.abort('failed') }
      return json({ task })
    }
    if (path === `/api/v1/support/tickets/${tid}`) return json({
      ticket, state, operations: [], evidence: [], tasks: [], task_handovers: [{ active: true, task_id: task.task_id,
        handover: { task, customer_statements: [{ session_id: sid, sequence: 1, content: '耳机左耳没声音', created_at: ticket.created_at }],
          observed_evidence: [], operations: [], note: '人工说明不授予额外权益。' } }],
    })
    if (path.endsWith('/support-state')) return json(path.includes(sid) ? state : { mode: 'bot', cursor: 0 })
    if (path.endsWith('/transcript')) return json({ state: path.includes(sid) ? state : { mode: 'bot', cursor: 0 }, events: [], cursor: state.cursor, tasks: [] })
    if (path.startsWith('/api/v1/task-operations/')) return json([])
    if (path.startsWith('/api/v1/task-messages/')) return json({ processing: 'idle', messages: [], received_sequence: 0 })
    return json({})
  })
  await page.goto('/')
  await page.getByRole('button', { name: '客服工作台' }).click()
  await page.getByRole('button', { name: '领取工单' }).click()
  await expect(page.getByRole('region', { name: '事项交接记录' })).toContainText('ORD-A')
  await page.getByRole('textbox', { name: '人工补充内容' }).fill('左耳没有声音')
  await page.getByRole('button', { name: '保存事项更新' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('button', { name: '保存事项更新' }).click()
  await expect.poll(() => calls.length).toBe(2)
  expect(calls[0]).toEqual(calls[1])
  await expect(page.getByRole('region', { name: '事项交接记录' })).toContainText('左耳没有声音')
  await page.getByRole('button', { name: '标记解决' }).click()
  await expect(page.getByRole('button', { name: '保存事项更新' })).toHaveCount(0)
  expect(state.mode).toBe('human')
  await page.screenshot({ path: testInfo.outputPath('human-handoff.png'), fullPage: true })
})
