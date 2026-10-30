# 文件说明：在一次性 MySQL 中测试核心工具契约、归属、规则与事务行为。
"""Real MySQL tests in a disposable, uniquely named database (never the application DB)."""
import base64
import json
import os
from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
import pymysql
from app.data.order_dao import parse_mysql_dsn
from app.data.business_store import BusinessStore
from app.services.commerce import CommerceService,service_factory
from app.services.commerce.common import BusinessError,decoded,dumps,ref,utcnow
from app.tools.catalog import TOOLS
from app.tools.registry import execute_tool
from app.tools.request_context import set_current_user_id,reset_current_user_id
from app.tasks.candidates import BusinessScope

@pytest.fixture(scope='module')
def database():
    admin=os.environ.get('CORE_TOOLS_TEST_MYSQL_ADMIN')
    if not admin: pytest.skip('CORE_TOOLS_TEST_MYSQL_ADMIN must explicitly enable disposable MySQL tests')
    params=parse_mysql_dsn(admin); params.pop('database',None)
    conn=pymysql.connect(**params,autocommit=True)
    name='test_core_tools_'+uuid4().hex
    assert name.startswith('test_core_tools_')
    with conn.cursor() as cur: cur.execute('CREATE DATABASE `'+name+'` CHARACTER SET utf8mb4')
    dsn=admin.rsplit('/',1)[0]+'/'+name
    try:
        from scripts.migrate_mysql_business_schema import migrate
        assert migrate(dsn)
        assert migrate(dsn,check=True)
        yield BusinessStore(dsn)
    finally:
        with conn.cursor() as cur: cur.execute('DROP DATABASE `'+name+'`')
        conn.close()

@pytest.fixture
def svc(database):
    now=utcnow()
    with database.transaction(write=True) as tx:
        tables=tx.all('SHOW TABLES')
        tx.execute('SET FOREIGN_KEY_CHECKS=0')
        for table in tables:
            name=next(iter(table.values()))
            if name!='schema_migrations': tx.execute('DELETE FROM `'+name+'`')
        tx.execute('SET FOREIGN_KEY_CHECKS=1')
        for user in ['alice','bob']:
            tx.execute('INSERT INTO users(user_id,username,username_key,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)',(user,user,user,'active',now,now))
        tx.execute("INSERT INTO merchants(merchant_id,name,category,description,status) VALUES ('m','测试商家','耳机','描述','active')")
        tx.execute("INSERT INTO products(product_id,merchant_id,name,category,description,specs) VALUES ('p','m','耳机','耳机','通勤耳机','{}')")
        for sid,price in [('s1',Decimal('10.00')),('s2',Decimal('20.00'))]:
            tx.execute('INSERT INTO product_skus(sku_id,product_id,sku_code,spec_values,list_price,currency,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',(sid,'p',sid,'{}',price,'CNY','active',now,now))
            tx.execute('INSERT INTO sku_inventory(sku_id,available_qty,reserved_qty,version,updated_at) VALUES (%s,20,0,0,%s)',(sid,now))
        for oid,user,status,paid in [('paid','alice','paid',30),('unpaid','alice','pending_payment',0),('other','bob','paid',30)]:
            tx.execute('INSERT INTO orders(platform_code,order_id,user_id,status,created_at,merchant_id,currency,goods_amount,discount_amount,shipping_amount,payable_amount,paid_amount,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,30,0,0,30,%s,%s)',('shop',oid,user,status,now,'m','CNY',paid,now))
            tx.execute('INSERT INTO order_items(order_item_id,platform_code,order_id,sku_id,product_id,title_snapshot,spec_snapshot,quantity,unit_price,discount_amount,payable_amount) VALUES (%s,%s,%s,%s,%s,%s,%s,3,10,0,30)',('i-'+oid,'shop',oid,'s1','p','耳机','{}'))
        for action in ['refund_only','return_refund','invoice','cancel_order','change_address','after_sale_cancel','pricing']:
            rule=dict(schema_version=1,allowed_statuses=['paid','pending_payment'],local_authority=True,inventory_managed=True,coupons_managed=True)
            tx.execute('INSERT INTO business_policy_versions(policy_id,version,action,platform_code,rules,effective_from,source,status) VALUES (%s,1,%s,%s,%s,%s,%s,%s)',(action,action,'shop',dumps(rule),now-timedelta(days=365),'test','active'))
        tx.execute('INSERT INTO coupon_templates(coupon_id,merchant_id,platform_code,name,kind,currency,threshold_amount,discount_amount,total_limit,per_user_limit,claim_starts_at,claim_ends_at,valid_days,status) VALUES (%s,%s,%s,%s,%s,%s,10,5,2,1,%s,%s,7,%s)',('c','m','shop','满十减五','amount_off','CNY',now-timedelta(days=1),now+timedelta(days=1),'active'))
        tx.execute("INSERT INTO coupon_scopes(coupon_id,scope_type,scope_id) VALUES ('c','all','*')")
    return CommerceService(database,'alice',scope=BusinessScope(platform_code='shop'),secret='test-only-cursor-signing-key',keys=json.dumps({'1':base64.urlsafe_b64encode(b'k'*32).decode()}))

def read(svc,name,**args): return svc.read(name,args)

def commit(svc,name,args,oid=None):
    p=svc.preview(name,args)
    return svc.submit(name,p['arguments'],oid or uuid4().hex,p['business_version'])


@pytest.fixture
def standard_provider(svc,monkeypatch):
    """Application-only tests configure a channel but never send HTTP requests."""
    from app.config.settings import settings
    monkeypatch.setattr(settings,'commerce_provider_mode','http')
    monkeypatch.setattr(settings,'commerce_provider_config',json.dumps({k:{'url':'https://unused.invalid','token':'test'} for k in ('refund_only','return_refund','invoice')}))
    with svc.store.transaction(write=True) as tx:
        tx.execute('''INSERT INTO payment_transactions(payment_id,platform_code,order_id,user_id,provider,provider_txn_id,amount,currency,status,created_at)
            VALUES ('payment-standard','shop','paid','alice','test','test-paid',30,'CNY','succeeded',%s)''',(utcnow(),))
    return svc

def test_catalog_and_strict_validation():
    assert len(TOOLS)==56
    token=set_current_user_id('alice')
    try:
        for t in TOOLS:
            r=json.loads(execute_tool(t.name,{'user_id':'bob'}))
            assert not r['ok'] and r['error']['code']=='invalid_arguments'
    finally: reset_current_user_id(token)

def test_public_and_owned_reads(svc):
    assert read(svc,'get_my_profile')['data']['username']=='alice'
    assert 'users' in read(svc,'get_my_profile')['sources'][0]['tables']
    assert read(svc,'list_addresses')['data']['total']==0
    assert read(svc,'list_platform_accounts')['data']['total']==0
    assert read(svc,'search_products',query='耳机',max_price={'amount':'15.00','currency':'CNY'})['data']['items'][0]['sku_summary'][0]['sku_id']=='s1'
    assert read(svc,'get_product',product_id='p')['data']['total']==2
    assert read(svc,'get_sku',sku_id='s1')['data']['inventory']['available_qty']==20
    assert len(read(svc,'compare_skus',sku_ids=['s1','s2'])['data']['items'])==2
    assert read(svc,'get_merchant',merchant_id='m')['data']['name']=='测试商家'
    assert read(svc,'find_orders',status='all')['data']['total']==2
    assert read(svc,'get_order_items',order_ref=ref('order','shop','paid'))['data']['items'][0]['quantity']==3
    with pytest.raises(BusinessError,match='订单不存在'): read(svc,'get_order',order_ref=ref('order','shop','other'))
    with pytest.raises(BusinessError): read(svc,'get_order',order_ref=ref('order','other-platform','paid'))

def test_price_history_includes_interval_before_window(svc):
    now=utcnow()
    with svc.store.transaction(write=True) as tx:
        tx.execute('INSERT INTO sku_price_history(sku_id,price_type,amount,currency,effective_from,source) VALUES (%s,%s,9,%s,%s,%s)',('s1','list','CNY',now-timedelta(days=60),'test'))
    assert read(svc,'get_price_history',sku_id='s1',days=30)['data']['recorded_minimum']['list:CNY']==9

def test_coupon_claim_idempotent_and_limit_under_concurrency(svc):
    args={'coupon_ref':ref('coupon_template','c')}
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:svc.claim_coupon(args,'same-request'),range(2)))
    assert results[0]==results[1]
    with pytest.raises(BusinessError,match='限制'): svc.claim_coupon(args,'different-request')
    assert read(svc,'list_my_coupons')['data']['total']==1
    with svc.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM business_operation_receipts')['n']==1
        assert tx.one('SELECT COUNT(*) AS n FROM coupon_claim_rules')['n']==1

def test_coupon_queries_estimate_and_frozen_rules(svc):
    assert read(svc,'list_claimable_coupons')['data']['total']==1
    assert read(svc,'list_promotions')['data']['total']==0
    value=svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'claim')['data']['coupon_ref']
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE coupon_templates SET discount_amount=1 WHERE coupon_id='c'")
    assert read(svc,'get_coupon',coupon_ref=value)['data']['rules_basis']=='claim_snapshot'
    out=read(svc,'estimate_purchase',items=[{'sku_id':'s1','quantity':2}],coupon_refs=[value])
    assert out['data']['goods_after_discount']==15 and out['data']['payable_amount'] is None
    assert read(svc,'check_coupon',coupon_ref=value,order_ref=ref('order','shop','unpaid'))['data']['discount_amount']==5
    with pytest.raises(BusinessError): read(svc,'estimate_purchase',items=[{'sku_id':'s1','quantity':2}],coupon_refs=[ref('coupon_template','c')])

def test_coupon_used_and_expired_are_distinct(svc):
    value=svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'claim')['data']['coupon_ref']
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE user_coupons SET status='used',expires_at=%s",(utcnow()-timedelta(days=1),))
    wallet=read(svc,'list_my_coupons',status='used')['data']['items']
    assert len(wallet)==1 and wallet[0]['is_expired']
    assert read(svc,'list_my_coupons',status='expired')['data']['total']==0

def test_assessment_has_no_write_and_partial_refund_reservation(svc,standard_provider):
    args={'kind':'refund_only','items':[{'order_item_id':'i-paid','quantity':1}]}
    assert read(svc,'assess_after_sale',**args)['data']['amount']==10
    with svc.store.transaction() as tx: assert tx.one('SELECT COUNT(*) AS n FROM after_sale_requests')['n']==0
    submitted=commit(svc,'prepare_after_sale',dict(args,reason='不需要了'))
    aid=submitted['after_sale_id']
    assert read(svc,'list_after_sales')['data']['total']==1
    assert read(svc,'get_after_sale',after_sale_id=aid)['data']['items'][0]['quantity']==1
    assert read(svc,'get_refunds',after_sale_id=aid)['data']['recorded_success_amount']==0
    assert read(svc,'assess_after_sale',kind='refund_only',items=[{'order_item_id':'i-paid','quantity':3}])['data']['eligibility']=='ineligible'
    assert commit(svc,'prepare_after_sale_cancel',{'after_sale_id':aid,'reason':'撤回'})['status']=='cancelled'

def test_unknown_policy_is_not_eligible_and_does_not_prepare(svc):
    with svc.store.transaction(write=True) as tx: tx.execute("DELETE FROM business_policy_versions WHERE action='refund_only'")
    args={'kind':'refund_only','items':[{'order_item_id':'i-paid','quantity':1}]}
    assert read(svc,'assess_after_sale',**args)['data']['eligibility']=='unknown'
    with pytest.raises(BusinessError): svc.preview('prepare_after_sale',dict(args,reason='退款'))

def test_prepared_version_changes_and_duplicate_submission(svc,standard_provider):
    args={'kind':'refund_only','items':[{'order_item_id':'i-paid','quantity':1}],'reason':'退款'}
    p=svc.preview('prepare_after_sale',args)
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE orders SET status='pending_receipt' WHERE order_id='paid'")
    with pytest.raises(BusinessError): svc.submit('prepare_after_sale',p['arguments'],'op',p['business_version'])
    with svc.store.transaction(write=True) as tx: tx.execute("UPDATE orders SET status='paid' WHERE order_id='paid'")
    p=svc.preview('prepare_after_sale',args)
    a=svc.submit('prepare_after_sale',p['arguments'],'op',p['business_version'])
    assert a==svc.submit('prepare_after_sale',p['arguments'],'op',p['business_version'])

def test_invoice_is_encrypted_application_only(svc,standard_provider):
    assert read(svc,'assess_order_action',order_ref=ref('order','shop','paid'),action='invoice')['data']['eligibility']=='eligible'
    result=commit(svc,'prepare_invoice',{'order_ref':ref('order','shop','paid'),'invoice_type':'company','title':'测试公司','tax_number':'TEST-TAX'})
    assert read(svc,'list_invoice_requests')['data']['total']==1
    assert read(svc,'get_invoice_request',invoice_id=result['invoice_id'])['data']['title']=='***'
    with svc.store.transaction() as tx:
        row=tx.one('SELECT * FROM invoice_requests')
        assert b'TEST-TAX' not in row['tax_number_ciphertext']
        assert svc.cipher.open(row['title_ciphertext'],'invoice-title:alice',row['key_version'])=='测试公司'
        assert row['status']=='pending' and row['issued_at'] is None

def test_policy_text_version_conflict_is_explicit(svc,monkeypatch):
    from app.tasks.replies import ReplyCoordinator
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='refund_only'",(dumps(dict(schema_version=1,allowed_statuses=['paid'],rag_policy_id='terms',rag_policy_version='2')),))
    def search(query,count,context):
        assert context.order_evidence_id.startswith('mysql:') and context.order_id=='paid'
        return {'success':True,'context':context.model_dump(mode='json'),'results':[{'source_id':'terms','version':'1'}]}
    monkeypatch.setattr('app.rag.service.search_policy',search)
    result=read(svc,'search_policies',query='退款规则',subject_ref=ref('order','shop','paid'))
    assert result['data']['rule_checks'][0]['text_version_matches'] is False
    assert any('不一致' in v for v in result['limitations'])
    assert not ReplyCoordinator._new_policy_current(None,result)

def test_cancel_requires_owned_inventory_and_releases_atomically(svc):
    args={'order_ref':ref('order','shop','unpaid'),'reason':'不需要'}
    with pytest.raises(BusinessError,match='预留'): svc.preview('prepare_order_cancel',args)
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE sku_inventory SET reserved_qty=3 WHERE sku_id='s1'")
        tx.execute('INSERT INTO inventory_reservations(reservation_id,platform_code,order_id,order_item_id,sku_id,quantity,status,request_id,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,3,%s,%s,%s,%s)',('r','shop','unpaid','i-unpaid','s1','reserved','reservation',utcnow(),utcnow()))
    assert commit(svc,'prepare_order_cancel',args)['status']=='cancelled'
    with svc.store.transaction() as tx:
        row=tx.one("SELECT * FROM sku_inventory WHERE sku_id='s1'")
        assert row['available_qty']==23 and row['reserved_qty']==0
        assert tx.one('SELECT status FROM inventory_reservations')['status']=='released'

def test_address_history_and_scope(svc):
    now=utcnow()
    encrypted={field:svc.cipher.seal(value,'address:alice:'+field) for field,value in [('recipient_name','收件人'),('phone','13800000000'),('detail','新地址') ]}
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO sku_fulfillment VALUES ('s1','warehouse',600,'test',%s)",(now,))
        tx.execute("INSERT INTO delivery_rules VALUES ('address-rule',1,'shop','m','warehouse','p','*','*','CNY',1000,0,500,0,1,3,'test',%s,NULL,'active')",(now-timedelta(days=1),))
        tx.execute('INSERT INTO user_addresses(user_id,recipient_name_ciphertext,phone_ciphertext,province_code,city_code,district_code,detail_ciphertext,key_version,is_default,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,1,1,%s,%s)',('alice',encrypted['recipient_name'],encrypted['phone'],'p','c','d',encrypted['detail'],now,now))
        aid=str(tx.one('SELECT address_id FROM user_addresses')['address_id'])
        tx.execute('INSERT INTO order_address_snapshots(platform_code,order_id,recipient_name_ciphertext,phone_ciphertext,province_code,city_code,district_code,detail_ciphertext,key_version,captured_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s)',('shop','unpaid',b'oldname',b'oldphone','p','c','d',b'old',now))
    args={'order_ref':ref('order','shop','unpaid'),'address_id':aid}
    prepared=svc.preview('prepare_order_address_change',args)
    assert '新地址' not in dumps(prepared)
    assert svc.confirmation_review(prepared)['address']['detail']=='新地址'
    assert commit(svc,'prepare_order_address_change',args)['status']=='updated'
    with svc.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM order_address_changes')['n']==1
        assert tx.one('SELECT detail_ciphertext FROM order_address_snapshots')['detail_ciphertext']==encrypted['detail']

def test_shipping_label_does_not_count_as_dispatch(svc):
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO shipments(shipment_id,platform_code,order_id,status,created_at) VALUES ('pack','shop','paid','created',%s)",(utcnow(),))
        tx.execute("INSERT INTO shipment_items(shipment_id,order_item_id,quantity) VALUES ('pack','i-paid',2)")
    out=read(svc,'get_shipments',order_ref=ref('order','shop','paid'))
    assert out['data']['quantities'][0]['recorded_shipped']==0
    assert read(svc,'get_shipment_tracking',shipment_id='pack')['data']['total']==0
    assert read(svc,'get_payments',order_ref=ref('order','shop','paid'))['data']['total']==0

def test_cursor_is_bound_to_user_filters_and_snapshot(svc):
    rows=[{'n':n} for n in range(25)]
    cursor=svc.page(rows,'test',{})['next_cursor']
    assert svc.page(rows,'test',{'cursor':cursor})['data']['items'][0]['n']==20
    with pytest.raises(BusinessError): svc.page(rows,'other',{'cursor':cursor})
    with pytest.raises(BusinessError): svc.page(rows+[{'n':26}],'test',{'cursor':cursor})
    svc.user='bob'
    with pytest.raises(BusinessError): svc.page(rows,'test',{'cursor':cursor})
