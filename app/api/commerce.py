"""Authenticated customer lists and invoice files with ownership checks."""

from contextlib import contextmanager
from typing import Literal
import pymysql
from fastapi import APIRouter, Depends, Request, HTTPException, Query, Response
from fastapi.responses import FileResponse
from app.api.support import identity, request_budget
from app.services.commerce import service
from app.services.commerce.common import BusinessError, ref
from app.services.commerce.customer_views import CustomerViews
from app.services.commerce.providers import invoice_file

router = APIRouter(
    prefix="/api/commerce", tags=["commerce"], dependencies=[Depends(request_budget)]
)


@contextmanager
def customer_view(request: Request, response: Response):
    """Keep HTTP cache and error handling at the API boundary."""
    response.headers["Cache-Control"] = "no-store"
    try:
        yield CustomerViews(service(identity(request)))
    except (BusinessError, pymysql.MySQLError) as error:
        raise list_error(error) from None


def list_error(error):
    if isinstance(error, BusinessError):
        code = {
            "invalid_cursor": 400,
            "invalid_arguments": 400,
            "state_changed": 400,
            "account_not_found": 404,
        }.get(error.code, 503)
        return HTTPException(status_code=code, detail=error.message)
    return HTTPException(status_code=503, detail="暂时无法加载，请稍后重试")


@router.get("/profile")
def customer_profile(request: Request, response: Response):
    with customer_view(request, response) as view:
        return view.profile()


@router.get("/orders")
def customer_orders(
    request: Request,
    response: Response,
    status: Literal[
        "all", "pending_payment", "paid", "pending_receipt", "after_sale"
    ] = "all",
    cursor: str | None = Query(None, max_length=4096),
):
    with customer_view(request, response) as view:
        return view.orders(status=status, cursor=cursor)


@router.get("/coupons")
def customer_coupons(
    request: Request,
    response: Response,
    status: Literal["all"] = "all",
    cursor: str | None = Query(None, max_length=4096),
    coupon_type: Literal[
        "all", "shop", "platform", "category", "newcomer", "general"
    ] = "all",
):
    with customer_view(request, response) as view:
        return view.coupons(status=status, cursor=cursor, coupon_type=coupon_type)


@router.get("/invoices/{issued_invoice_id}/document")
def download_invoice(issued_invoice_id: str, request: Request):
    svc = service(identity(request))
    try:
        with svc.store.transaction() as tx:
            invoice, _ = svc.issued_invoice(tx, issued_invoice_id)
            if invoice["status"] != "issued":
                raise BusinessError("not_found", "发票文件不可用")
            path = invoice_file(invoice)
        return FileResponse(
            path,
            media_type=invoice["mime_type"],
            filename="invoice" + path.suffix,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except BusinessError as error:
        raise HTTPException(
            status_code=404 if error.code == "not_found" else 503, detail=error.message
        ) from None


@router.get("/invoice-requests/{invoice_id}")
def invoice_request_status(invoice_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = service(identity(request)).read(
            "get_invoices", {"target_ref": ref("invoice", invoice_id)}
        )
        return result["data"]["items"][0]
    except BusinessError as error:
        raise HTTPException(
            status_code=404 if error.code == "not_found" else 503, detail=error.message
        ) from None
