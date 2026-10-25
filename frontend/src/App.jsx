import { useEffect, useState } from 'react'
import AuthScreen from './auth/AuthScreen'
import SupportConsole from './SupportConsole'
import CustomerWorkspace from './CustomerWorkspace'
import { post, request } from './api/client'

export default function App() {
  const [identity, setIdentity] = useState(null)
  const [staffView, setStaffView] = useState(false)
  async function loadIdentity() {
    try {
      setIdentity(await request('/api/v1/support/me'))
    } catch {
      setIdentity(null)
    }
  }
  useEffect(() => {
    loadIdentity()
  }, [])
  async function logout() {
    try {
      await post('/api/v1/auth/logout')
    } finally {
      setIdentity(null)
      setStaffView(false)
    }
  }
  if (!identity) return <AuthScreen onSave={loadIdentity} />
  if (staffView && identity.role !== 'customer')
    return <SupportConsole role={identity.role} onBack={() => setStaffView(false)} />
  return (
    <CustomerWorkspace
      key={identity.user_id}
      identity={identity}
      onLogout={logout}
      onStaff={() => setStaffView(true)}
    />
  )
}
