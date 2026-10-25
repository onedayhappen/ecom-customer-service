"""Authenticated customer lists and invoice files with ownership checks."""
from typing import Literal
import pymysql
from fastapi import APIRouter, Depends, Request, HTTPException, Query, Response
from fastapi.responses import FileResponse
from app.api.support import identity, request_budget
from app.services.commerce import service
from app.services.commerce.common import BusinessError, decoded, ref
from app.services.commerce.providers import invoice_file
from app.data.membership_store import membership_facts

router=APIRouter(prefix='/api/commerce',tags=['commerce'],dependencies=[Depends(request_budget)])


@router.get('/profile')
def customer_profile(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    svc = service(identity(request))
    try:
        with svc.store.transaction() as tx:
            account = tx.one('SELECT username,created_at FROM users WHERE user_id=%s', (svc.user,))
            if not account:
                raise HTTPException(status_code=404, detail='账号资料不存在')
            platforms = tx.all('SELECT DISTINCT platform_code FROM memberships WHERE user_id=%s ORDER BY platform_code', (svc.user,))
            return {**account, 'memberships': [membership_facts(tx, svc.user, row['platform_code']) for row in platforms]}
    except (BusinessError, pymysql.MySQLError) as error:
        raise list_error(error) from None


def list_error(error):
    if isinstance(error, BusinessError):
        code = 400 if error.code in {'invalid_cursor', 'invalid_arguments', 'state_changed'} else 503
        return HTTPException(status_code=code, detail=error.message)
    return HTTPException(status_code=503, detail='暂时无法加载，请稍后重试')


@router.get('/orders')
def customer_orders(request: Request, response: Response,
                    status: Literal['all', 'pending_payment', 'paid', 'pending_receipt', 'after_sale'] = 'all',
                    cursor: str | None = Query(None, max_length=4096)):
    response.headers['Cache-Control'] = 'no-store'
    svc = service(identity(request))
    try:
        with svc.store.transaction() as tx:
            result = svc.find_orders(tx, status=status, cursor=cursor)
            for order in result['data']['items']:
                order['items'] = [dict(order_item_id=item['order_item_id'],
                    title_snapshot=item['title_snapshot'], spec_snapshot=decoded(item['spec_snapshot'], {}),
                    quantity=item['quantity']) for item in svc.order_rows(tx, 'order_items', order)]
            return {**result['data'], 'next_cursor': result['next_cursor']}
    except (BusinessError, pymysql.MySQLError) as error:
        raise list_error(error) from None


@router.get('/coupons')
def customer_coupons(request: Request, response: Response,
                     status: Literal['all'] = 'all', cursor: str | None = Query(None, max_length=4096),
                     coupon_type: Literal['all', 'shop', 'platform', 'category', 'newcomer', 'general'] = 'all'):
    response.headers['Cache-Control'] = 'no-store'
    svc = service(identity(request))
    try:
        with svc.store.transaction() as tx:
            result = svc.list_my_coupons(tx, status=status, cursor=cursor,
                                         exclude_expired=True, coupon_type=coupon_type)
            for coupon in result['data']['items']:
                template, instance, scopes = svc.coupon(tx, ref('coupon_instance', coupon['user_coupon_id']))
                basis = 'claim_snapshot'
                try:
                    template, scopes = svc.frozen_coupon(tx, template, instance, scopes)
                except BusinessError:
                    basis = 'current_template'
                coupon['template'] = {key: template.get(key) for key in (
                    'name', 'kind', 'currency', 'platform_code', 'threshold_amount',
                    'discount_amount', 'discount_bps', 'cap_amount')}
                coupon['rules_basis'] = basis
                coupon['scope_description'] = '全场适用' if any(s['scope_type'] == 'all' for s in scopes) else '限指定商家或商品使用'
            return {**result['data'], 'next_cursor': result['next_cursor']}
    except (BusinessError, pymysql.MySQLError) as error:
        raise list_error(error) from None

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
