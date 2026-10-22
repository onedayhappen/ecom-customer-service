import { test, expect } from '@playwright/test'

async function prepare(page, { status = 'prepared', canConfirm = true, confirm, review } = {}) {
  const operation = {
    operation_id: '9b581291-86dc-46e9-a7d8-44a16d5cbb52', status, can_confirm: canConfirm,
    confirmation_token: 'server-bound-confirmation-token', expires_at: Date.now() / 1000 + 600,
    review: { title: '申请整单退款', order_id: 'ORD-A', items: [{ name: '耳机', quantity: 1 }], quantity: 1,
      reason: '左耳没声音', amount_minor: 12999, amount_kind: 'order_reference', is_demo: true,
      impact: '本次仅登记申请，退款金额和优惠券处理等待审核。' },
  }
  if (review) operation.review = review
  const calls = [], messages = []
  await page.addInitScript(() => {
    window.EventSource = class { addEventListener() {} close() {} }
  })
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    const json = data => route.fulfill({ json: data })
    if (path.endsWith('/support/me')) return json({ user_id: 'alice', role: 'customer' })
    if (path.endsWith('/support-state')) return json({ mode: 'bot', control_version: 0 })
    if (path.endsWith('/transcript')) return json({ state: { mode: 'bot', control_version: 0 }, events: [], cursor: 0, tasks: [], next_before: null })
    if (path.startsWith('/api/v1/task-operations/') && req.method() === 'GET') return json([operation])
    if (path.startsWith('/api/v1/task-operations/') && req.method() === 'POST') {
      calls.push({ path, body: req.postDataJSON() })
      if (path.endsWith('/confirm') && confirm) return confirm(route, operation)
      operation.status = path.endsWith('/cancel') ? 'cancelled' : 'succeeded'
      operation.can_confirm = false
      return json(operation)
    }
    if (path === '/api/v1/task-messages') {
      messages.push(req.postDataJSON())
      return route.fulfill({ status: 202, json: { status: 'accepted' } })
    }
    if (path.startsWith('/api/v1/task-messages/')) return json({ processing: 'idle', messages: [], received_sequence: 0 })
    return json({})
  })
  await page.goto('/')
  await expect(page.getByRole('region', { name: '操作确认' })).toBeVisible()
  return { operation, calls, messages }
}

test('review binds fixed details; confirmation in flight allows new input and reload shows receipt', async ({ page }, testInfo) => {
  let release
  const held = new Promise(resolve => { release = resolve })
  const app = await prepare(page, { confirm: async (route, operation) => {
    operation.status = 'running'; operation.can_confirm = false
    await held
    operation.status = 'succeeded'
    await route.fulfill({ json: operation })
  } })
  await expect(page.getByText('订单金额（参考，非承诺退款金额）：CNY 129.99')).toBeVisible()
  await expect(page.getByText('耳机 × 1')).toBeVisible()
  await page.getByRole('button', { name: '确认这项操作' }).click()
  await expect.poll(() => app.calls.length).toBe(1)
  expect(Object.keys(app.calls[0].body).sort()).toEqual(['confirmation_token', 'session_id'])
  await page.getByRole('textbox', { name: '咨询内容' }).fill('另外一单查物流')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect.poll(() => app.messages.length).toBe(1)
  release()
  await expect(page.getByText(/已收到业务回执/)).toBeVisible()
  await page.reload()
  await expect(page.getByText(/已收到业务回执/)).toBeVisible()
  await expect(page.getByRole('button', { name: '确认这项操作' })).toHaveCount(0)
  expect(app.calls.length).toBe(1)
  await page.screenshot({ path: testInfo.outputPath('operation-receipt.png'), fullPage: true })
})

test('invalid confirmation is disabled while cancel remains available', async ({ page }) => {
  const app = await prepare(page, { canConfirm: false })
  await expect(page.getByRole('button', { name: '确认这项操作' })).toBeDisabled()
  await page.getByRole('button', { name: '暂不执行' }).click()
  await expect(page.getByText(/本次操作已取消/)).toBeVisible()
  expect(app.calls[0].path).toMatch(/\/cancel$/)
  expect(Object.keys(app.calls[0].body)).toEqual(['session_id'])
})

test('lost confirm response restores unknown state and only queries the original operation', async ({ page }) => {
  const app = await prepare(page, { confirm: async (route, operation) => {
    operation.status = 'unknown'; operation.can_confirm = false
    await route.abort('failed')
  } })
  await page.getByRole('button', { name: '确认这项操作' }).click()
  await expect(page.getByText(/请刷新操作状态后再决定下一步/)).toBeVisible()
  await page.reload()
  await expect(page.getByText(/结果待核实，不能重复提交/)).toBeVisible()
  await expect(page.getByRole('button', { name: '确认这项操作' })).toHaveCount(0)
  await page.getByRole('button', { name: '核实操作结果' }).click()
  await expect(page.getByText(/已收到业务回执/)).toBeVisible()
  expect(app.calls.map(c => c.path.split('/').at(-1))).toEqual(['confirm', 'refresh'])
})

test('core refund review uses decimal money and selected item quantities', async ({ page }) => {
  await prepare(page, { review: { title: '申请仅退款', order_id: 'NEW-ORDER', currency: 'CNY',
    amount: '10.00', amount_kind: 'refund_estimate', quantity: 1, items: [{ name: '耳机', quantity: 1 }],
    impact: '申请提交不等于退款成功' } })
  await expect(page.getByText('申请退款金额（待审核）：CNY 10.00')).toBeVisible()
  await expect(page.getByText('耳机 × 1')).toBeVisible()
})

test('private invoice and address reviews show the exact requested destination', async ({ page }) => {
  await prepare(page, { review: { title: '提交开票申请', order_id: 'NEW-ORDER', invoice_type: 'company',
    invoice_title: '示例公司', tax_number: 'TEST-TAX',
    address: { region: ['省', '市', '区'], detail: '新地址', recipient_name: '张三', phone: '13800000000' } } })
  await expect(page.getByText('抬头：示例公司；类型：企业；税号：TEST-TAX')).toBeVisible()
  await expect(page.getByText(/目标收货地址：省 \/ 市 \/ 区 新地址/)).toBeVisible()
  await expect(page.getByText(/共.*件/)).toHaveCount(0)
})
