import { useEffect, useState } from 'react'
import { API_BASE, request } from './api/client'

export default function InvoiceDocuments({ invoiceId }) {
  const [invoice, setInvoice] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let timer
    async function refresh() {
      try {
        const value = await request(
          `/api/commerce/invoice-requests/${encodeURIComponent(invoiceId)}`,
          {
            signal: controller.signal,
          },
        )
        if (controller.signal.aborted) return
        setInvoice(value)
        setError('')
        if (['issued', 'rejected', 'cancelled', 'closed'].includes(value.status)) return
      } catch (failure) {
        if (controller.signal.aborted) return
        setError(failure.message)
      }
      timer = setTimeout(refresh, 5000)
    }
    setInvoice(null)
    refresh()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [invoiceId])
  return (
    <div aria-label="票据文件">
      <p>{invoice?.is_demo ? '演示票据，非正式发票' : '开票进度与文件'}</p>
      {error && <p role="status">{error}</p>}
      {!invoice && !error && <p>正在查询开票进度…</p>}
      {invoice && !['issued', 'rejected'].includes(invoice.status) && (
        <p>申请处理中，文件尚未生成。</p>
      )}
      {invoice?.status === 'rejected' && <p>开票申请已被拒绝，请查询原因或联系人工客服。</p>}
      {invoice?.status === 'issued' &&
        !invoice.issued_invoices?.some((doc) => doc.document_available) && (
          <p>票据文件当前不可下载，请联系人工客服核实。</p>
        )}
      {(invoice?.issued_invoices || [])
        .filter((doc) => doc.document_available)
        .map((doc) => (
          <p key={doc.issued_invoice_id}>
            <a
              href={`${API_BASE}/api/commerce/invoices/${encodeURIComponent(doc.issued_invoice_id)}/document`}
              target="_blank"
              rel="noreferrer"
            >
              {doc.is_demo ? '下载演示票据' : '下载票据文件'}
            </a>
          </p>
        ))}
    </div>
  )
}
