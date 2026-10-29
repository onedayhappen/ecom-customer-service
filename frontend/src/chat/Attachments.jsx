import { useEffect, useRef, useState } from 'react'
import { API_BASE, post, request } from '../api/client'

const labels = {
  pending: '等待识别',
  running: '正在识别',
  retrying: '正在重试',
  succeeded: '识别完成',
  failed: '识别失败，可补充文字或重试',
  cancelled: '识别已停止',
}

export function Attachment({ id, sessionId, sequence, ticketId, onRemove, revoked = false }) {
  const [asset, setAsset] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [retried, setRetried] = useState(false)
  const retryId = useRef(null)
  const query = ticketId ? `?ticket_id=${encodeURIComponent(ticketId)}` : ''
  const path = `/api/v1/attachments/${encodeURIComponent(id)}`
  useEffect(() => {
    let stopped = false,
      timer
    const controller = new AbortController()
    async function refresh() {
      try {
        const value = await request(path + query, { signal: controller.signal })
        if (!stopped) {
          setAsset(value)
          setError('')
        }
        if (value.status !== 'ready') return
        const current = value.analyses?.find(
          (a) => a.session_id === sessionId && a.sequence === sequence,
        )
        if (!current || ['succeeded', 'failed', 'cancelled'].includes(current.status)) return
      } catch (e) {
        if (!stopped) {
          setError(e.message)
          setAsset(null)
        }
      }
      if (!stopped) timer = setTimeout(refresh, 5000)
    }
    refresh()
    return () => {
      stopped = true
      controller.abort()
      clearTimeout(timer)
    }
  }, [path, query, sessionId, sequence])
  const analysis = asset?.analyses?.find(
    (a) => a.session_id === sessionId && a.sequence === sequence,
  )
  const available = !revoked && asset?.status === 'ready'
  async function act(action) {
    if (busy) return
    setBusy(true)
    try {
      if (action === 'retry') {
        retryId.current ||= crypto.randomUUID()
        await post(path + '/analysis-retries', {
          request_id: retryId.current,
          session_id: sessionId,
          source_sequence: sequence,
        })
        setRetried(true)
      } else if (onRemove) await onRemove(id)
      else {
        await request(path, { method: 'DELETE' })
        setAsset((a) => ({ ...a, status: 'revoked' }))
      }
      setError('')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <figure className="chat-attachment">
      {available ? (
        <a href={`${API_BASE}${path}/content${query}`} target="_blank" rel="noreferrer">
          <img src={`${API_BASE}${path}/content${query}`} alt="用户上传的图片" loading="lazy" />
        </a>
      ) : (
        <span>{asset || revoked ? '图片已撤回或过期' : '图片加载中'}</span>
      )}
      <figcaption>
        {available && (retried ? '已提交重新识别' : labels[analysis?.status])}
      </figcaption>
      {available && !ticketId && (
        <button type="button" disabled={busy} onClick={() => act('remove')}>
          {onRemove ? '移除图片' : '撤回图片'}
        </button>
      )}
      {available && analysis?.status === 'failed' && !ticketId && !retried && (
        <button type="button" disabled={busy} onClick={() => act('retry')}>
          重新识别
        </button>
      )}
      {error && <small role="alert">{error}</small>}
    </figure>
  )
}

export default function Attachments({ message, sessionId, ticketId }) {
  if (!message?.attachment_ids?.length) return null
  return (
    <div className="chat-attachments">
      {message.attachment_ids.map((id) => (
        <Attachment
          key={id}
          id={id}
          sessionId={sessionId}
          sequence={message.sequence}
          ticketId={ticketId}
          revoked={message.revoked_attachment_ids?.includes(id)}
        />
      ))}
    </div>
  )
}
