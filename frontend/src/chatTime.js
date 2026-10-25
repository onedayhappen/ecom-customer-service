import { parseTimestamp } from './shared/format.js'

function sameDay(left, right) {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  )
}

// Group nearby messages, but always mark the start of a new calendar day.
export function messageTimestamp(current, previous, now = new Date()) {
  const date = parseTimestamp(current),
    before = parseTimestamp(previous)
  if (!date) return null
  if (before && sameDay(date, before) && Math.abs(date - before) < 5 * 60 * 1000) return null
  const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  const yesterday = new Date(now)
  yesterday.setDate(yesterday.getDate() - 1)
  let label = time
  if (sameDay(date, yesterday)) label = `昨天 ${time}`
  else if (!sameDay(date, now)) {
    const year = date.getFullYear() === now.getFullYear() ? '' : `${date.getFullYear()}年`
    label = `${year}${date.getMonth() + 1}月${date.getDate()}日 ${time}`
  }
  return { dateTime: date.toISOString(), label }
}
