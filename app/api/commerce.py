"""Authenticated access to issued invoice files, with per-request ownership checks."""
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import FileResponse
from app.api.support import identity, request_budget
from app.services.commerce import service
from app.services.commerce.common import BusinessError
from app.services.commerce.providers import invoice_file

router=APIRouter(prefix='/api/commerce',tags=['commerce'],dependencies=[Depends(request_budget)])

@router.get('/invoices/{issued_invoice_id}/document')
def download_invoice(issued_invoice_id: str,request: Request):
    svc=service(identity(request))
    try:
        with svc.store.transaction() as tx:
            invoice,_=svc.issued_invoice(tx,issued_invoice_id)
            if invoice['status']!='issued': raise BusinessError('not_found','发票文件不可用')
            path=invoice_file(invoice)
        return FileResponse(path,media_type=invoice['mime_type'],filename='invoice'+path.suffix,
            headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
    except BusinessError as error:
        raise HTTPException(status_code=404 if error.code=='not_found' else 503,detail=error.message) from None
