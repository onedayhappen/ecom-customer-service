import { useEffect, useState } from 'react'
import { parseTimestamp } from '../shared/format'

const REPLY_TIMEOUT_MS = 60_000

// A local send starts the clock immediately, before HTTP acknowledgement or SSE.
// Reloaded conversations use the latest user message's server timestamp instead.
export function useReplyTimeout({ sessionId, messages, enabled }) {
  const [sent, setSent] = useState(null)
  const [expiredKey, setExpiredKey] = useState(null)
  const latestUser = messages.findLast((message) => message.role === 'user')
  const local = sent?.sessionId === sessionId ? sent : null
  const acknowledged = local && messages.find((message) => message.request_id === local.requestId)
  const requestId = local?.requestId ?? latestUser?.request_id ?? latestUser?.sequence
  const sentAt = local?.sentAt ?? parseTimestamp(latestUser?.at)?.getTime()
  const sequence =
    acknowledged?.sequence ?? local?.sequence ?? (!local ? latestUser?.sequence : null)
  const key = requestId == null ? null : `${sessionId}:${requestId}`
  const answered =
    sequence != null &&
    messages.some(
      (message) =>
        !message.internal &&
        ['assistant', 'agent'].includes(message.role) &&
        (message.received_sequence ?? message.sequence) >= sequence,
    )

  useEffect(() => {
    if (!enabled || !key || sentAt == null || answered) return
    const timer = setTimeout(
      () => setExpiredKey(key),
      Math.max(0, sentAt + REPLY_TIMEOUT_MS - Date.now()),
    )
    return () => clearTimeout(timer)
  }, [enabled, key, sentAt, answered])

  function recordSend(payload) {
    setSent({
      sessionId: payload.session_id,
      requestId: payload.request_id,
      sentAt: Date.now(),
      sequence: null,
    })
  }

  function recordReceipt(payload, receipt) {
    setSent((previous) =>
      previous?.sessionId === payload.session_id && previous.requestId === payload.request_id
        ? { ...previous, sequence: receipt.sequence ?? previous.sequence }
        : previous,
    )
  }

  return {
    replyTimedOut: enabled && !answered && key != null && expiredKey === key,
    recordSend,
    recordReceipt,
  }
}
