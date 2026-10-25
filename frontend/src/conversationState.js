// Event identity survives notification retries, reconnects and snapshot reloads.
export function mergeMessages(previous, events) {
  const merged = new Map(previous.map((m) => [m.sequence, m]))
  for (const e of events) {
    if (e.kind === 'message')
      merged.set(e.sequence, {
        ...e.payload,
        sequence: e.sequence,
        actor: e.actor,
        at: e.created_at,
      })
  }
  return [...merged.values()].sort((a, b) => a.sequence - b.sequence)
}

export function advanceCursor(current, incoming) {
  return Number.isSafeInteger(incoming) ? Math.max(current, incoming) : current
}

export function acknowledge(pending, messages) {
  const received = new Set(messages.map((m) => m.request_id).filter(Boolean))
  return pending.filter((item) => !received.has(item.payload.request_id))
}

export function awaitingReply(messages, pending, receivedSequence = 0) {
  let received = receivedSequence,
    answered = 0
  const requests = new Map()
  for (const message of messages) {
    if (message.internal) continue
    if (message.role === 'user') {
      received = Math.max(received, message.sequence)
      if (message.request_id) requests.set(message.request_id, message.sequence)
    } else if (message.role === 'assistant' || message.role === 'agent') {
      answered = Math.max(answered, message.received_sequence ?? message.sequence)
    }
  }
  return (
    received > answered ||
    pending.some(
      (item) =>
        item.status !== 'failed' && (requests.get(item.payload.request_id) ?? Infinity) > answered,
    )
  )
}

export function restorePending(storage, key, sessionId) {
  try {
    return JSON.parse(storage.getItem(key) || '[]')
      .filter(
        (item) =>
          item.payload?.session_id === sessionId &&
          item.payload?.request_id &&
          typeof item.payload?.message === 'string',
      )
      .slice(0, 20)
      .map((item) => ({ payload: item.payload, status: 'failed' }))
  } catch {
    return []
  }
}
