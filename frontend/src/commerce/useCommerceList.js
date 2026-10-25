import { useCallback, useEffect, useRef, useState } from 'react'
import { request } from '../api/client'

const emptyResult = { items: [], total: 0, next_cursor: null }

export function useCommerceList(path) {
  const [result, setResult] = useState(emptyResult)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const activeRequest = useRef(null)

  const fetchPage = useCallback(
    async (cursor = null) => {
      activeRequest.current?.abort()
      const controller = new AbortController()
      activeRequest.current = controller
      setLoading(true)
      setError('')
      try {
        const url = cursor == null ? path : `${path}&cursor=${encodeURIComponent(cursor)}`
        const next = await request(url, { signal: controller.signal })
        if (controller.signal.aborted) return
        setResult((previous) =>
          cursor == null ? next : { ...next, items: [...previous.items, ...next.items] },
        )
      } catch (error) {
        if (!controller.signal.aborted) setError(error.message)
      } finally {
        if (activeRequest.current === controller && !controller.signal.aborted) {
          activeRequest.current = null
          setLoading(false)
        }
      }
    },
    [path],
  )

  useEffect(() => {
    setResult(emptyResult)
    fetchPage()
    return () => activeRequest.current?.abort()
  }, [fetchPage])

  function loadMore() {
    if (!activeRequest.current && result.next_cursor != null) fetchPage(result.next_cursor)
  }

  return { result, loading, error, refresh: () => fetchPage(), loadMore }
}
