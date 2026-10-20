import { test, expect } from '@playwright/test'

async function prepare(page, { onPost } = {}) {
  const events = [], requests = [], received = new Map()
  const state = { mode: 'bot', control_version: 0 }
  await page.addInitScript(() => {
    window.streams = []
    window.EventSource = class {
      constructor() { this.listeners = {}; window.streams.push(this) }
      addEventListener(kind, listener) { this.listeners[kind] = listener }
      close() { this.closed = true }
    }
  })
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    const json = data => route.fulfill({ json: data })
    if (path.endsWith('/support/me')) return json({ user_id: 'alice', role: 'customer' })
    if (path.endsWith('/support-state')) return json(state)
    if (path.endsWith('/transcript')) return json({ state, events, cursor: events.at(-1)?.sequence || 0, tasks: [], next_before: null })
    if (path === '/api/v1/task-messages' && req.method() === 'POST') {
      const body = req.postDataJSON()
      requests.push(body)
      if (!received.has(body.request_id)) {
        const sequence = events.length + 1
        received.set(body.request_id, { ...body, sequence })
        events.push({ kind: 'message', sequence, actor: 'alice', created_at: new Date().toISOString(),
          payload: { role: 'user', content: body.message, request_id: body.request_id } })
      }
      if (onPost && await onPost(route, body, requests.length)) return
      return route.fulfill({ status: 202, json: { ...received.get(body.request_id), status: 'accepted' } })
    }
    if (path.startsWith('/api/v1/task-messages/')) return json({
      received_sequence: events.length, associated_sequence: 0, messages: [], processing: events.length ? 'processing' : 'idle',
    })
    return json({})
  })
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: '咨询内容' })).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.streams.some(s => !s.closed))).toBeTruthy()
  async function notify(batch = events, cursor = batch.at(-1)?.sequence || 0) {
    await page.evaluate(({ events, cursor, state }) => {
      window.streams.filter(s => !s.closed).forEach(s => s.listeners.update?.({ data: JSON.stringify({ events, cursor, state }) }))
    }, { events: batch, cursor, state })
  }
  return { events, requests, received, notify }
}

test('another input sends while the first HTTP request is pending; late response preserves the draft', async ({ page }) => {
  let release
  const held = new Promise(resolve => { release = resolve })
  const app = await prepare(page, { onPost: async (_, __, index) => { if (index === 1) await held; return false } })
  const input = page.getByRole('textbox', { name: '咨询内容' })
  await input.fill('ORD-A 查物流')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect.poll(() => app.requests.length).toBe(1)
  await input.fill('ORD-B 也查一下')
  await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect.poll(() => app.requests.length).toBe(2)
  await input.fill('还在输入的补充')
  release()
  await app.notify()
  await expect(input).toHaveValue('还在输入的补充')
  await expect(page.locator('.support-message')).toHaveCount(2)
  expect(new Set(app.requests.map(r => r.request_id)).size).toBe(2)
  expect(new Set(app.requests.map(r => r.session_id)).size).toBe(1)
  await expect(page.getByRole('status')).toContainText('消息已接收')
})

test('an unknown HTTP outcome retries with the same id and reload recovers the single saved message', async ({ page }) => {
  const app = await prepare(page, { onPost: async (route, _, index) => {
    if (index === 1) { await route.abort('failed'); return true }
    return false
  } })
  await page.getByRole('textbox', { name: '咨询内容' }).fill('不是 A，是 B')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect(page.getByRole('button', { name: '重试这条消息' })).toBeVisible()
  await page.getByRole('button', { name: '重试这条消息' }).click()
  await expect.poll(() => app.requests.length).toBe(2)
  expect(app.requests[0]).toEqual(app.requests[1])
  await app.notify()
  await app.notify()
  await expect(page.locator('.support-message')).toHaveCount(1)
  await page.reload()
  await expect(page.locator('.support-message')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '重试这条消息' })).toHaveCount(0)
})

test('fast reply and slow follow-up survive duplicate and out-of-order updates plus refresh', async ({ page }, testInfo) => {
  const app = await prepare(page)
  for (const [sequence, content] of [[1, 'ORD-A 已发货'], [2, 'ORD-B 待发货']]) {
    app.events.push({ kind: 'message', sequence, actor: 'bot', created_at: new Date().toISOString(),
      payload: { event_id: `reply-${sequence}`, role: 'assistant', content } })
    await app.notify()
    await expect(page.locator('.support-message')).toHaveCount(sequence)
  }
  await app.notify()
  await app.notify(app.events.slice(0, 1), 1)
  await expect(page.locator('.support-message')).toHaveCount(2)
  await page.reload()
  await expect(page.locator('.support-message')).toHaveCount(2)
  await expect(page.getByRole('button', { name: '确认这项操作' })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('continuous-input.png'), fullPage: true })
})
