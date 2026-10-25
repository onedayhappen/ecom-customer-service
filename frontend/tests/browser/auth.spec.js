import { test, expect } from '@playwright/test'

async function prepare(page) {
  let authenticated = false
  const submissions = []
  await page.addInitScript(() => {
    window.EventSource = class {
      addEventListener() {}
      close() {}
    }
  })
  await page.route(/\/api\/(?:v1|commerce)\//, async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/support/me'))
      return route.fulfill({
        status: authenticated ? 200 : 401,
        json: authenticated ? { user_id: 'alice', role: 'customer' } : { detail: '请先登录' },
      })
    if (path.endsWith('/auth/login') || path.endsWith('/auth/register')) {
      const body = route.request().postDataJSON()
      submissions.push(body)
      if (body.password === 'wrong')
        return route.fulfill({ status: 401, json: { detail: '用户名或密码错误' } })
      authenticated = true
      return route.fulfill({ json: {} })
    }
    if (path.endsWith('/auth/logout')) {
      authenticated = false
      return route.fulfill({ json: {} })
    }
    if (path.endsWith('/profile'))
      return route.fulfill({ json: { username: '爱丽丝', memberships: [] } })
    if (path.endsWith('/transcript'))
      return route.fulfill({
        json: { events: [], state: { mode: 'bot' }, cursor: 0, next_before: null },
      })
    return route.fulfill({ json: {} })
  })
  await page.goto('/')
  return submissions
}

test('login errors remain actionable and logout returns to the login screen', async ({ page }) => {
  const submissions = await prepare(page)
  await page.getByLabel('用户名', { exact: true }).fill('alice')
  await page.getByLabel('密码', { exact: true }).fill('wrong')
  await page.getByRole('button', { name: '进入工作台', exact: true }).click()
  await expect(page.getByText('用户名或密码错误')).toBeVisible()
  await page.getByLabel('密码', { exact: true }).fill('valid-password')
  await page.getByRole('button', { name: '进入工作台', exact: true }).click()
  await expect(page.getByRole('heading', { name: '智能咨询' })).toBeVisible()
  expect(submissions).toHaveLength(2)
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible()
})

test('registration checks password confirmation before submitting', async ({ page }) => {
  const submissions = await prepare(page)
  await page.getByRole('button', { name: '没有账号？立即注册' }).click()
  await page.getByLabel('用户名', { exact: true }).fill('alice')
  await page.getByLabel('密码', { exact: true }).fill('valid-password')
  await page.getByLabel('确认密码', { exact: true }).fill('another-password')
  await page.getByRole('button', { name: '注册并进入' }).click()
  await expect(page.getByText('两次输入的密码不一致')).toBeVisible()
  expect(submissions).toHaveLength(0)
  await page.getByLabel('确认密码', { exact: true }).fill('valid-password')
  await page.getByRole('button', { name: '注册并进入' }).click()
  await expect(page.getByRole('heading', { name: '智能咨询' })).toBeVisible()
  expect(submissions).toHaveLength(1)
})
