"""Extended workflows against disposable MySQL; no external provider calls."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import pytest
from app.services.commerce.common import BusinessError, dumps, ref, utcnow
from app.services.commerce.providers import provider_factory, invoice_file
from app.services.commerce import CommerceService
from tests.test_core_tools import database, svc, read, commit
from tests.test_message_association import store
from tests.test_task_storage import empty_store


@pytest.fixture
def extended(svc):
    now=utcnow()
    with svc.store.transaction(write=True) as tx:
        encrypted={k:svc.cipher.seal(v,'address:alice:'+k) for k,v in {'recipient_name':'张三','phone':'13800138000','detail':'一号楼'}.items()}
        tx.execute('INSERT INTO user_addresses(user_id,recipient_name_ciphertext,phone_ciphertext,province_code,city_code,district_code,detail_ciphertext,key_version,is_default,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,1,1,%s,%s)',('alice',encrypted['recipient_name'],encrypted['phone'],'p','c','d',encrypted['detail'],now,now))
        address=str(tx.one('SELECT address_id FROM user_addresses')['address_id'])
        tx.execute("INSERT INTO address_labels(address_id,label,province_name) VALUES (%s,'家里','浙江')",(address,))
        for sid in ['s1','s2']:
            tx.execute("INSERT INTO sku_fulfillment VALUES (%s,'warehouse',600,'test',%s)",(sid,now))
        tx.execute("INSERT INTO delivery_rules VALUES ('delivery',1,'shop','m','warehouse','p','*','*','CNY',1000,5,500,2,1,3,'test',%s,NULL,'active')",(now-timedelta(days=1),))
        for action in ['purchase','exchange','repair','price_protection','invoice_reissue']:
            rule=dict(schema_version=1,allowed_statuses=['paid','pending_payment'],local_authority=True,inventory_managed=True,coupons_managed=True)
            tx.execute('INSERT INTO business_policy_versions(policy_id,version,action,platform_code,rules,effective_from,source,status) VALUES (%s,1,%s,%s,%s,%s,%s,%s)',(action,action,'shop',dumps(rule),now-timedelta(days=365),'test','active'))
    svc.address_id=address
    return svc


class Provider:
    def __init__(self, *responses): self.responses=list(responses); self.calls=[]
    def call(self,operation,request_id,kind,payload=None):
        self.calls.append((operation,request_id,kind,payload))
        value=self.responses.pop(0)
        if isinstance(value,Exception): raise value
        return value


@pytest.mark.parametrize('days,other_delivered,expected',[(1,False,'eligible'),(30,True,'ineligible')])
@pytest.mark.parametrize('time_basis',['request','delivered'])
def test_sale_window_uses_requested_original_item(svc,days,other_delivered,expected,time_basis):
    now=utcnow()
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='return_refund'",(dumps(dict(schema_version=1,allowed_statuses=['paid'],window_days=7,time_basis=time_basis)),))
        tx.execute("INSERT INTO order_items(order_item_id,platform_code,order_id,sku_id,product_id,title_snapshot,spec_snapshot,quantity,unit_price,discount_amount,payable_amount) VALUES ('other-item','shop','paid','s2','p','other','{}',1,0,0,0)")
        for sid,item,qty,delivered in [('original','i-paid',3,now-timedelta(days=days)),('unrelated','other-item',1,now if other_delivered else None)]:
            tx.execute('INSERT INTO shipments(shipment_id,platform_code,order_id,status,shipped_at,delivered_at,created_at) VALUES (%s,\'shop\',\'paid\',%s,%s,%s,%s)',(sid,'delivered' if delivered else 'in_transit',now-timedelta(days=40),delivered,now))
            tx.execute('INSERT INTO shipment_items(shipment_id,order_item_id,quantity) VALUES (%s,%s,%s)',(sid,item,qty))
    result=read(svc,'assess_after_sale',kind='return_refund',items=[dict(order_item_id='i-paid',quantity=1)])
    assert result['data']['eligibility']==expected,result


def test_zero_amount_discount_and_full_discount_stack(extended):
    svc=extended
    lines=[dict(sku_id='free',amount=Decimal(0))]
    svc.allocate_discount(lines,Decimal(0))
    assert lines[0]['amount']==0
    with pytest.raises(BusinessError): svc.allocate_discount(lines,Decimal(1))
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE coupon_templates SET threshold_amount=0,discount_amount=20 WHERE coupon_id='c'")
    coupon=svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'claim-free')['data']['coupon_ref']
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE product_skus SET list_price=0 WHERE sku_id='s1'")
    assert read(svc,'estimate_purchase',items=[dict(sku_id='s1',quantity=1)],coupon_refs=[coupon])['data']['goods_after_discount']==0


def test_changed_coupon_rules_invalidate_confirmation(svc):
    p=svc.preview('claim_coupon',{'coupon_ref':ref('coupon_template','c')})
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE coupon_templates SET discount_amount=1 WHERE coupon_id='c'")
    with pytest.raises(BusinessError,match='规则已变化'):
        svc.submit('claim_coupon',p['arguments'],'claim-changed',p['business_version'])
    with svc.store.transaction() as tx: assert tx.one('SELECT COUNT(*) n FROM user_coupons')['n']==0


def test_address_change_rechecks_delivery_and_fee(extended):
    svc=extended
    quote=svc.quote_checkout(basket(svc))['data']['quote_ref']
    order=commit(svc,'prepare_purchase',{'quote_ref':quote},'address-order')
    args={'order_ref':order['order_ref'],'address_id':svc.address_id}
    prepared=svc.preview('prepare_order_address_change',args)
    assert prepared['review']['shipping_amount']=='7.00'
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE user_addresses SET province_code='unsupported'")
    with pytest.raises(BusinessError,match='运费规则'): svc.preview('prepare_order_address_change',args)
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE user_addresses SET province_code='p'")
        tx.execute("UPDATE delivery_rules SET base_fee=6")
    with pytest.raises(BusinessError,match='运费与原订单不同'):
        svc.submit('prepare_order_address_change',prepared['arguments'],'address-change',prepared['business_version'])
    with svc.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM order_address_changes')['n']==0
        assert tx.one('SELECT shipping_amount FROM orders WHERE order_id=%s',(order['order_id'],))['shipping_amount']==7


def test_standalone_platform_uses_trusted_business_records(svc):
    svc.scope=None
    assert read(svc,'get_membership')['data']['platform_code']=='shop'
    assert read(svc,'list_claimable_coupons')['data']['total']==1
    with svc.store.transaction(write=True) as tx:
        tx.execute('''INSERT INTO orders(platform_code,order_id,user_id,status,created_at,merchant_id,currency,
            goods_amount,discount_amount,shipping_amount,payable_amount,paid_amount,updated_at)
            SELECT 'other',order_id,user_id,status,created_at,merchant_id,currency,goods_amount,discount_amount,
            shipping_amount,payable_amount,paid_amount,updated_at FROM orders WHERE order_id='paid' ''')
    with pytest.raises(BusinessError,match='平台'): read(svc,'get_membership')


def test_new_customer_platform_requires_unique_business_configuration(svc):
    now=utcnow()
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO users(user_id,username,username_key,status,created_at,updated_at) VALUES ('new','new','new','active',%s,%s)",(now,now))
    fresh=CommerceService(svc.store,'new',secret=svc.secret)
    assert read(fresh,'list_claimable_coupons')['data']['total']==1
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO business_policy_versions(policy_id,version,action,platform_code,rules,effective_from,source,status) VALUES ('other-pricing',1,'pricing','other','{\"schema_version\":1}',%s,'test','active')",(now-timedelta(days=1),))
    with pytest.raises(BusinessError,match='平台'): read(fresh,'list_claimable_coupons')


def test_dispatch_confirmation_only_queues_and_replays(extended):
    svc=extended; provider=Provider()
    with provider_factory(lambda _:provider):
        p=svc.preview('request_dispatch',{'order_ref':ref('order','shop','paid')})
        assert not provider.calls
        with svc.store.transaction() as tx: assert tx.one('SELECT COUNT(*) n FROM commerce_provider_requests')['n']==0
        result=svc.submit('request_dispatch',p['arguments'],'confirmed-dispatch',p['business_version'])
        assert result['status']=='queued' and result['success']
        assert svc.submit('request_dispatch',p['arguments'],'confirmed-dispatch',p['business_version'])==result
        assert not provider.calls
        with svc.store.transaction() as tx: assert tx.one('SELECT COUNT(*) n FROM commerce_provider_requests')['n']==1


def test_after_sale_logistics_are_bounded_and_pageable(extended):
    svc=extended; now=utcnow()
    auth=dict(address={'detail':'退货仓'},carriers=[dict(code='SF',name='顺丰')],requirements=[],deadline=(now+timedelta(days=7)).isoformat())
    provider=Provider(dict(status='accepted',reference='repair-logistics',return_authorization=auth))
    with provider_factory(lambda _:provider):
        sale=commit(svc,'prepare_repair',dict(order_item_id='i-paid',quantity=1,problem='故障'),'repair-logistics')
        svc.process_provider_request('repair-logistics')
    aid=sale['after_sale_id']
    shipment=commit(svc,'prepare_return_shipment',dict(after_sale_id=aid,carrier_code='SF',tracking_number='SF123'))
    rid=shipment['return_shipment_id']
    with svc.store.transaction(write=True) as tx:
        authorization=tx.one('SELECT authorization_id FROM return_authorizations WHERE after_sale_id=%s',(aid,))['authorization_id']
        for i in range(5):
            tx.execute("INSERT INTO return_shipments(return_shipment_id,authorization_id,carrier_code,tracking_number,status,operation_id,created_at,updated_at) VALUES (%s,%s,'SF',%s,'registered',%s,%s,%s)",(f'extra{i}',authorization,f'SFextra{i}',f'parcel-op{i}',now,now))
        with tx.connection.cursor() as cur:
            cur.executemany("INSERT INTO return_shipment_events(event_id,return_shipment_id,status,description,source,event_time) VALUES (%s,%s,'in_transit','轨迹','test',%s)",[(f'event{i:04}',rid,now+timedelta(seconds=i)) for i in range(61)])
    detail=read(svc,'get_after_sale_status',after_sale_id=aid)['data']
    assert detail['return_shipments_total']==6 and detail['return_shipments_truncated']
    assert len(detail['return_shipments'])==5 and len(detail['return_events'])==3
    assert detail['return_events_total']==61
    parcels=read(svc,'get_logistics',target_ref=detail['logistics_ref'])
    assert parcels['data']['total']==6 and len(parcels['data']['items'])==5
    assert len(read(svc,'get_logistics',target_ref=detail['logistics_ref'],cursor=parcels['next_cursor'])['data']['items'])==1
    args=dict(target_ref=ref('return_shipment',rid)); ids=[]
    while True:
        page=read(svc,'get_logistics',**args)
        assert len(page['data']['items'])<=20 and page['data']['total']==61
        ids += [e['event_id'] for e in page['data']['items']]
        if not page['next_cursor']: break
        args['cursor']=page['next_cursor']
    assert ids==[f'event{i:04}' for i in range(61)]
    bob=CommerceService(svc.store,'bob',scope=svc.scope,secret=svc.secret)
    with pytest.raises(BusinessError): read(bob,'get_logistics',target_ref=ref('return_shipment',rid))


def test_search_sql_filter_pagination_and_batch_skus(extended):
    svc=extended; now=utcnow()
    with svc.store.transaction(write=True) as tx:
        with tx.connection.cursor() as cur:
            cur.executemany('INSERT INTO products(product_id,merchant_id,name,category,description,specs) VALUES (%s,\'m\',%s,\'耳机\',\'描述\',\'{}\')',[(f'bulk{i:05}',f'批量耳机{i}') for i in range(10005)])
            cur.executemany('INSERT INTO product_skus(sku_id,product_id,sku_code,spec_values,list_price,currency,status,created_at,updated_at) VALUES (%s,%s,%s,\'{"颜色":"黑"}\',10,\'CNY\',\'active\',%s,%s)',[(f'bulk{i:05}',f'bulk{i:05}',f'bulk{i:05}',now,now) for i in range(10005)])
    exact=read(svc,'search_products',query='批量耳机10004')
    assert exact['data']['total']==1
    args=dict(query='测试商家 批量',specs=[dict(name='颜色',value='黑')],max_price=dict(amount='10.00',currency='CNY'))
    first=read(svc,'search_products',**args)
    assert first['data']['total']==10005 and len(first['data']['items'])==10
    second=read(svc,'search_products',**args,cursor=first['next_cursor'])
    assert not {r['product_id'] for r in first['data']['items']} & {r['product_id'] for r in second['data']['items']}
    assert read(svc,'search_products',query='批量',specs=[dict(name='颜色',value='白')])['data']['total']==0
    unknown=read(svc,'search_products',query='批量耳机10004',specs=[dict(name='不存在',value='a')])
    assert unknown['data']['items'][0]['unapplied_conditions']==[dict(name='不存在',value='a')]
    with pytest.raises(BusinessError): read(svc,'search_products',query='别的条件',cursor=first['next_cursor'])


def basket(svc,quantity=2):
    return dict(items=[dict(sku_id='s1',quantity=quantity)],address_id=svc.address_id,coupon_refs=[])


def test_delivery_restock_addresses_specs_and_ownership(extended):
    svc=extended
    assert read(svc,'quote_delivery',items=basket(svc)['items'],address_id=svc.address_id)['data']['shipping_amount']==7
    assert read(svc,'list_addresses',query='家里')['data']['total']==1
    assert read(svc,'list_addresses',query='公司')['status']=='no_match'
    assert read(svc,'get_restock_plan',sku_id='s1')['status']=='no_match'
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE product_skus SET spec_values=%s WHERE sku_id='s1'",(dumps({'颜色':'黑'}),))
        tx.execute("INSERT INTO restock_plans VALUES ('rp','s1','warehouse',%s,%s,10,'planned','erp',%s)",(utcnow(),utcnow(),utcnow()))
    assert read(svc,'search_products',query='耳机',specs=[dict(name='颜色',value='黑')])['data']['items'][0]['sku_count']==1
    assert read(svc,'get_product_details',target_ref=ref('sku','s1'))['data']['items'][0]['spec_values']=={'颜色':'黑'}
    assert read(svc,'get_restock_plan',sku_id='s1')['data']['items'][0]['source']=='erp'
    bob=CommerceService(svc.store,'bob',scope=svc.scope)
    with pytest.raises(BusinessError): read(bob,'quote_delivery',items=basket(svc)['items'],address_id=svc.address_id)
    with svc.store.transaction(write=True) as tx: tx.execute('DELETE FROM delivery_rules')
    with pytest.raises(BusinessError): svc.quote_checkout(basket(svc))


def test_checkout_purchase_idempotency_coupon_and_expiration(extended):
    svc=extended; args=basket(svc)
    args['coupon_refs']=[svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'claim')['data']['coupon_ref']]
    quote=svc.quote_checkout(args)['data']
    assert quote['payable_amount']==22
    with svc.store.transaction() as tx: assert tx.one("SELECT reserved_qty FROM sku_inventory WHERE sku_id='s1'")['reserved_qty']==0
    preview=svc.preview('prepare_purchase',{'quote_ref':quote['quote_ref']})
    result=svc.submit('prepare_purchase',preview['arguments'],'buy',preview['business_version'])
    assert svc.submit('prepare_purchase',preview['arguments'],'buy',preview['business_version'])==result
    with svc.store.transaction(write=True) as tx:
        assert tx.one("SELECT reserved_qty FROM sku_inventory WHERE sku_id='s1'")['reserved_qty']==2
        assert tx.one('SELECT status FROM user_coupons')['status']=='reserved'
        tx.execute('UPDATE order_expirations SET expires_at=%s',(utcnow()-timedelta(seconds=1),))
    assert svc.expire_order('shop',result['order_id'])
    assert not svc.expire_order('shop',result['order_id'])
    with svc.store.transaction() as tx:
        assert tx.one("SELECT available_qty FROM sku_inventory WHERE sku_id='s1'")['available_qty']==20
        assert tx.one('SELECT status FROM user_coupons')['status']=='available'


def test_quote_change_and_concurrent_last_stock(extended):
    svc=extended
    quote=svc.quote_checkout(basket(svc))['data']
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE product_skus SET list_price=11 WHERE sku_id='s1'")
    with pytest.raises(BusinessError): svc.preview('prepare_purchase',{'quote_ref':quote['quote_ref']})
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE sku_inventory SET available_qty=2 WHERE sku_id='s1'")
    plans=[svc.preview('prepare_purchase',{'quote_ref':svc.quote_checkout(basket(svc))['data']['quote_ref']}) for _ in range(2)]
    def buy(pair):
        i,p=pair
        try: return svc.submit('prepare_purchase',p['arguments'],'buy-'+str(i),p['business_version'])
        except BusinessError: return None
    with ThreadPoolExecutor(2) as pool: results=list(pool.map(buy,enumerate(plans)))
    assert sum(r is not None for r in results)==1
    with svc.store.transaction() as tx:
        row=tx.one("SELECT available_qty,reserved_qty FROM sku_inventory WHERE sku_id='s1'")
        assert row==dict(available_qty=0,reserved_qty=2)


def test_membership_points_balance_is_not_one_page(extended):
    svc=extended; now=utcnow()
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO membership_programs(program_id,platform_code,program_type,name,status,rule_version,evaluation_rules) VALUES ('shop_growth','shop','growth','会员','active',1,'{}')")
        tx.execute("INSERT INTO membership_levels(platform_code,program_id,level_code,version,name,benefits,source) VALUES ('shop','shop_growth','gold',1,'金卡','[]','crm')")
        tx.execute("INSERT INTO memberships(membership_id,user_id,platform_code,program_id,level_code,level_version,starts_at,expires_at,status,updated_at) VALUES ('alice_member','alice','shop','shop_growth','gold',1,%s,%s,'active',%s)",(now,now+timedelta(days=1),now))
        for i in range(25):
            tx.execute("INSERT INTO points_ledger(entry_id,user_id,platform_code,delta,kind,source,created_at) VALUES (%s,'alice','shop',%s,'adjustment',%s,%s)",(str(i),-10 if i==24 else 10,str(i),now))
    page=read(svc,'list_points')
    assert len(page['data']['items'])==20 and page['data']['ledger_balance']==230
    assert len(read(svc,'list_points',cursor=page['next_cursor'])['data']['items'])==5
    assert read(svc,'get_membership')['data']['membership']['name']=='金卡'


def test_dispatch_timeout_queries_original_request(extended):
    svc=extended; args={'order_ref':ref('order','shop','paid')}
    with pytest.raises(BusinessError): svc.request_dispatch(args,'dispatch')
    provider=Provider(TimeoutError(),dict(status='accepted',reference='real-receipt'))
    with provider_factory(lambda _:provider):
        with pytest.raises(BusinessError,match='回执'): svc.request_dispatch(args,'dispatch')
        result=svc.request_dispatch(args,'dispatch')
    assert result['data']['provider_reference']=='real-receipt'
    assert [c[0] for c in provider.calls]==['submit','query']
    assert {c[1] for c in provider.calls}=={'dispatch'}


@pytest.mark.parametrize('dispatched',[False,True])
def test_pending_receipt_dispatch_checks_parcel_evidence(extended,dispatched):
    svc=extended
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE orders SET status='pending_receipt' WHERE order_id='paid'")
        if dispatched:
            tx.execute("INSERT INTO shipments(shipment_id,platform_code,order_id,tracking_number,carrier,status,shipped_at,created_at) VALUES ('dispatch-evidence','shop','paid','TEST','test','shipped',%s,%s)",(utcnow(),utcnow()))
    provider=Provider(dict(status='accepted',reference='dispatch-receipt'))
    with provider_factory(lambda _:provider):
        if dispatched:
            with pytest.raises(BusinessError,match='已有发货记录'):
                svc.request_dispatch({'order_ref':ref('order','shop','paid')},'dispatch-evidence-test')
            assert not provider.calls
        else:
            assert svc.request_dispatch({'order_ref':ref('order','shop','paid')},'dispatch-evidence-test')['data']['status']=='accepted'


def test_repair_return_authorization_and_tracking(extended):
    svc=extended
    auth=dict(address={'recipient':'售后仓','detail':'真实仓库'},carriers=[dict(code='SF',name='顺丰')],requirements=['附订单号'],deadline=(utcnow()+timedelta(days=7)).isoformat())
    provider=Provider(dict(status='accepted',reference='work-1',return_authorization=auth),dict(status='completed',reference='work-1'))
    with provider_factory(lambda _:provider):
        result=commit(svc,'prepare_repair',dict(order_item_id='i-paid',quantity=1,problem='无法开机'),'repair')
        assert result['status']=='submitted'
        svc.process_provider_request('repair')
        aid=result['after_sale_id']
        assert read(svc,'get_return_instructions',after_sale_id=aid)['data']['address']['detail']=='真实仓库'
        args=dict(after_sale_id=aid,carrier_code='SF',tracking_number='SF123')
        assert commit(svc,'prepare_return_shipment',args)['status']=='registered'
        with pytest.raises(BusinessError): svc.preview('prepare_return_shipment',args)
        svc.process_provider_request('repair')
    assert provider.calls[-1][3]['return_shipment']['tracking_number']=='SF123'
    assert read(svc,'get_after_sale_status',after_sale_id=aid)['data']['after_sale']['status']=='completed'


@pytest.mark.parametrize('outcome',['rejected','cancelled','completed'])
def test_exchange_inventory_lifecycle(extended,outcome):
    svc=extended
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE product_skus SET list_price=10 WHERE sku_id='s2'")
    response=dict(status='completed' if outcome=='completed' else 'rejected',reference='exchange-1')
    if outcome=='completed': response['replacement_shipment']=dict(tracking_number='SF999',carrier='SF',shipped_at=utcnow().isoformat(),sku_id='s2',quantity=1)
    provider=Provider(response)
    with provider_factory(lambda _:provider):
        result=commit(svc,'prepare_exchange',dict(order_item_id='i-paid',replacement_sku_id='s2',quantity=1,reason='换颜色'),'exchange')
        if outcome=='cancelled': commit(svc,'prepare_after_sale_cancel',dict(after_sale_id=result['after_sale_id']))
        else: svc.process_provider_request('exchange')
    with svc.store.transaction() as tx:
        stock=tx.one("SELECT available_qty,reserved_qty FROM sku_inventory WHERE sku_id='s2'")
        assert stock==dict(available_qty=19 if outcome=='completed' else 20,reserved_qty=0)
    if outcome=='completed':
        result=read(svc,'get_logistics',target_ref=ref('order','shop','paid'))['data']
        assert result['quantities'][0]['recorded_shipped']==0
        assert result['quantities'][0]['recorded_replacement_shipped']==1
        assert result['items'][0]['replacement']['replacement_sku_id']=='s2'


def test_price_protection_requires_payment_and_prevents_duplicate_difference(extended):
    svc=extended; now=utcnow()
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO order_item_benefits VALUES ('i-paid',%s,'cash_per_unit',1,'sale')",(now+timedelta(days=7),))
        tx.execute("INSERT INTO price_protection_prices VALUES ('pp','s1','shop','CNY',8,'cash_per_unit',%s,%s,'sale')",(now-timedelta(days=1),now+timedelta(days=1)))
        tx.execute("INSERT INTO payment_transactions(payment_id,platform_code,order_id,user_id,provider,amount,currency,status,created_at) VALUES ('payment','shop','paid','alice','test',30,'CNY','succeeded',%s)",(now,))
    assert read(svc,'assess_price_protection',order_item_id='i-paid')['data']['amount']==6
    provider=Provider(dict(status='completed',reference='pp-1',refund=dict(payment_id='payment',provider_refund_id='refund-real',amount='6.00',currency='CNY',completed_at=now.isoformat())))
    with provider_factory(lambda _:provider):
        result=commit(svc,'prepare_price_protection',dict(order_item_id='i-paid'),'protect')
        svc.process_provider_request('protect'); svc.process_provider_request('protect')
    assert read(svc,'get_refunds',target_ref=ref('after_sale',result['after_sale_id']))['data']['recorded_success_amount']==6
    assert read(svc,'assess_price_protection',order_item_id='i-paid')['data']['eligibility']=='ineligible'
    assert len(provider.calls)==1
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE after_sale_requests SET status='closed'")
    assert read(svc,'assess_price_protection',order_item_id='i-paid')['data']['eligibility']=='unknown'


def test_invoice_reissue_and_private_file(extended,tmp_path,monkeypatch):
    from app.config.settings import settings
    svc=extended; now=utcnow(); content=b'%PDF-1.7 test receipt'; checksum=hashlib.sha256(content).hexdigest()
    (tmp_path/'receipt.pdf').write_bytes(content)
    monkeypatch.setattr(settings,'commerce_invoice_root',str(tmp_path))
    original=commit(svc,'prepare_invoice',dict(order_ref=ref('order','shop','paid'),invoice_type='personal',title='张三'))
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE invoice_requests SET status='issued'")
        tx.execute("INSERT INTO issued_invoices VALUES ('original',%s,'old-number','issued','receipt.pdf','application/pdf',%s,'erp',%s,%s)",(original['invoice_id'],checksum,now,now))
    assert read(svc,'get_invoice_document',issued_invoice_id='original')['data']['download_url'].endswith('/original/document')
    bob=CommerceService(svc.store,'bob',scope=svc.scope)
    with pytest.raises(BusinessError): read(bob,'get_invoice_document',issued_invoice_id='original')
    provider=Provider(dict(status='completed',reference='invoice-1',original_voided=True,invoice=dict(invoice_number='new-number',document_key='receipt.pdf',mime_type='application/pdf',sha256=checksum,issued_at=now.isoformat())))
    with provider_factory(lambda _:provider):
        result=commit(svc,'prepare_invoice_reissue',dict(issued_invoice_id='original',invoice_type='personal',title='李四'),'reissue')
        svc.process_provider_request('reissue')
    with svc.store.transaction() as tx:
        assert tx.one("SELECT status FROM issued_invoices WHERE issued_invoice_id='original'")['status']=='voided'
        assert tx.one('SELECT status FROM invoice_requests WHERE invoice_id=%s',(result['invoice_id'],))['status']=='issued'
    with pytest.raises(BusinessError): invoice_file(dict(document_key='../escape.pdf',mime_type='application/pdf',sha256=checksum))
    (tmp_path/'receipt.pdf').write_bytes(b'corrupted')
    with pytest.raises(BusinessError): invoice_file(dict(document_key='receipt.pdf',mime_type='application/pdf',sha256=checksum))


def test_purchase_confirmation_through_task_ledger(extended,store,monkeypatch):
    from app.config.settings import settings
    from app.services.commerce import service_factory
    from app.tasks.operations import TaskOperations
    from tests.test_task_candidates import task
    from tests.test_task_execution import Client,response,request,engine
    svc=extended
    monkeypatch.setattr(settings,'task_operations_enabled',True)
    monkeypatch.setattr(settings,'api_jwt_secret','isolated-extended-confirmation-secret-key')
    rec,source=task(store,'s',goal='请帮我购买耳机',details={'platform_code':'shop'})
    quote=svc.quote_checkout(basket(svc))['data']['quote_ref']
    with service_factory(lambda user,scope:svc):
        client=Client(response(calls=[('prepare_purchase',{'quote_ref':quote})]))
        out=engine(client).execute_task(store,'alice',request(rec,source))
        assert out['state']=='needs_confirmation',out
        operations=TaskOperations(store,'alice')
        card=operations.list('s')[0]
        assert card['review']['address']['detail']=='一号楼'
        assert 'address' not in out.get('review',{})
        result=operations.confirm('s',card['operation_id'],card['confirmation_token'])
        assert result['status']=='succeeded',result
        assert operations.confirm('s',card['operation_id'],card['confirmation_token'])==result


def test_coupon_rule_checks_and_quote_expiration(extended):
    svc=extended
    coupon=svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'claim')['data']['coupon_ref']
    data=read(svc,'check_coupon',coupon_ref=coupon,target={'items':[dict(sku_id='s1',quantity=1)]})['data']
    assert data['eligibility']=='eligible' and len(data['checks'])==5
    assert all(c['source']=='coupon_claim_rules' for c in data['checks'])
    quote=svc.quote_checkout(basket(svc))['data']['quote_ref']
    with svc.store.transaction(write=True) as tx: tx.execute('UPDATE checkout_quotes SET expires_at=%s',(utcnow()-timedelta(seconds=1),))
    with pytest.raises(BusinessError): svc.preview('prepare_purchase',dict(quote_ref=quote))


def test_provider_repeated_receipt_does_not_invalidate_return_confirmation(extended):
    svc=extended
    receipt=dict(status='accepted',reference='work-1',return_authorization=dict(address={'detail':'售后仓'},carriers=[dict(code='SF',name='顺丰')],requirements=[],deadline=(utcnow()+timedelta(days=1)).isoformat()))
    provider=Provider(receipt,receipt)
    with provider_factory(lambda _:provider):
        result=commit(svc,'prepare_repair',dict(order_item_id='i-paid',quantity=1,problem='无法开机'),'repair')
        svc.process_provider_request('repair')
        args=dict(after_sale_id=result['after_sale_id'],carrier_code='SF',tracking_number='SF100')
        with svc.store.transaction() as tx:
            assert '售后仓' not in tx.one("SELECT result FROM commerce_provider_requests WHERE request_id='repair'")['result']
        prepared=svc.preview('prepare_return_shipment',args)
        svc.process_provider_request('repair')
        assert svc.submit('prepare_return_shipment',prepared['arguments'],'returned',prepared['business_version'])['status']=='registered'


def test_all_tools_schema_and_provider_validation():
    from app.tools.catalog import TOOLS,BY_NAME
    from app.tools.selection import for_task
    from app.tools.contracts import Question,CheckCoupon,CancelOrder
    from app.services.commerce.providers import HttpGateway,ProviderResponse
    from pydantic import ValidationError
    assert for_task('refund','任意问题')==frozenset(t.name for t in TOOLS)
    schema=BY_NAME['prepare_invoice_reissue'].definition()['function']['parameters']
    assert 'title' in schema['properties'] and 'title' in schema['required']
    assert CancelOrder(order_ref='ref').reason is None
    assert Question(question='哪一件？',choices=[{'label':'第一件','ref':'a'},{'label':'第二件','ref':'b'}])
    with pytest.raises(ValidationError): CheckCoupon(coupon_ref='c',target={'order_ref':'o','items':[{'sku_id':'s','quantity':1}]})
    with pytest.raises(ValidationError): ProviderResponse(status='completed',reference='r',refund={'amount':'-1.00'})
    with pytest.raises(BusinessError): HttpGateway({'url':'http://insecure.example','token':'test'})


def test_repair_confirmation_and_actual_external_completion(extended,store,monkeypatch):
    from app.config.settings import settings
    from app.services.commerce import service_factory
    from app.tasks.operations import TaskOperations
    from app.tasks.external_progress import ExternalProgress
    from app.tasks.repository import TaskRepository
    from tests.test_task_candidates import task
    svc=extended
    monkeypatch.setattr(settings,'task_operations_enabled',True)
    monkeypatch.setattr(settings,'task_external_progress_enabled',True)
    monkeypatch.setattr(settings,'api_jwt_secret','isolated-extended-confirmation-secret-key')
    rec,_=task(store,'s',goal='帮我维修耳机并跟进结果',details={'platform_code':'shop'})
    provider=Provider(dict(status='completed',reference='repair-real'))
    with service_factory(lambda user,scope:svc), provider_factory(lambda _:provider):
        operations=TaskOperations(store,'alice')
        operations.prepare('s',rec.task_id,rec.version,'prepare_repair',arguments=dict(order_item_id='i-paid',quantity=1,problem='无法开机'))
        card=operations.list('s')[0]
        assert operations.confirm('s',card['operation_id'],card['confirmation_token'])['status']=='succeeded'
        assert ExternalProgress(store).tick()==['updated']
        svc.process_provider_request(card['operation_id'])
        with store.transaction() as tx: tx.execute('UPDATE task_external_waits SET next_check=0')
        assert ExternalProgress(store).tick()==['completed']
    assert TaskRepository(store,'alice').get(rec.task_id).status=='completed'


def test_invalid_provider_completion_cannot_change_business_state(extended):
    svc=extended
    provider=Provider(dict(status='completed',reference='invalid',replacement_shipment=dict(tracking_number='X',carrier='SF',shipped_at=utcnow().isoformat(),sku_id='s2',quantity=999)))
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE product_skus SET list_price=10 WHERE sku_id='s2'")
    with provider_factory(lambda _:provider):
        result=commit(svc,'prepare_exchange',dict(order_item_id='i-paid',replacement_sku_id='s2',quantity=1,reason='换颜色'),'exchange')
        with pytest.raises(BusinessError): svc.process_provider_request('exchange')
    with svc.store.transaction() as tx:
        assert tx.one('SELECT status FROM service_work_orders')['status']=='pending_review'
        assert tx.one("SELECT reserved_qty FROM sku_inventory WHERE sku_id='s2'")['reserved_qty']==1
        assert tx.one('SELECT COUNT(*) AS n FROM shipments')['n']==0
