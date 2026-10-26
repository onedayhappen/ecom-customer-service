"""Document-aligned order changes against an isolated MySQL database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.commerce.common import BusinessError, decoded, dumps, ref, utcnow
from app.services.commerce.providers import provider_factory
from app.tools.contracts import UpdateOrder
from tests.test_core_tools import database, svc, read, commit
from tests.test_extended_tools import extended, basket, Provider
from tests.test_message_association import store
from tests.test_task_storage import empty_store


def paid(svc, shipped=False, split=False):
    now=utcnow()
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE business_policy_versions SET rules=JSON_SET(rules,'$.allowed_statuses',JSON_ARRAY('pending_payment','paid','pending_receipt')) WHERE action='cancel_order'")
        tx.execute("UPDATE orders SET shipping_amount=5,payable_amount=35,paid_amount=35 WHERE order_id='paid'")
        for ident,amount in ([('pay-a',20),('pay-b',15)] if split else [('pay-a',35)]):
            tx.execute('''INSERT INTO payment_transactions(payment_id,platform_code,order_id,user_id,provider,provider_txn_id,amount,currency,status,paid_at,created_at)
                VALUES (%s,'shop','paid','alice','gateway',%s,%s,'CNY','succeeded',%s,%s)''', (ident,'original-'+ident,amount,now-timedelta(days=1),now-timedelta(days=1)))
        if shipped:
            tx.execute("UPDATE orders SET status='pending_receipt' WHERE order_id='paid'")
            tx.execute("INSERT INTO shipments(shipment_id,platform_code,order_id,status,shipped_at,created_at) VALUES ('parcel','shop','paid','in_transit',%s,%s)",(now-timedelta(days=1),now-timedelta(days=1)))
            tx.execute("INSERT INTO shipment_items(shipment_id,order_item_id,quantity) VALUES ('parcel','i-paid',3)")


def cancel_args():
    return dict(order_ref=ref('order','shop','paid'),reason='不需要了')


def completed(svc, aid, *, returned=False):
    now=utcnow()
    with svc.store.transaction() as tx:
        refunds=tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s',(aid,))
    return dict(status='completed',reference='merchant-receipt',
        cancellation=dict(approved=True,fulfillment_stopped=True,returns=[dict(shipment_id='parcel',refused_at=now-timedelta(hours=3),returned_at=now-timedelta(hours=2),inspected_at=now-timedelta(hours=1))] if returned else []),
        refunds=[dict(payment_id=r['payment_id'],provider_refund_id='refund-'+r['payment_id'],amount=str(r['amount']),currency='CNY',completed_at=now.isoformat()) for r in refunds])


def test_paid_cancel_waits_for_review_and_original_refund(svc):
    paid(svc,split=True)
    provider=Provider(dict(status='accepted',reference='review'))
    with provider_factory(lambda _:provider):
        prepared=svc.preview('prepare_order_cancel',cancel_args())
        assert prepared['review']['refund_amount']=='35.00'
        assert Decimal(prepared['review']['shipping_deduction'])==0
        result=svc.submit('prepare_order_cancel',prepared['arguments'],'cancel-paid',prepared['business_version'])
        assert result['status']=='submitted' and not provider.calls
        assert svc.submit('prepare_order_cancel',prepared['arguments'],'cancel-paid',prepared['business_version'])==result
        svc.process_provider_request('cancel-paid')
        with svc.store.transaction() as tx:
            assert tx.one("SELECT status FROM orders WHERE order_id='paid'")['status']=='after_sale'
            assert {r['status'] for r in tx.all('SELECT status FROM refund_transactions')}=={'pending'}
        provider.responses.append(completed(svc,result['after_sale_id']))
        svc.process_provider_request('cancel-paid')
        svc.process_provider_request('cancel-paid')
    assert len(provider.calls)==2
    with svc.store.transaction() as tx:
        assert tx.one("SELECT status FROM orders WHERE order_id='paid'")['status']=='cancelled'
        assert tx.one("SELECT SUM(amount) amount FROM refund_transactions WHERE status='succeeded'")['amount']==35
        assert tx.one('SELECT available_qty FROM sku_inventory WHERE sku_id=\'s1\'')['available_qty']==20


def test_shipped_cancel_requires_all_refusal_and_inspection_receipts(svc):
    paid(svc,shipped=True)
    provider=Provider()
    with provider_factory(lambda _:provider):
        result=commit(svc,'prepare_order_cancel',cancel_args(),'cancel-shipped')
        assert Decimal(result['refund_amount'])==30 and Decimal(result['shipping_deduction'])==5
        response=completed(svc,result['after_sale_id'])
        with pytest.raises(BusinessError,match='拒收退回验货'):
            with svc.store.transaction(write=True) as tx:
                job=tx.one('SELECT * FROM commerce_provider_requests')
                order=svc.owned_order(tx,cancel_args()['order_ref'],lock=True)
                svc.apply_cancellation_response(tx,job,order,response)
        provider.responses.append(completed(svc,result['after_sale_id'],returned=True))
        svc.process_provider_request('cancel-shipped')
    with svc.store.transaction() as tx:
        assert tx.one('SELECT amount,status FROM refund_transactions')==dict(amount=Decimal(30),status='succeeded')
        assert tx.one("SELECT status FROM orders WHERE order_id='paid'")['status']=='cancelled'


@pytest.mark.parametrize('problem',['wrong_amount','future_refund','no_approval','missing_payment'])
def test_invalid_refund_receipt_never_completes(svc,problem):
    paid(svc)
    with provider_factory(lambda _:Provider()): result=commit(svc,'prepare_order_cancel',cancel_args(),'invalid')
    response=completed(svc,result['after_sale_id'])
    if problem=='wrong_amount': response['refunds'][0]['amount']='99.00'
    if problem=='future_refund': response['refunds'][0]['completed_at']=(utcnow()+timedelta(days=1)).isoformat()
    if problem=='no_approval': response['cancellation']['approved']=False
    if problem=='missing_payment': response['refunds'][0]['payment_id']='unrelated'
    with pytest.raises(BusinessError):
        with svc.store.transaction(write=True) as tx:
            svc.apply_cancellation_response(tx,tx.one('SELECT * FROM commerce_provider_requests'),svc.owned_order(tx,cancel_args()['order_ref'],lock=True),response)
    with svc.store.transaction() as tx:
        assert tx.one('SELECT status FROM refund_transactions')['status']=='pending'
        assert tx.one("SELECT status FROM orders WHERE order_id='paid'")['status']=='after_sale'


def test_cancel_rejection_restores_order_and_releases_refund_reservation(svc):
    paid(svc)
    provider=Provider(dict(status='rejected',reference='rejected'))
    with provider_factory(lambda _:provider):
        commit(svc,'prepare_order_cancel',cancel_args(),'rejected')
        svc.process_provider_request('rejected')
    with svc.store.transaction() as tx:
        assert tx.one("SELECT status FROM orders WHERE order_id='paid'")['status']=='paid'
        assert tx.one('SELECT status FROM refund_transactions')['status']=='cancelled'


def test_concurrent_cancel_reserves_refund_once(svc):
    paid(svc)
    with provider_factory(lambda _:Provider()): prepared=svc.preview('prepare_order_cancel',cancel_args())
    def submit(oid):
        with provider_factory(lambda _:Provider()):
            try: return svc.submit('prepare_order_cancel',prepared['arguments'],oid,prepared['business_version'])
            except BusinessError: return None
    with ThreadPoolExecutor(2) as pool: results=list(pool.map(submit,['first','second']))
    assert sum(r is not None for r in results)==1
    with svc.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM refund_transactions')['n']==1


def test_received_and_restricted_goods_cannot_cancel(svc):
    paid(svc,shipped=True)
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE shipments SET status='delivered',delivered_at=%s",(utcnow(),))
    assert read(svc,'assess_order_action',order_ref=cancel_args()['order_ref'],action='cancel_order')['data']['eligibility']=='ineligible'
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE products SET specs=JSON_OBJECT('cancel_allowed',false)")
    with pytest.raises(BusinessError,match='不可取消'):
        svc.preview('prepare_order_cancel',dict(order_ref=ref('order','shop','unpaid')))


def test_order_edits_encrypt_invoice_preserve_address_and_require_reconfirmation(extended):
    svc=extended
    order=commit(svc,'prepare_purchase',{'quote_ref':svc.quote_checkout(basket(svc))['data']['quote_ref']})
    args=dict(order_ref=order['order_ref'],phone='13900000000',note='送货前联系',invoice=dict(invoice_type='company',title='测试企业',tax_number='TAX-TEST'))
    prepared=svc.preview('prepare_order_update',args)
    assert '13900000000' not in dumps(prepared['review'])
    assert svc.confirmation_review(prepared)['changes']['phone']=='13900000000'
    result=svc.submit('prepare_order_update',prepared['arguments'],'edit',prepared['business_version'])
    assert result['status']=='updated'
    assert svc.submit('prepare_order_update',prepared['arguments'],'edit',prepared['business_version'])==result
    assert read(svc,'get_order',order_ref=order['order_ref'])['data']['note']=='送货前联系'
    with svc.store.transaction() as tx:
        assert b'TAX-TEST' not in tx.one('SELECT details_ciphertext FROM order_edit_details')['details_ciphertext']
        assert tx.one('SELECT COUNT(*) n FROM invoice_requests')['n']==0
        address=tx.one('SELECT * FROM user_addresses')
        assert svc.cipher.open(address['phone_ciphertext'],'address:alice:phone',address['key_version'])!='13900000000'
    later=svc.preview('prepare_order_update',dict(order_ref=order['order_ref'],note='新备注'))
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE orders SET status='pending_receipt' WHERE order_id=%s",(order['order_id'],))
        tx.execute("INSERT INTO shipments(shipment_id,platform_code,order_id,status,shipped_at,created_at) VALUES ('sent','shop',%s,'shipped',%s,%s)",(order['order_id'],utcnow(),utcnow()))
    with pytest.raises(BusinessError,match='已发货'):
        svc.submit('prepare_order_update',later['arguments'],'edit-later',later['business_version'])


def test_paid_unshipped_edits_only_phone_and_note(extended):
    svc=extended
    order=commit(svc,'prepare_purchase',{'quote_ref':svc.quote_checkout(basket(svc))['data']['quote_ref']})
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE orders SET status='paid',paid_amount=payable_amount WHERE order_id=%s",(order['order_id'],))
    assert commit(svc,'prepare_order_update',dict(order_ref=order['order_ref'],phone='13900000000',note=''))['status']=='updated'
    for fields in [dict(recipient_name='其他人'),dict(invoice=dict(invoice_type='personal',title='新抬头'))]:
        with pytest.raises(BusinessError,match='仅可修改电话和备注'):
            svc.preview('prepare_order_update',dict(order_ref=order['order_ref'],**fields))
    with pytest.raises(BusinessError): svc.preview('prepare_order_address_change',dict(order_ref=order['order_ref'],address_id=svc.address_id))


def test_same_city_address_reprices_unpaid_order(extended):
    svc=extended
    order=commit(svc,'prepare_purchase',{'quote_ref':svc.quote_checkout(basket(svc))['data']['quote_ref']})
    args=dict(order_ref=order['order_ref'],address_id=svc.address_id)
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE user_addresses SET city_code='other'")
    with pytest.raises(BusinessError,match='同城'): svc.preview('prepare_order_address_change',args)
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE user_addresses SET city_code='c'")
        tx.execute('UPDATE delivery_rules SET base_fee=6')
    prepared=svc.preview('prepare_order_address_change',args)
    assert prepared['review']['shipping_amount']=='8.00'
    svc.submit('prepare_order_address_change',prepared['arguments'],'same-city',prepared['business_version'])
    with svc.store.transaction() as tx:
        row=tx.one('SELECT shipping_amount,payable_amount FROM orders WHERE order_id=%s',(order['order_id'],))
        assert row['shipping_amount']==8 and row['payable_amount']==28


def test_update_contract_rejects_empty_and_product_changes():
    for changes in [{},dict(quantity=2),dict(phone='abc'),dict(invoice=dict(invoice_type='company',title='缺税号'))]:
        with pytest.raises(ValidationError): UpdateOrder(order_ref='order',**changes)


def test_cancel_confirmation_tracks_final_result(svc,store,monkeypatch):
    from app.config.settings import settings
    from app.services.commerce import service_factory
    from app.tasks.operations import TaskOperations
    from app.tasks.external_progress import ExternalProgress
    from app.tasks.repository import TaskRepository
    from tests.test_task_candidates import task
    paid(svc)
    monkeypatch.setattr(settings,'task_operations_enabled',True)
    monkeypatch.setattr(settings,'task_external_progress_enabled',True)
    monkeypatch.setattr(settings,'api_jwt_secret','isolated-cancellation-confirmation-secret')
    rec,_=task(store,'s',goal='取消已付款订单并跟进退款结果',details={'platform_code':'shop'})
    provider=Provider()
    with service_factory(lambda user,scope:svc),provider_factory(lambda _:provider):
        operations=TaskOperations(store,'alice')
        operations.prepare('s',rec.task_id,rec.version,'prepare_order_cancel',arguments=cancel_args())
        card=operations.list('s')[0]
        assert operations.confirm('s',card['operation_id'],card['confirmation_token'])['status']=='succeeded'
        assert TaskRepository(store,'alice').get(rec.task_id).status=='waiting_external'
        assert ExternalProgress(store).tick()==['updated']
        with svc.store.transaction() as tx: aid=tx.one('SELECT after_sale_id FROM after_sale_requests')['after_sale_id']
        provider.responses.append(completed(svc,aid))
        svc.process_provider_request(card['operation_id'])
        with store.transaction() as tx: tx.execute('UPDATE task_external_waits SET next_check=0')
        assert ExternalProgress(store).tick()==['completed']
    assert TaskRepository(store,'alice').get(rec.task_id).status=='completed'
