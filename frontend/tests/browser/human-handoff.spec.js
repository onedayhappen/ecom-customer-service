import { test, expect } from '@playwright/test'

test('staff handover update retries with the same id; resolving does not enable the robot', async ({
  page,
}, testInfo) => {
  const sid = '584de576-eeba-4d9d-9234-e5837d8e2e1f',
    tid = '464de576-eeba-4d9d-9234-e5837d8e2e1f'
  const task = {
    task_id: '374de576-eeba-4d9d-9234-e5837d8e2e1f',
    goal: '耳机退货',
    version: 1,
    details: { order_id: 'ORD-A' },
    waiting_for: null,
  }
  const ticket = {
    ticket_id: tid,
    session_id: sid,
    status: 'queued',
    version: 1,
    created_at: new Date().toISOString(),
    reason: 'customer_requested',
    summary: '客户希望核实耳机售后。',
  }
  const state = { mode: 'handoff_pending', control_version: 1, ticket, cursor: 1 }
  const calls = []
  await page.addInitScript(() => {
    window.EventSource = class {
      addEventListener() {}
      close() {}
    }
  })
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname
    const json = (data) => route.fulfill({ json: data })
    if (path.endsWith('/support/me')) return json({ user_id: 'agent1', role: 'agent' })
    if (path.endsWith('/support/staff')) return json([{ user_id: 'agent1', enabled: true }])
    if (path === '/api/v1/support/tickets') return json([ticket])
    if (path.endsWith('/transition')) {
      const action = request.postDataJSON().action
      ticket.status = action === 'claim' ? 'in_progress' : action
      ticket.assignee = 'agent1'
      ticket.version++
      state.mode = 'human'
      state.cursor++
      return json(state)
    }
    if (path === `/api/v1/support/tickets/${tid}/tasks/${task.task_id}`) {
      calls.push(request.postDataJSON())
      if (calls.length === 1) {
        task.details.reason = calls[0].details.reason
        task.version++
        state.cursor++
        return route.abort('failed')
      }
      return json({ task })
    }
    if (path === `/api/v1/support/tickets/${tid}`)
      return json({
        ticket,
        state,
        operations: [],
        evidence: [],
        tasks: [],
        task_handovers: [
          {
            active: true,
            task_id: task.task_id,
            handover: {
              task,
              customer_statements: [
                {
                  session_id: sid,
                  sequence: 1,
                  content: '耳机左耳没声音',
                  created_at: ticket.created_at,
                },
              ],
              observed_evidence: [
                {
                  event_id: 'candidate-receipt',
                  tool: 'find_orders',
                  evidence_kind: 'candidates',
                  success: true,
                  observed_at: '2026-10-29T01:00:00Z',
                  result_observed_at: '2026-10-29T00:59:59Z',
                  session_id: sid,
                  sequence: 2,
                  task_version: 1,
                  query_scope: { query: '耳机' },
                  sources: [{ kind: 'mysql', tables: ['orders'] }],
                  facts: { items: [{ order_id: 'ORD-A' }, { order_id: 'ORD-B' }], total: 3 },
                  limitations: ['渠道成功不代表已核实银行卡入账'],
                  has_more: true,
                },
                { event_id: 'empty-receipt', success: false, facts: {} },
              ],
              operations: [
                {
                  operation_id: 'op1',
                  tool: 'prepare_after_sale',
                  status: 'succeeded',
                  result: { status: 'submitted', after_sale_id: 'SALE-A' },
                  note: '申请提交不代表退款到账。',
                },
              ],
              image_observations: [
                {
                  attachment_id: 'image-a',
                  session_id: sid,
                  sequence: 1,
                  status: 'completed',
                  observations: [
                    {
                      observation_id: 'vision-a',
                      created_at: ticket.created_at,
                      result: { summary: '杯子边缘有裂纹', uncertainties: ['无法确定破损原因'] },
                    },
                  ],
                },
              ],
              note: '人工说明不授予额外权益。',
            },
          },
        ],
      })
    if (path.endsWith('/support-state'))
      return json(path.includes(sid) ? state : { mode: 'bot', cursor: 0 })
    if (path.endsWith('/transcript'))
      return json({
        state: path.includes(sid) ? state : { mode: 'bot', cursor: 0 },
        events: [],
        cursor: state.cursor,
        tasks: [],
      })
    if (path.startsWith('/api/v1/task-operations/')) return json([])
    if (path.startsWith('/api/v1/task-messages/'))
      return json({ processing: 'idle', messages: [], received_sequence: 0 })
    return json({})
  })
  await page.goto('/')
  await page.getByRole('button', { name: '客服工作台' }).click()
  await page.getByRole('button', { name: '领取工单' }).click()
  await expect(page.getByRole('region', { name: '事项交接记录' })).toContainText('ORD-A')
  await page.getByText('查询依据与操作回执', { exact: true }).click()
  await expect(page.getByText('候选记录（尚未选定）', { exact: false })).toBeVisible()
  await expect(page.getByText('本摘要仅覆盖部分结果，请核对分页、截断和缺失范围。')).toBeVisible()
  await expect(
    page.getByText('摘要未载入可展示事实，不代表没有业务记录；请核对原始回执。'),
  ).toBeVisible()
  await expect(page.getByText('申请提交不代表退款到账。')).toBeVisible()
  await expect(page.getByText('渠道成功不代表已核实银行卡入账', { exact: false })).toBeVisible()
  await page.getByText('图片观察（待业务核验）', { exact: true }).click()
  await expect(page.getByText('杯子边缘有裂纹', { exact: false })).toBeVisible()
  await expect(page.getByText('无法确定破损原因', { exact: false })).toBeVisible()
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
