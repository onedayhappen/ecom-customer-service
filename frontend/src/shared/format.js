// The API returns UTC timestamps, sometimes without an explicit offset.
export function parseTimestamp(value) {
  if (typeof value !== 'string' || !value.trim()) return null
  const text = value.trim()
  const timestamp = new Date(/Z$|[+-]\d\d:\d\d$/i.test(text) ? text : `${text}Z`)
  return Number.isNaN(timestamp.getTime()) ? null : timestamp
}

export function formatDate(value) {
  return parseTimestamp(value)?.toLocaleDateString('zh-CN') ?? '未提供'
}

export function formatDateTime(value) {
  return parseTimestamp(value)?.toLocaleString('zh-CN', { hour12: false }) ?? '未提供'
}

export function formatMoney(value, currency = 'CNY') {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '金额待确认'
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(Number(value))
  } catch {
    return '金额待确认'
  }
}
