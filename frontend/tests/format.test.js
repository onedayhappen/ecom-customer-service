import test from 'node:test'
import assert from 'node:assert/strict'
import { parseTimestamp, formatDate, formatDateTime, formatMoney } from '../src/shared/format.js'

test('API timestamps share UTC handling across profile, orders, coupons and chat', () => {
  assert.equal(parseTimestamp('2026-10-25T09:00:00').toISOString(), '2026-10-25T09:00:00.000Z')
  assert.equal(
    parseTimestamp('2026-10-25T17:00:00+08:00').toISOString(),
    '2026-10-25T09:00:00.000Z',
  )
  for (const value of [null, '', 'bad timestamp']) {
    assert.equal(parseTimestamp(value), null)
    assert.equal(formatDate(value), '未提供')
    assert.equal(formatDateTime(value), '未提供')
  }
})

test('money distinguishes zero from unknown amounts and handles invalid currency safely', () => {
  assert.equal(formatMoney('0.00'), '¥0.00')
  assert.equal(formatMoney('1234.50'), '¥1,234.50')
  for (const value of [null, undefined, '', 'bad amount', Infinity]) {
    assert.equal(formatMoney(value), '金额待确认')
  }
  assert.equal(formatMoney('10.00', 'not-a-currency'), '金额待确认')
})
