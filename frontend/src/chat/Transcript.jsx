import { Fragment, useEffect, useRef } from 'react'
import { Bot, Headphones, UserRound } from 'lucide-react'
import { messageTimestamp } from '../chatTime'

function CustomerMessage({ message, typing = false }) {
  const role = message?.role ?? 'assistant'
  const isUser = role === 'user'
  const Avatar = isUser ? UserRound : role === 'agent' ? Headphones : Bot
  const Container = typing ? 'div' : 'article'
  return (
    <Container
      className={`customer-message ${typing ? 'reply-typing-message' : 'support-message'}${isUser ? ' from-user' : ''}`}
      role={typing ? 'status' : undefined}
      aria-label={typing ? '优优正在输入' : undefined}
    >
      <span className="message-avatar" role="img" aria-label={isUser ? '用户头像' : '客服头像'}>
        <Avatar size={20} />
      </span>
      <div className="message-content">
        {!isUser && <span className="message-sender">{role === 'agent' ? '客服' : '优优'}</span>}
        <div className={`message-bubble${typing ? ' reply-typing' : ''}`}>
          {typing ? (
            <>
              <span>正在输入中</span>
              <span className="reply-typing-dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
            </>
          ) : (
            <p>{message.content}</p>
          )}
        </div>
      </div>
    </Container>
  )
}

function StaffMessage({ message }) {
  return (
    <article
      className={`support-message ${message.role === 'user' ? 'from-user' : ''} ${message.internal ? 'internal-note' : ''}`}
    >
      <small>
        {message.role === 'user'
          ? '用户'
          : message.role === 'agent'
            ? `客服 ${message.actor}`
            : 'AI 客服'}
        {message.internal ? ' · 内部备注' : ''} · {new Date(message.at).toLocaleTimeString()}
      </small>
      <p>{message.content}</p>
    </article>
  )
}

export default function Transcript({
  messages,
  hasOlder = false,
  loadingHistory = false,
  loadOlder,
  showEmptyState = true,
  customerView = false,
  typing = false,
  replyTimedOut = false,
}) {
  const container = useRef(null)
  const lastSequence = messages.at(-1)?.sequence
  useEffect(() => {
    const node = container.current
    if (node) node.scrollTop = node.scrollHeight
  }, [lastSequence, typing, replyTimedOut])

  return (
    <div ref={container} className="support-transcript" aria-live="polite">
      {hasOlder && (
        <button onClick={loadOlder} disabled={loadingHistory}>
          {loadingHistory ? '加载中…' : '查看更早消息'}
        </button>
      )}
      {showEmptyState && messages.length === 0 && <p className="support-muted">暂无消息</p>}
      {messages.map((message, index) => {
        if (!customerView) return <StaffMessage key={message.sequence} message={message} />
        const timestamp = messageTimestamp(message.at, messages[index - 1]?.at)
        return (
          <Fragment key={message.sequence}>
            {timestamp && (
              <time className="message-time" dateTime={timestamp.dateTime}>
                {timestamp.label}
              </time>
            )}
            <CustomerMessage message={message} />
          </Fragment>
        )
      })}
      {customerView && typing && <CustomerMessage typing />}
      {customerView && replyTimedOut && (
        <CustomerMessage
          message={{ role: 'assistant', content: '抱歉，优优出错了，请稍后再试。' }}
        />
      )}
    </div>
  )
}
