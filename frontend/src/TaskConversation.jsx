import { useTaskConversation } from './chat/useTaskConversation'
import Transcript from './chat/Transcript'
import OperationConfirmation from './OperationConfirmation'
import { Attachment } from './chat/Attachments'

export default function TaskConversation({ identity }) {
  const {
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
  } = useTaskConversation(identity)
  return (
    <main className="support-shell customer-shell">
      <header className="support-top">
        <div>
          <h1>
            {state?.mode === 'human'
              ? '人工客服'
              : state?.mode === 'handoff_pending'
                ? '等待人工接入'
                : '智能咨询'}
          </h1>
        </div>
        <div className="support-toolbar">
          <button onClick={startNew} disabled={pending.length > 0 || uploading}>
            新建会话
          </button>
        </div>
      </header>
      <section className="support-detail">
        {(error || conversation.error) && (
          <div className="support-alert" role="alert">
            {error || conversation.error}
          </div>
        )}
        <Transcript
          {...conversation}
          sessionId={sessionId}
          showEmptyState={false}
          customerView
          typing={typing}
          replyTimedOut={replyTimedOut}
        />
        {state && state.mode !== 'bot' && (
          <div className="support-toolbar">
            <button onClick={() => changeControl('resume-bot')}>返回机器人</button>
          </div>
        )}
        {pending
          .filter((item) => item.status === 'failed')
          .map((item) => (
            <div className="support-banner" key={item.payload.request_id}>
              <span>{item.payload.message} · 等待核实接收结果</span>
              <button onClick={() => deliver(item.payload)}>重试这条消息</button>
            </div>
          ))}
        {operations.map((operation) => (
          <OperationConfirmation
            key={operation.operation_id}
            operation={operation}
            enabled={state?.mode === 'bot' && pending.length === 0}
            busy={!!busyOperations[operation.operation_id]}
            onConfirm={(value) => operate(value, 'confirm')}
            onCancel={(value) => operate(value, 'cancel')}
            onRefresh={(value) => operate(value, 'refresh')}
          />
        ))}
        <form className="support-compose" onSubmit={send}>
          <label className="image-upload">
            图片
            <input
              type="file"
              aria-label="添加图片"
              accept="image/jpeg,image/png,image/webp"
              multiple
              disabled={uploading || attachments.length >= 4}
              onChange={uploadImages}
            />
          </label>
          <textarea
            aria-label="咨询内容"
            rows={1}
            wrap="off"
            maxLength={4000}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleComposerKeyDown}
            title="Enter 发送，Shift+Enter 换行"
            placeholder="可以连续补充订单、原因和其他问题"
          />
          <button
            disabled={(!input.trim() && !attachments.length) || uploading || pending.length >= 20}
          >
            {uploading ? '上传中…' : '发送消息'}
          </button>
        </form>
        <div className="chat-attachments">
          {attachments.map((a) => (
            <Attachment key={a.attachment_id} id={a.attachment_id} onRemove={removeAttachment} />
          ))}
        </div>
      </section>
    </main>
  )
}
