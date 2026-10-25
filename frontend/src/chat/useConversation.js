import { useEffect, useRef, useState } from 'react'
import { API_BASE, request } from '../api/client'
import { advanceCursor, mergeMessages as mergeTranscript } from '../conversationState'

export function useConversation(sessionId, staff = false, ticketId = '') {
  const [messages, setMessages] = useState([])
  const [state, setState] = useState(null)
  const [error, setError] = useState('')
  const [tasks, setTasks] = useState([])
  const [taskQuestion, setTaskQuestion] = useState(null)
  const [historyBefore, setHistoryBefore] = useState(null)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const historyLoader = useRef(null)
  useEffect(() => {
    setMessages([])
    setState(null)
    setError('')
    setTasks([])
    setTaskQuestion(null)
    setHistoryBefore(null)
    setLoadingHistory(false)
    historyLoader.current = null
    if (!sessionId) return
    let cancelled = false,
      source,
      cursor = 0
    const controller = new AbortController()
    const options = { signal: controller.signal }
    const scope = `staff=${staff}${ticketId ? `&ticket_id=${ticketId}` : ''}`
    const mergeMessages = (events) => setMessages((previous) => mergeTranscript(previous, events))
    let before = null,
      loadingOlder = false
    historyLoader.current = async () => {
      if (cancelled || before === null || loadingOlder) return
      loadingOlder = true
      setLoadingHistory(true)
      try {
        const data = await request(
          `/api/v1/sessions/${sessionId}/transcript?before=${before}&${scope}`,
          options,
        )
        if (cancelled) return
        mergeMessages(data.events)
        before = data.next_before
        setHistoryBefore(before)
      } catch (e) {
        if (!cancelled) setError(e.message)
      } finally {
        loadingOlder = false
        if (!cancelled) setLoadingHistory(false)
      }
    }
    const apply = (data) => {
      if (cancelled) return
      if (data.cursor < cursor) {
        mergeMessages(data.events)
        return
      }
      cursor = advanceCursor(cursor, data.cursor)
      setState((previous) =>
        previous?.control_version > data.state?.control_version ? previous : data.state,
      )
      setTasks((previous) => {
        const merged = new Map(previous.map((t) => [t.task_id, t]))
        data.events
          .filter((e) => e.kind === 'task_state')
          .forEach((e) => merged.set(e.payload.task.task_id, e.payload.task))
        return [...merged.values()]
      })
      data.events
        .filter((e) => e.kind === 'task_choice')
        .forEach((e) => setTaskQuestion(e.payload.question))
      mergeMessages(data.events)
    }
    async function start() {
      try {
        if (!staff) await request(`/api/v1/sessions/${sessionId}/support-state`, options)
        const data = await request(`/api/v1/sessions/${sessionId}/transcript?${scope}`, options)
        if (cancelled) return
        setTasks(data.tasks || [])
        setTaskQuestion(data.question || null)
        before = data.next_before
        setHistoryBefore(before)
        apply(data)
        source = new EventSource(
          `${API_BASE}/api/v1/sessions/${sessionId}/support-events?after=${cursor}&${scope}`,
          { withCredentials: true },
        )
        source.addEventListener('update', (e) => {
          if (cancelled) return
          apply(JSON.parse(e.data))
          setError('')
        })
        source.addEventListener('revoked', () => {
          if (cancelled) return
          cancelled = true
          controller.abort()
          source.close()
          setMessages([])
          setTasks([])
          setTaskQuestion(null)
          setState(null)
          setHistoryBefore(null)
          setLoadingHistory(false)
          setError('会话权限已变更，请返回列表刷新')
        })
        source.onerror = () => {
          if (!cancelled) setError('连接中断，正在恢复消息…')
        }
        source.onopen = () => {
          if (!cancelled) setError('')
        }
      } catch (e) {
        if (!cancelled) setError(e.message)
      }
    }
    start()
    return () => {
      cancelled = true
      controller.abort()
      source?.close()
    }
  }, [sessionId, staff, ticketId])
  return {
    messages,
    state,
    error,
    setState,
    tasks,
    taskQuestion,
    hasOlder: historyBefore !== null,
    loadingHistory,
    loadOlder: () => historyLoader.current?.(),
  }
}
