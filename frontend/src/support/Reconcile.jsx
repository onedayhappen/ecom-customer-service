import { useState } from 'react'
import { post } from '../api/client'

export default function Reconcile({ operation, onDone }) {
  const [evidence, setEvidence] = useState('')
  const [error, setError] = useState('')
  const [stopped, setStopped] = useState(false)
  const [busy, setBusy] = useState(false)
  async function submit(status) {
    setBusy(true)
    try {
      await post(
        `/api/v1/support/operations/${operation.operation_id}/${operation.status === 'running' ? 'mark-unknown' : 'reconcile'}`,
        { status, evidence, execution_stopped: stopped },
      )
      setEvidence('')
      await onDone()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div>
      <textarea
        aria-label="核实依据"
        value={evidence}
        onChange={(e) => setEvidence(e.target.value)}
        placeholder="填写进程终止或下游业务查询回执等核实依据（至少10字）"
      />
      {operation.status === 'running' ? (
        <>
          <label>
            <input
              type="checkbox"
              checked={stopped}
              onChange={(e) => setStopped(e.target.checked)}
            />
            已确认原执行进程停止，且不会继续提交业务操作
          </label>
          <button
            disabled={busy || !stopped || evidence.trim().length < 10}
            onClick={() => submit()}
          >
            转为结果待核实
          </button>
        </>
      ) : (
        <>
          <button
            disabled={busy || evidence.trim().length < 10}
            onClick={() => submit('succeeded')}
          >
            已核实成功
          </button>
          <button disabled={busy || evidence.trim().length < 10} onClick={() => submit('failed')}>
            已核实未执行
          </button>
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  )
}
