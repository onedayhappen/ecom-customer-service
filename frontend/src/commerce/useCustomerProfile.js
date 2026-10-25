import { useEffect, useState } from 'react'
import { request } from '../api/client'

export function useCustomerProfile() {
  const [profile, setProfile] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    request('/api/commerce/profile', { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setProfile(data)
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [revision])
  return { profile, loading, error, refresh: () => setRevision((n) => n + 1) }
}
