import { useState } from 'react'
import { post } from '../api/client'

export default function StaffSettings() {
  const [user, setUser] = useState('')
  const [role, setRole] = useState('agent')
  const [enabled, setEnabled] = useState(true)
  const [notice, setNotice] = useState('')
  async function save(e) {
    e.preventDefault()
    try {
      await post(`/api/v1/support/staff/${encodeURIComponent(user)}`, { role, enabled })
      setNotice('权限已更新；该用户仍需拥有已配置的登录账号。')
    } catch (e) {
      setNotice(e.message)
    }
  }
  return (
    <details className="support-card">
      <summary>客服权限管理</summary>
      <form className="support-staff-form" onSubmit={save}>
        <input
          aria-label="客服用户名"
          placeholder="已有登录用户名"
          value={user}
          onChange={(e) => setUser(e.target.value)}
          required
        />
        <select aria-label="角色" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="agent">客服</option>
          <option value="admin">管理员</option>
        </select>
        <label>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          启用
        </label>
        <button>保存权限</button>
        <p role="status">{notice}</p>
      </form>
    </details>
  )
}
