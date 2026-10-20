// 09 will supply a server-authorized operation. 08 exposes no submission route.
export default function OperationConfirmation({ operation, enabled = false, busy = false, onConfirm, onCancel }) {
  if (!operation) return null
  return <section className="support-banner" aria-label="操作确认">
    <strong>{operation.title}</strong>
    <span>{operation.summary}</span>
    <button disabled={!enabled || busy || !onConfirm} onClick={() => onConfirm(operation.operation_id)}>确认这项操作</button>
    <button disabled={busy || !onCancel} onClick={() => onCancel(operation.operation_id)}>暂不执行</button>
  </section>
}
