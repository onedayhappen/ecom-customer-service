export const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8010').replace(
  /\/+$/,
  '',
)
export async function request(path, options = {}) {
  const cookie = document.cookie.split('; ').find((x) => x.startsWith('ecom_agent_csrf='))
  const token = cookie ? decodeURIComponent(cookie.slice('ecom_agent_csrf='.length)) : ''
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { 'X-CSRF-Token': token } : {}),
      ...options.headers,
    },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || '服务暂时不可用')
  return data
}
export const post = (path, body = {}) =>
  request(path, { method: 'POST', body: JSON.stringify(body) })
