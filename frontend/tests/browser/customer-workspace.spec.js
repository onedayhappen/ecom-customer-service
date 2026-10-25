import { test, expect } from '@playwright/test'

async function prepare(
  page,
  {
    failOrders = false,
    emptyCoupons = false,
    couponCatalog = false,
    failProfile = false,
    profileMode = 'active',
  } = {},
) {
  await page.addInitScript(() => {
    window.EventSource = class {
      addEventListener() {}
      close() {}
    }
  })
  const statuses = ['pending_payment', 'paid', 'pending_receipt', 'after_sale', 'received']
  const orders = statuses.map((status, index) => ({
    order_id: `ORDER-${index}`,
    platform_code: '商城',
    status,
    created_at: '2026-10-25T01:00:00',
    currency: 'CNY',
    payable_amount: 199,
    items: [
      {
        order_item_id: `item-${index}`,
        title_snapshot: '无线降噪耳机',
        spec_snapshot: { color: '雾白色' },
        quantity: 1,
      },
    ],
  }))
  await page.route(/\/api\/(?:v1|commerce)\//, async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname
    if (path.endsWith('/support/me'))
      return route.fulfill({ json: { user_id: 'alice', role: 'customer' } })
    if (path.endsWith('/profile')) {
      if (failProfile) return route.fulfill({ status: 503, json: { detail: '个人信息暂时不可用' } })
      const membership = {
        membership_id: 'growth-member',
        program_id: 'growth',
        name: '铂金',
        effective_status: 'active',
        growth_value: 8200,
        starts_at: '2026-10-01T00:00:00',
        expires_at: '2026-12-31T00:00:00',
      }
      const subscription = {
        membership_id: 'vip-member',
        plan_code: 'annual',
        starts_at: '2026-10-01T00:00:00',
        ends_at: '2027-10-01T00:00:00',
      }
      return route.fulfill({
        json: {
          username: '爱丽丝',
          created_at: '2026-10-01T00:00:00',
          memberships:
            profileMode === 'empty'
              ? []
              : [
                  {
                    platform_code: '商城',
                    growth_membership: membership,
                    vip: {
                      status: profileMode,
                      subscription: profileMode === 'active' ? subscription : null,
                      subscription_history: [subscription],
                      records: [{ membership_id: 'vip-member', program_id: 'vip' }],
                    },
                    store_memberships: [
                      {
                        ...membership,
                        membership_id: 'store-member',
                        program_id: 'store',
                        program_name: '优选服饰店会员',
                        name: '高级店铺会员',
                      },
                    ],
                    benefit_definitions: [
                      {
                        benefit_id: 'growth-coupon',
                        version: 1,
                        program_id: 'growth',
                        benefit_family: 'growth-month',
                        status: 'active',
                        membership_active: true,
                        period_type: 'month',
                        quota: 2,
                        rules: { threshold_amount: 200, discount_amount: 25, currency: 'CNY' },
                      },
                      {
                        benefit_id: 'vip-coupon',
                        version: 1,
                        program_id: 'vip',
                        benefit_family: 'vip-month',
                        status: 'active',
                        membership_active: profileMode === 'active',
                        period_type: 'month',
                        quota: 2,
                        rules: { threshold_amount: 200, discount_amount: 50, currency: 'CNY' },
                      },
                      {
                        benefit_id: 'planned-coupon',
                        version: 1,
                        program_id: 'growth',
                        status: 'planned',
                        membership_active: true,
                        period_type: 'month',
                        quota: 1,
                        rules: {},
                      },
                    ],
                    benefit_entitlements: [
                      {
                        program_id: 'growth',
                        benefit_family: 'growth-month',
                        effective_status: 'active',
                        claimed_count: 1,
                        remaining_count: 1,
                      },
                    ],
                  },
                ],
        },
      })
    }
    if (path.endsWith('/orders')) {
      if (failOrders) return route.fulfill({ status: 503, json: { detail: '订单服务暂时不可用' } })
      const status = url.searchParams.get('status'),
        filtered = status === 'all' ? orders : orders.filter((o) => o.status === status)
      const more = status === 'all' && !url.searchParams.get('cursor')
      return route.fulfill({
        json: {
          items: more ? filtered.slice(0, 2) : status === 'all' ? filtered.slice(2) : filtered,
          total: filtered.length,
          next_cursor: more ? 'next-page' : null,
        },
      })
    }
    if (path.endsWith('/coupons')) {
      const coupons = [
        {
          user_coupon_id: 'coupon-1',
          coupon_type: 'platform',
          status: 'available',
          expires_at: '2026-12-31T15:59:59',
          scope_description: '全场适用',
          template: {
            name: '秋日购物礼券',
            platform_code: '商城',
            kind: 'amount_off',
            discount_amount: 20,
            threshold_amount: 100,
            currency: 'CNY',
          },
        },
        {
          user_coupon_id: 'coupon-2',
          coupon_type: 'shop',
          status: 'used',
          expires_at: '2026-12-31T15:59:59',
          scope_description: '限指定商家或商品使用',
          template: {
            name: '会员专享折扣',
            platform_code: '商城',
            kind: 'percent_off',
            discount_bps: 1500,
            threshold_amount: 0,
            currency: 'CNY',
          },
        },
      ]
      if (couponCatalog)
        ['shop', 'shop', 'category', 'newcomer', 'general'].forEach((type, index) =>
          coupons.push({
            ...coupons[0],
            user_coupon_id: `extra-${index}`,
            coupon_type: type,
            template: { ...coupons[0].template, name: `分类优惠券 ${index}` },
          }),
        )
      const type = url.searchParams.get('coupon_type')
      const filtered = emptyCoupons
        ? []
        : coupons.filter((c) => type === 'all' || c.coupon_type === type)
      const offset = Number(url.searchParams.get('cursor') || 0)
      return route.fulfill({
        json: {
          items: filtered.slice(offset, offset + 2),
          total: filtered.length,
          next_cursor: offset + 2 < filtered.length ? String(offset + 2) : null,
        },
      })
    }
    if (path.endsWith('/transcript'))
      return route.fulfill({
        json: { state: { mode: 'bot' }, events: [], cursor: 0, next_before: null },
      })
    if (path.includes('/task-operations/')) return route.fulfill({ json: [] })
    return route.fulfill({ json: {} })
  })
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: '咨询内容' })).toBeVisible()
  return () => {
    failOrders = false
    failProfile = false
  }
}

test('sidebar switches views, filters all order states, paginates and preserves the chat draft', async ({
  page,
}) => {
  await prepare(page)
  await page.getByRole('textbox', { name: '咨询内容' }).fill('帮我看一下订单')
  await page.getByRole('button', { name: '订单列表', exact: true }).click()
  await expect(page.getByRole('main', { name: '我的订单', exact: true })).toBeVisible()
  await expect(page.locator('.commerce-heading .eyebrow')).toHaveCount(0)
  await expect(page.locator('.order-card')).toHaveCount(2)
  await expect(page.locator('.order-card header time').first()).not.toContainText('商城')
  await page.getByRole('button', { name: '加载更多' }).click()
  await expect(page.locator('.order-card')).toHaveCount(5)
  for (const label of ['待付款', '待发货', '待收货', '售后中']) {
    await page.getByRole('button', { name: label, exact: true }).click()
    await expect(page.locator('.order-card')).toHaveCount(1)
    await expect(page.locator('.order-card .commerce-badge')).toHaveText(label)
  }
  await page
    .getByRole('navigation', { name: '订单状态' })
    .getByRole('button', { name: '全部', exact: true })
    .click()
  await expect(page.locator('.order-card')).toHaveCount(2)
  await page.screenshot({ path: test.info().outputPath('orders.png'), fullPage: true })
  await page.getByRole('button', { name: '优惠券列表', exact: true }).click()
  await expect(page.getByRole('main', { name: '我的优惠', exact: true })).toBeVisible()
  await expect(page.locator('.commerce-heading .eyebrow')).toHaveCount(0)
  await expect(page.locator('.coupon-card')).toHaveCount(2)
  await expect(page.locator('.coupon-card').first()).toContainText('满 ¥100.00 可用')
  await expect(page.locator('.coupon-card').last()).toContainText('8.5 折')
  await expect(page.locator('.coupon-card').last()).toContainText('已使用')
  await page.screenshot({ path: test.info().outputPath('coupons.png'), fullPage: true })
  await page.getByRole('button', { name: '咨询客服', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '咨询内容' })).toHaveValue('帮我看一下订单')
})

test('failed requests can retry, empty coupons are explicit and mobile navigation stays reachable', async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 640 })
  const recover = await prepare(page, { failOrders: true, emptyCoupons: true })
  const input = await page.getByRole('textbox', { name: '咨询内容' }).boundingBox()
  const send = await page.getByRole('button', { name: '发送消息', exact: true }).boundingBox()
  expect(input.y + input.height).toBeLessThanOrEqual(640)
  expect(send.x).toBeGreaterThanOrEqual(input.x + input.width)
  expect(send.x + send.width).toBeLessThanOrEqual(320)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: test.info().outputPath('mobile-chat.png'), fullPage: true })
  await page.getByRole('button', { name: '订单列表', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('订单服务暂时不可用')
  recover()
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.locator('.order-card')).toHaveCount(2)
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.getByRole('button', { name: '优惠券列表', exact: true }).click()
  await expect(page.getByRole('heading', { name: '暂无优惠券' })).toBeVisible()
  await page.getByRole('button', { name: '通用券', exact: true }).click()
  await expect(page.getByRole('heading', { name: '暂无通用券' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})

test('coupon type tabs filter and paginate independently', async ({ page }) => {
  await prepare(page, { couponCatalog: true })
  await page.getByRole('button', { name: '优惠券列表', exact: true }).click()
  await expect(page.locator('.list-summary')).toContainText('共 7 张优惠券')
  await page.getByRole('button', { name: '店铺券', exact: true }).click()
  await expect(page.locator('.list-summary')).toContainText('共 3 张优惠券')
  await expect(page.locator('.coupon-card')).toHaveCount(2)
  await page.getByRole('button', { name: '加载更多' }).click()
  await expect(page.locator('.coupon-card')).toHaveCount(3)
  for (const card of await page.locator('.coupon-card').all())
    await expect(card).toContainText('店铺券')
  for (const label of ['官方券', '品类券', '新人券', '通用券']) {
    await page.getByRole('button', { name: label, exact: true }).click()
    await expect(page.locator('.coupon-card')).toHaveCount(1)
    await expect(page.locator('.coupon-card')).toContainText(label)
    await expect(page.getByRole('button', { name: '加载更多' })).toHaveCount(0)
  }
  await page.getByRole('button', { name: '全部', exact: true }).click()
  await expect(page.locator('.coupon-card')).toHaveCount(2)
  await expect(page.locator('.list-summary')).toContainText('共 7 张优惠券')
  await expect(page.getByRole('button', { name: '全部', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.screenshot({ path: test.info().outputPath('coupon-types.png'), fullPage: true })
})

test('profile displays username, memberships and benefits while preserving the chat draft', async ({
  page,
}) => {
  await prepare(page)
  await expect(page.locator('.profile strong')).toHaveText('爱丽丝')
  await expect(page.locator('.profile small')).toHaveCount(0)
  await expect(
    page.getByRole('navigation', { name: '客户服务导航' }).getByRole('button').first(),
  ).toHaveText('个人信息')
  await page.getByRole('textbox', { name: '咨询内容' }).fill('会员优惠咨询')
  await page.getByRole('button', { name: '个人信息', exact: true }).click()
  await expect(page.getByRole('main', { name: '个人信息', exact: true })).toBeVisible()
  await expect(page.locator('.commerce-heading .eyebrow')).toHaveCount(0)
  await expect(page.getByRole('region', { name: '账号资料' })).toContainText('爱丽丝')
  await expect(page.locator('.membership-card').first()).toContainText('铂金')
  await expect(page.locator('.membership-card').first()).toContainText('8200')
  await expect(page.locator('.membership-card').first()).toContainText('本期已领取 1 份，剩余 1 份')
  await expect(page.locator('.membership-card').first()).toContainText('暂未开放')
  await expect(page.locator('.vip-card')).toContainText('年度 VIP')
  await expect(page.locator('.vip-card')).toContainText('满 ¥200.00 减 ¥50.00')
  await expect(page.locator('.membership-card')).toHaveCount(2)
  await page.screenshot({ path: test.info().outputPath('profile.png'), fullPage: true })
  await page.getByRole('button', { name: '咨询客服', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '咨询内容' })).toHaveValue('会员优惠咨询')
})

test('profile failures can retry and expired VIP has no active benefits on mobile', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const recover = await prepare(page, { failProfile: true, profileMode: 'expired' })
  await page.getByRole('button', { name: '个人信息', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('个人信息暂时不可用')
  recover()
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.locator('.vip-card')).toContainText('已过期')
  await expect(page.locator('.vip-card')).toContainText('当前没有生效中的 VIP 权益')
  await expect(page.locator('.vip-card .membership-benefits')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: test.info().outputPath('mobile-profile.png'), fullPage: true })
})

test('new accounts have an explicit empty membership state', async ({ page }) => {
  await prepare(page, { profileMode: 'empty' })
  await page.getByRole('button', { name: '个人信息', exact: true }).click()
  await expect(page.getByRole('heading', { name: '暂无会员信息' })).toBeVisible()
  await expect(page.getByRole('region', { name: '账号资料' })).toContainText('爱丽丝')
})

test('changing order filters cancels an in-flight pagination request', async ({ page }) => {
  await prepare(page)
  let release
  const held = new Promise((resolve) => {
    release = resolve
  })
  await page.route('**/api/commerce/orders?**', async (route) => {
    if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
    await held
    await route.fulfill({ json: { items: [], total: 99, next_cursor: null } })
  })
  await page.getByRole('button', { name: '订单列表', exact: true }).click()
  await expect(page.locator('.order-card')).toHaveCount(2)
  const request = page.waitForRequest((request) => request.url().includes('cursor='))
  await page.getByRole('button', { name: '加载更多' }).click()
  await request
  const cancelled = page.waitForEvent('requestfailed', (request) =>
    request.url().includes('cursor='),
  )
  await page.getByRole('button', { name: '待发货', exact: true }).click()
  await cancelled
  release()
  await expect(page.locator('.list-summary')).toContainText('共 1 笔订单')
  await expect(page.locator('.order-card')).toHaveCount(1)
  await expect(page.locator('.order-card .commerce-badge')).toHaveText('待发货')
  await expect(page.getByRole('alert')).toHaveCount(0)
})
