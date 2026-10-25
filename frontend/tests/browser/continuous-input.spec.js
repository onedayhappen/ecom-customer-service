import { test, expect } from '@playwright/test'

async function prepare(page, { onPost } = {}) {
  const events = [],
    requests = [],
    received = new Map()
  const state = { mode: 'bot', control_version: 0 }
  await page.addInitScript(() => {
    window.streams = []
    window.EventSource = class {
      constructor() {
        this.listeners = {}
        window.streams.push(this)
      }
      addEventListener(kind, listener) {
        this.listeners[kind] = listener
      }
      close() {
        this.closed = true
      }
    }
  })
  await page.route('**/api/v1/**', async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname
    const json = (data) => route.fulfill({ json: data })
    if (path.endsWith('/support/me')) return json({ user_id: 'alice', role: 'customer' })
    if (path.endsWith('/support-state')) return json(state)
    if (path.endsWith('/transcript'))
      return json({
        state,
        events,
        cursor: events.at(-1)?.sequence || 0,
        tasks: [],
        next_before: null,
      })
    if (path === '/api/v1/task-messages' && req.method() === 'POST') {
      const body = req.postDataJSON()
      requests.push(body)
      if (!received.has(body.request_id)) {
        const sequence = events.length + 1
        received.set(body.request_id, { ...body, sequence })
        events.push({
          kind: 'message',
          sequence,
          actor: 'alice',
          created_at: new Date().toISOString(),
          payload: { role: 'user', content: body.message, request_id: body.request_id },
        })
      }
      if (onPost && (await onPost(route, body, requests.length))) return
      return route.fulfill({
        status: 202,
        json: { ...received.get(body.request_id), status: 'accepted' },
      })
    }
    if (path.startsWith('/api/v1/task-messages/'))
      return json({
        received_sequence:
          events.filter((event) => event.payload.role === 'user').at(-1)?.sequence || 0,
        associated_sequence: 0,
        messages: [],
        processing: events.length ? 'processing' : 'idle',
      })
    return json({})
  })
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: '咨询内容' })).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.streams.some((s) => !s.closed))).toBeTruthy()
  async function notify(batch = events, cursor = batch.at(-1)?.sequence || 0) {
    await page.evaluate(
      ({ events, cursor, state }) => {
        window.streams
          .filter((s) => !s.closed)
          .forEach((s) => s.listeners.update?.({ data: JSON.stringify({ events, cursor, state }) }))
      },
      { events: batch, cursor, state },
    )
  }
  return { events, requests, received, notify }
}

test('typing ends on a reply even before HTTP acknowledgement, and tracks later questions', async ({
  page,
}, testInfo) => {
  let release
  const held = new Promise((resolve) => {
    release = resolve
  })
  const app = await prepare(page, {
    onPost: async (_, __, index) => {
      if (index === 1) await held
      return false
    },
  })
  const input = page.getByRole('textbox', { name: '咨询内容' })
  const typing = page.getByRole('status', { name: '优优正在输入' })
  await expect(typing).toHaveCount(0)
  await input.fill('查一下订单')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(1)
  await expect(typing).toBeVisible()
  await expect(typing.getByRole('img', { name: '客服头像' })).toBeVisible()
  await expect(typing.locator('.message-sender')).toHaveText('优优')
  await expect(typing.locator('.message-bubble')).toContainText('正在输入中')
  await app.notify()
  await page.screenshot({ path: testInfo.outputPath('typing.png'), fullPage: true })
  const reply = (sequence, received_sequence) => ({
    kind: 'message',
    sequence,
    actor: 'bot',
    created_at: new Date().toISOString(),
    payload: { role: 'assistant', content: '正在为你解答', received_sequence },
  })
  app.events.push(reply(2, 1))
  await app.notify()
  await expect(typing).toHaveCount(0)
  release()
  await expect(page.getByRole('button', { name: '新建会话' })).toBeEnabled()
  await expect(typing).toHaveCount(0)
  await expect(page.getByText('消息已接收。', { exact: true })).toHaveCount(0)
  await input.fill('再查一下物流')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(2)
  await app.notify()
  await expect(typing).toBeVisible()
  app.events.push(reply(4, 1))
  await app.notify()
  await expect(typing).toBeVisible()
  app.events.push(reply(5, 3))
  await app.notify()
  await expect(typing).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.support-message')).toHaveCount(5)
  await expect(typing).toHaveCount(0)
})

test('Enter sends, Shift+Enter adds a newline, and IME confirmation never submits', async ({
  page,
}) => {
  const app = await prepare(page)
  const input = page.getByRole('textbox', { name: '咨询内容' })
  await input.press('Enter')
  await expect(input).toHaveValue('')
  await input.fill('查询订单')
  await input.press('End')
  await input.press('Shift+Enter')
  await input.pressSequentially('补充信息')
  const message = '查询订单\n补充信息'
  await expect(input).toHaveValue(message)
  await input.dispatchEvent('keydown', {
    key: 'Enter',
    code: 'Enter',
    isComposing: true,
    bubbles: true,
  })
  await expect(input).toHaveValue(message)
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, bubbles: true })
  await expect(input).toHaveValue(message)
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', repeat: true, bubbles: true })
  await expect(input).toHaveValue(message)
  expect(app.requests).toHaveLength(0)
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(1)
  expect(app.requests[0].message).toBe(message)
  await expect(input).toHaveValue('')
  await expect(input).toBeFocused()
})

test('chat timestamps separate message groups and remain stable after replay and reload', async ({
  page,
}) => {
  const app = await prepare(page)
  const minutes = [0, 2, 7, 8]
  app.events.push(
    ...minutes.map((minute, index) => ({
      kind: 'message',
      sequence: index + 1,
      actor: index % 2 ? 'bot' : 'alice',
      created_at: `2026-10-25T09:${String(minute).padStart(2, '0')}:00Z`,
      payload: { role: index % 2 ? 'assistant' : 'user', content: `消息 ${index + 1}` },
    })),
  )
  await app.notify()
  await expect(page.locator('.support-message')).toHaveCount(4)
  await expect(page.locator('.message-time')).toHaveCount(2)
  await expect(page.locator('.message-time').first()).toHaveAttribute(
    'datetime',
    '2026-10-25T09:00:00.000Z',
  )
  await expect(page.locator('.message-time').last()).toHaveAttribute(
    'datetime',
    '2026-10-25T09:07:00.000Z',
  )
  await app.notify()
  await expect(page.locator('.message-time')).toHaveCount(2)
  await page.reload()
  await expect(page.locator('.support-message')).toHaveCount(4)
  await expect(page.locator('.message-time')).toHaveCount(2)
})

test('another input sends while the first HTTP request is pending; late response preserves the draft', async ({
  page,
}) => {
  let release
  const held = new Promise((resolve) => {
    release = resolve
  })
  const app = await prepare(page, {
    onPost: async (_, __, index) => {
      if (index === 1) await held
      return false
    },
  })
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
  expect(new Set(app.requests.map((r) => r.request_id)).size).toBe(2)
  expect(new Set(app.requests.map((r) => r.session_id)).size).toBe(1)
  await expect(page.getByRole('status', { name: '优优正在输入' })).toBeVisible()
})

test('an unknown HTTP outcome retries with the same id and reload recovers the single saved message', async ({
  page,
}) => {
  const app = await prepare(page, {
    onPost: async (route, _, index) => {
      if (index === 1) {
        await route.abort('failed')
        return true
      }
      return false
    },
  })
  await page.getByRole('textbox', { name: '咨询内容' }).fill('不是 A，是 B')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect(page.getByRole('button', { name: '重试这条消息' })).toBeVisible()
  await expect(page.getByRole('status', { name: '优优正在输入' })).toHaveCount(0)
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

test('fast reply and slow follow-up survive duplicate and out-of-order updates plus refresh', async ({
  page,
}, testInfo) => {
  const app = await prepare(page)
  for (const [sequence, content] of [
    [1, 'ORD-A 已发货'],
    [2, 'ORD-B 待发货'],
  ]) {
    app.events.push({
      kind: 'message',
      sequence,
      actor: 'bot',
      created_at: new Date().toISOString(),
      payload: { event_id: `reply-${sequence}`, role: 'assistant', content },
    })
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

test('a transcript acknowledgement wins over a later HTTP failure', async ({ page }) => {
  let release
  const held = new Promise((resolve) => {
    release = resolve
  })
  const app = await prepare(page, {
    onPost: async (route) => {
      await held
      await route.abort('failed')
      return true
    },
  })
  await page.getByRole('textbox', { name: '咨询内容' }).fill('查询物流')
  await page.getByRole('textbox', { name: '咨询内容' }).press('Enter')
  await expect.poll(() => app.requests.length).toBe(1)
  await app.notify()
  await expect(page.locator('.support-message')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '新建会话' })).toBeEnabled()
  const failed = page.waitForEvent('requestfailed', (request) => request.method() === 'POST')
  release()
  await failed
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '重试这条消息' })).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.support-message')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '重试这条消息' })).toHaveCount(0)
})

test('late callbacks from a closed stream cannot clear a new conversation', async ({ page }) => {
  const app = await prepare(page)
  await page.evaluate(() => {
    window.previousStreams = [...window.streams]
  })
  await page.getByRole('button', { name: '新建会话' }).click()
  await expect
    .poll(() =>
      page.evaluate(() =>
        window.streams.some((stream) => !window.previousStreams.includes(stream) && !stream.closed),
      ),
    )
    .toBe(true)
  app.events.push({
    kind: 'message',
    sequence: 1,
    actor: 'bot',
    created_at: new Date().toISOString(),
    payload: { role: 'assistant', content: '新的会话回复' },
  })
  await app.notify()
  await expect(page.locator('.support-message')).toHaveText('优优新的会话回复')
  await page.getByRole('textbox', { name: '咨询内容' }).fill('新会话草稿')
  await page.evaluate(() =>
    window.previousStreams.forEach((stream) => {
      stream.listeners.revoked?.()
      stream.onerror?.()
      stream.onopen?.()
    }),
  )
  await expect(page.locator('.support-message')).toHaveText('优优新的会话回复')
  await expect(page.getByRole('textbox', { name: '咨询内容' })).toHaveValue('新会话草稿')
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('a failed control request from the previous session cannot show an error in the new one', async ({
  page,
}) => {
  await prepare(page)
  let release
  const held = new Promise((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/sessions/*/resume-bot', async (route) => {
    await held
    await route.fulfill({ status: 503, json: { detail: '旧会话错误' } })
  })
  await page.evaluate(() =>
    window.streams
      .filter((stream) => !stream.closed)
      .forEach((stream) =>
        stream.listeners.update({
          data: JSON.stringify({
            events: [],
            cursor: 0,
            state: { mode: 'human', control_version: 1 },
          }),
        }),
      ),
  )
  await page.getByRole('button', { name: '返回机器人' }).click()
  await page.getByRole('button', { name: '新建会话' }).click()
  await expect(page.getByRole('heading', { name: '智能咨询' })).toBeVisible()
  const response = page.waitForResponse('**/api/v1/sessions/*/resume-bot')
  release()
  await response
  await expect(page.getByRole('alert')).toHaveCount(0)
})

async function freezeReplyClock(page) {
  const now = new Date('2026-10-25T09:00:00Z')
  await page.clock.install({ time: now })
  await page.clock.pauseAt(now)
}

const timeoutText = '抱歉，优优出错了，请稍后再试。'

test('reply timeout restarts on the latest send, appears once at 60 seconds and accepts late replies', async ({
  page,
}, testInfo) => {
  await freezeReplyClock(page)
  const app = await prepare(page)
  const input = page.getByRole('textbox', { name: '咨询内容' })
  const typing = page.getByRole('status', { name: '优优正在输入' })
  await input.fill('查询订单')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(1)
  await app.notify()
  await page.clock.fastForward(30_000)
  await input.fill('再查一下物流')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(2)
  await app.notify()
  await input.fill('还没有发送的草稿')
  await page.clock.fastForward(59_000)
  await expect(page.getByText(timeoutText, { exact: true })).toHaveCount(0)
  await expect(typing).toBeVisible()
  await page.clock.fastForward(1_000)
  const timeout = page.locator('.customer-message').filter({ hasText: timeoutText })
  await expect(timeout).toHaveCount(1)
  await expect(timeout.getByRole('img', { name: '客服头像' })).toBeVisible()
  await expect(timeout.locator('.message-sender')).toHaveText('优优')
  await expect(typing).toHaveCount(0)
  await expect(input).toHaveValue('还没有发送的草稿')
  await page.screenshot({ path: testInfo.outputPath('reply-timeout.png'), fullPage: true })
  await page.clock.fastForward(60_000)
  await expect(timeout).toHaveCount(1)
  app.events.push({
    kind: 'message',
    sequence: 3,
    actor: 'bot',
    created_at: new Date().toISOString(),
    payload: { role: 'assistant', content: '这是刚收到的查询结果', received_sequence: 2 },
  })
  await app.notify()
  await expect(page.getByText('这是刚收到的查询结果')).toBeVisible()
  await expect(timeout).toHaveCount(0)
  await expect(typing).toHaveCount(0)
})

test('a covered reply cancels the timeout but a reply to an earlier question does not', async ({
  page,
}) => {
  await freezeReplyClock(page)
  const app = await prepare(page)
  const input = page.getByRole('textbox', { name: '咨询内容' })
  const reply = (sequence, received_sequence) => ({
    kind: 'message',
    sequence,
    actor: 'bot',
    created_at: new Date().toISOString(),
    payload: { role: 'assistant', content: '查询结果', received_sequence },
  })
  await input.fill('第一条')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(1)
  await app.notify()
  await page.clock.fastForward(59_000)
  app.events.push(reply(2, 1))
  await app.notify()
  await page.clock.fastForward(61_000)
  await expect(page.getByText(timeoutText, { exact: true })).toHaveCount(0)
  await input.fill('第二条')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(2)
  await app.notify()
  app.events.push(reply(4, 1))
  await app.notify()
  await page.clock.fastForward(60_000)
  await expect(page.getByText(timeoutText, { exact: true })).toHaveCount(1)
  await input.fill('第三条')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(3)
  await app.notify()
  await expect(page.getByText(timeoutText, { exact: true })).toHaveCount(0)
  await expect(page.getByRole('status', { name: '优优正在输入' })).toBeVisible()
  await expect(page.getByRole('button', { name: '新建会话' })).toBeEnabled()
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.clock.fastForward(60_000)
  await expect(page.getByText(timeoutText, { exact: true })).toHaveCount(0)
})

test('the timeout starts at send time even while the HTTP receipt is pending', async ({ page }) => {
  await freezeReplyClock(page)
  let release
  const held = new Promise((resolve) => {
    release = resolve
  })
  const app = await prepare(page, {
    onPost: async () => {
      await held
      return false
    },
  })
  const input = page.getByRole('textbox', { name: '咨询内容' })
  await input.fill('查询订单')
  await input.press('Enter')
  await expect.poll(() => app.requests.length).toBe(1)
  await page.clock.fastForward(60_000)
  await expect(page.getByText(timeoutText, { exact: true })).toBeVisible()
  release()
  await expect(page.getByRole('button', { name: '新建会话' })).toBeEnabled()
  await expect(page.getByText(timeoutText, { exact: true })).toBeVisible()
})
