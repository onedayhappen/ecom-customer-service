import { test, expect } from '@playwright/test'

test('uploads and sends a pure image, retries analysis, and revokes the preview', async ({
  page,
}, testInfo) => {
  let releaseUpload
  const uploadGate = new Promise((resolve) => {
    releaseUpload = resolve
  })
  let sent,
    retry,
    status = 'ready'
  const events = []
  const aid = '34e4a22f-e833-4c52-a86c-18633361719e'
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
      close() {}
    }
  })
  await page.route('**/api/v1/**', async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname
    const json = (data) => route.fulfill({ json: data })
    if (path.endsWith('/support/me')) return json({ user_id: 'alice', role: 'customer' })
    if (path.endsWith('/support-state')) return json({ mode: 'bot' })
    if (path.endsWith('/transcript'))
      return json({
        state: { mode: 'bot' },
        events,
        cursor: events.length,
        next_before: null,
        tasks: [],
      })
    if (path === '/api/v1/attachments') {
      expect(req.headers()['content-type']).toContain('multipart/form-data; boundary=')
      await uploadGate
      return route.fulfill({ status: 201, json: { attachment_id: aid, status } })
    }
    if (path.endsWith('/analysis-retries')) {
      retry = req.postDataJSON()
      return route.fulfill({ status: 202, json: { sequence: 2 } })
    }
    if (path.endsWith('/content'))
      return route.fulfill({
        contentType: 'image/png',
        body: Buffer.from(
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=',
          'base64',
        ),
      })
    if (path === `/api/v1/attachments/${aid}`) {
      if (req.method() === 'DELETE') {
        status = 'revoked'
        return route.fulfill({ status: 204 })
      }
      return json({
        attachment_id: aid,
        status,
        analyses: sent ? [{ session_id: sent.session_id, sequence: 1, status: 'failed' }] : [],
      })
    }
    if (path === '/api/v1/task-messages' && req.method() === 'POST') {
      sent = req.postDataJSON()
      events.push({
        kind: 'message',
        sequence: 1,
        actor: 'alice',
        created_at: new Date().toISOString(),
        payload: {
          role: 'user',
          content: sent.message,
          attachment_ids: sent.attachment_ids,
          request_id: sent.request_id,
        },
      })
      return route.fulfill({ status: 202, json: { sequence: 1, request_id: sent.request_id } })
    }
    return json({})
  })
  await page.goto('/')
  await page
    .getByLabel('添加图片', { exact: true })
    .setInputFiles({ name: 'photo.png', mimeType: 'image/png', buffer: Buffer.from('file bytes') })
  await expect(page.getByRole('button', { name: '上传中…' })).toBeDisabled()
  releaseUpload()
  await expect(page.getByRole('img', { name: '用户上传的图片' })).toBeVisible()
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect.poll(() => sent).toBeTruthy()
  expect(sent.message).toBe('')
  expect(sent.attachment_ids).toEqual([aid])
  await page.evaluate(
    (events) =>
      window.streams.forEach((s) =>
        s.listeners.update?.({
          data: JSON.stringify({ events, cursor: 1, state: { mode: 'bot' } }),
        }),
      ),
    events,
  )
  await expect(page.getByText('识别失败，可补充文字或重试')).toBeVisible()
  await page.getByRole('button', { name: '重新识别', exact: true }).click()
  await expect.poll(() => retry).toBeTruthy()
  expect(retry.session_id).toBe(sent.session_id)
  expect(retry.source_sequence).toBe(1)
  await expect(page.getByText('已提交重新识别')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('image-conversation.png') })
  await page.getByRole('button', { name: '撤回图片', exact: true }).click()
  await expect(page.getByText('图片已撤回或过期')).toBeVisible()
  await expect(page.getByRole('img', { name: '用户上传的图片' })).toHaveCount(0)
})
