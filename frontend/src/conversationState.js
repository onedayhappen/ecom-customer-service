// Event identity survives notification retries, reconnects and snapshot reloads.
export function mergeMessages(previous, events) {
  const merged = new Map(previous.map(m => [m.sequence, m]))
  for (const e of events) {
    if (e.kind === 'message') merged.set(e.sequence, { ...e.payload, sequence: e.sequence, actor: e.actor, at: e.created_at })
  }
  return [...merged.values()].sort((a, b) => a.sequence - b.sequence)
}

export function advanceCursor(current, incoming) {
  return Number.isSafeInteger(incoming) ? Math.max(current, incoming) : current
}

export function acknowledge(pending, messages) {
  const received = new Set(messages.map(m => m.request_id).filter(Boolean))
  return pending.filter(item => !received.has(item.payload.request_id))
}

export function restorePending(storage, key, sessionId) {
  try {
    return JSON.parse(storage.getItem(key) || '[]')
      .filter(item => item.payload?.session_id === sessionId && item.payload?.request_id && typeof item.payload?.message === 'string')
      .slice(0, 20).map(item => ({ payload: item.payload, status: 'failed' }))
  } catch { return [] }
}
