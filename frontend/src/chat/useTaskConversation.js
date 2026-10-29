import { useEffect, useRef, useState } from 'react'
import { post, request } from '../api/client'
import { useConversation } from './useConversation'
import { useReplyTimeout } from './useReplyTimeout'
import { acknowledge, awaitingReply, restorePending } from '../conversationState'

const POLL_INTERVAL_MS = 2000

export function useTaskConversation(identity) {
  const sessionKey = `ecom-task-session:${identity.user_id}`
  const [sessionId, setSessionId] = useState(
    () => localStorage.getItem(sessionKey) || crypto.randomUUID(),
  )
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState([])
  const [uploading, setUploading] = useState(false)
  const uploadLock = useRef(false)
  const [error, setError] = useState('')
  const [progress, setProgress] = useState(null)
  const [acceptedSequence, setAcceptedSequence] = useState(0)
  const [operations, setOperations] = useState([])
  const [busyOperations, setBusyOperations] = useState({})
  const operationRevision = useRef(0)
  const runningOperations = useRef(new Set())
  const outboxKey = `ecom-task-outbox:${identity.user_id}:${sessionId}`
  const [pending, setPending] = useState(() => restorePending(sessionStorage, outboxKey, sessionId))
  const items = useRef(pending)
  const active = useRef(sessionId)
  const conversation = useConversation(sessionId)
  const automaticReply =
    (!conversation.state || conversation.state.mode === 'bot') &&
    progress?.automatic_processing !== false &&
    progress?.processing !== 'waiting_human'
  const { replyTimedOut, recordSend, recordReceipt } = useReplyTimeout({
    sessionId,
    messages: conversation.messages,
    enabled: automaticReply,
  })
  useEffect(() => {
    localStorage.setItem(sessionKey, sessionId)
  }, [sessionKey, sessionId])
  useEffect(() => {
    active.current = sessionId
    return () => {
      active.current = null
    }
  }, [sessionId])
  function save(next) {
    items.current = next
    setPending(next)
    try {
      sessionStorage.setItem(outboxKey, JSON.stringify(next))
    } catch {
      setError('浏览器无法保存待发送消息，请保持页面打开并核对发送结果。')
    }
  }
  useEffect(() => {
    const next = acknowledge(items.current, conversation.messages)
    if (next.length !== items.current.length) save(next)
  }, [conversation.messages])
  useEffect(() => {
    let cancelled = false,
      timer
    const controller = new AbortController()
    async function refresh() {
      try {
        const data = await request(`/api/v1/task-messages/${sessionId}`, {
          signal: controller.signal,
        })
        if (!cancelled) {
          setProgress(data)
          const next = acknowledge(items.current, data.messages)
          if (next.length !== items.current.length) save(next)
        }
      } catch {
        /* SSE reports connectivity; an unused conversation has no intake yet. */
      }
      if (cancelled) return
      try {
        const revision = operationRevision.current
        const cards = await request(`/api/v1/task-operations/${sessionId}`, {
          signal: controller.signal,
        })
        if (
          !cancelled &&
          revision === operationRevision.current &&
          runningOperations.current.size === 0 &&
          Array.isArray(cards)
        )
          setOperations(cards)
      } catch {
        /* Preserve the last cards during a temporary read failure. */
      }
      if (!cancelled) timer = setTimeout(refresh, POLL_INTERVAL_MS)
    }
    refresh()
    return () => {
      cancelled = true
      controller.abort()
      clearTimeout(timer)
    }
  }, [sessionId])
  async function deliver(payload) {
    if (
      items.current.find((x) => x.payload.request_id === payload.request_id)?.status === 'sending'
    )
      return
    save(
      items.current.map((x) =>
        x.payload.request_id === payload.request_id ? { ...x, status: 'sending' } : x,
      ),
    )
    try {
      const receipt = await post('/api/v1/task-messages', payload)
      if (active.current !== payload.session_id) return
      recordReceipt(payload, receipt)
      save(items.current.filter((x) => x.payload.request_id !== payload.request_id))
      setAcceptedSequence((previous) => Math.max(previous, receipt.sequence || 0))
      setError('')
    } catch (e) {
      if (active.current !== payload.session_id) return
      // A transcript acknowledgement is authoritative even if HTTP later fails.
      if (!items.current.some((item) => item.payload.request_id === payload.request_id)) return
      save(
        items.current.map((x) =>
          x.payload.request_id === payload.request_id ? { ...x, status: 'failed' } : x,
        ),
      )
      setError('尚未确认是否收到，请用下方重试按钮核实：' + e.message)
    }
  }
  function send(event) {
    event.preventDefault()
    const message = input.trim()
    if ((!message && !attachments.length) || uploadLock.current || items.current.length >= 20)
      return
    const payload = {
      session_id: sessionId,
      request_id: crypto.randomUUID(),
      message,
      ...(attachments.length ? { attachment_ids: attachments.map((a) => a.attachment_id) } : {}),
    }
    recordSend(payload)
    save([...items.current, { payload, status: 'queued' }])
    setInput('')
    setAttachments([])
    setError('')
    deliver(payload)
  }
  async function uploadImages(event) {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    if (uploadLock.current || !files.length) return
    if (files.length + attachments.length > 4) {
      setError('每条消息最多 4 张图片')
      return
    }
    const sid = sessionId
    uploadLock.current = true
    setUploading(true)
    try {
      for (const file of files) {
        if (file.size > 10 * 1024 * 1024) throw new Error('每张图片不能超过 10 MB')
        const body = new FormData()
        body.append('session_id', sid)
        body.append('upload_request_id', crypto.randomUUID())
        body.append('file', file)
        const image = await request('/api/v1/attachments', { method: 'POST', body })
        if (active.current !== sid) return
        setAttachments((previous) => [...previous, image])
      }
      setError('')
    } catch (e) {
      if (active.current === sid) setError(e.message)
    } finally {
      uploadLock.current = false
      setUploading(false)
    }
  }
  async function removeAttachment(id) {
    if (uploadLock.current) return
    uploadLock.current = true
    setUploading(true)
    try {
      await request(`/api/v1/attachments/${id}`, { method: 'DELETE' })
      setAttachments((previous) => previous.filter((a) => a.attachment_id !== id))
    } catch (e) {
      setError(e.message)
    } finally {
      uploadLock.current = false
      setUploading(false)
    }
  }
  function handleComposerKeyDown(event) {
    if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey)
      return
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
    event.preventDefault()
    if (!event.repeat) event.currentTarget.form.requestSubmit()
  }
  function startNew() {
    if (uploadLock.current) return
    const sid = crypto.randomUUID()
    active.current = sid
    setSessionId(sid)
    setInput('')
    setAttachments([])
    setError('')
    setAcceptedSequence(0)
    setProgress(null)
    // Pending requests remain recoverable in the previous conversation's outbox.
    items.current = []
    setPending([])
    operationRevision.current += 1
    runningOperations.current.clear()
    setOperations([])
    setBusyOperations({})
  }
  async function operate(operation, action) {
    const oid = operation.operation_id,
      sid = sessionId
    if (runningOperations.current.has(oid)) return
    runningOperations.current.add(oid)
    operationRevision.current += 1
    setBusyOperations((previous) => ({ ...previous, [oid]: true }))
    try {
      const updated = await post(`/api/v1/task-operations/${oid}/${action}`, {
        session_id: sid,
        ...(action === 'confirm' ? { confirmation_token: operation.confirmation_token } : {}),
      })
      if (active.current === sid) {
        setOperations((previous) =>
          previous.map((item) => (item.operation_id === oid ? updated : item)),
        )
        setError('')
      }
    } catch (e) {
      if (active.current === sid) {
        setError(e.message + '。请刷新操作状态后再决定下一步。')
        setOperations((previous) =>
          previous.map((item) =>
            item.operation_id === oid ? { ...item, can_confirm: false } : item,
          ),
        )
      }
    } finally {
      runningOperations.current.delete(oid)
      operationRevision.current += 1
      if (active.current === sid) setBusyOperations((previous) => ({ ...previous, [oid]: false }))
    }
  }
  async function changeControl(action) {
    try {
      const state = await post(`/api/v1/sessions/${sessionId}/${action}`)
      if (active.current === sessionId) conversation.setState(state)
    } catch (e) {
      if (active.current === sessionId) setError(e.message)
    }
  }
  const state = conversation.state
  const typing =
    automaticReply &&
    !replyTimedOut &&
    !error &&
    !conversation.error &&
    awaitingReply(
      conversation.messages,
      pending,
      Math.max(acceptedSequence, progress?.received_sequence || 0),
    )
  return {
    sessionId,
    attachments,
    uploading,
    uploadImages,
    removeAttachment,
    conversation,
    state,
    typing,
    replyTimedOut,
    error,
    input,
    setInput,
    pending,
    operations,
    busyOperations,
    startNew,
    send,
    handleComposerKeyDown,
    deliver,
    operate,
    changeControl,
  }
}
