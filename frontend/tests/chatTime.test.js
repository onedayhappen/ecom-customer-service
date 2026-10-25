import test from 'node:test'
import assert from 'node:assert/strict'
import { messageTimestamp } from '../src/chatTime.js'

const local = (year, month, day, hour, minute) => new Date(year, month - 1, day, hour, minute)
const iso = (...parts) => local(...parts).toISOString()

test('nearby messages share a timestamp; five-minute gaps and day boundaries start a group', () => {
  const now = local(2026, 10, 25, 12, 0)
  assert.equal(messageTimestamp(iso(2026, 10, 25, 9, 0), undefined, now).label, '09:00')
  assert.equal(messageTimestamp(iso(2026, 10, 25, 9, 4), iso(2026, 10, 25, 9, 0), now), null)
  assert.equal(
    messageTimestamp(iso(2026, 10, 25, 9, 5), iso(2026, 10, 25, 9, 0), now).label,
    '09:05',
  )
  assert.equal(
    messageTimestamp(iso(2026, 10, 25, 0, 1), iso(2026, 10, 24, 23, 59), now).label,
    '00:01',
  )
})

test('labels distinguish today, yesterday, older dates and previous years', () => {
  const now = local(2026, 10, 25, 12, 0)
  assert.equal(messageTimestamp(iso(2026, 10, 24, 17, 38), null, now).label, '昨天 17:38')
  assert.equal(messageTimestamp(iso(2026, 10, 20, 17, 38), null, now).label, '10月20日 17:38')
  assert.equal(messageTimestamp(iso(2025, 10, 20, 17, 38), null, now).label, '2025年10月20日 17:38')
  assert.equal(
    messageTimestamp(iso(2025, 12, 31, 17, 38), null, local(2026, 1, 1, 12, 0)).label,
    '昨天 17:38',
  )
})

test('missing times are omitted and timezone-less server timestamps are treated as UTC', () => {
  for (const value of [undefined, null, '', 'invalid']) assert.equal(messageTimestamp(value), null)
  assert.equal(
    messageTimestamp('2026-10-25T09:00:00', 'invalid').dateTime,
    '2026-10-25T09:00:00.000Z',
  )
  assert.equal(messageTimestamp('2026-10-25T17:00:00+08:00').dateTime, '2026-10-25T09:00:00.000Z')
})
