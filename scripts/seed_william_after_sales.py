"""Apply migration 008 and seed two recent William multi-product demo orders once.

No provider calls, inventory deductions, password changes, or historical deletions.
Default previews the plan; --apply installs the scoped schema and commits data.
"""
from __future__ import annotations
import argparse
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pymysql
from dotenv import dotenv_values
from app.data.business_store import Transaction
from app.data.order_dao import parse_mysql_dsn
from app.services.commerce.common import Cipher, dumps, decoded
from scripts.migrate_mysql_business_schema import _apply_008

PREFIX='DEMO-WILLIAM-MULTI-'
VERSION='008_after_sale_fulfillment'


def plan(tx,cipher,now):
    if tx.one('SELECT order_id FROM orders WHERE LEFT(order_id,%s)=%s LIMIT 1',(len(PREFIX),PREFIX)):
        raise ValueError('William multi-product scenarios already exist')
    user=tx.one("SELECT user_id,username FROM users WHERE user_id='user_1'")
    if not user or user['username']!='William': raise ValueError('Expected William / user_1')
    address=tx.one("SELECT * FROM user_addresses WHERE user_id='user_1' AND deleted_at IS NULL ORDER BY is_default DESC,address_id LIMIT 1")
    if not address: raise ValueError('Missing William address')
    catalog=tx.all("""SELECT p.product_id,p.merchant_id,p.name,p.category,s.sku_id,s.spec_values,s.list_price,s.currency
        FROM products p JOIN product_skus s ON s.product_id=p.product_id
        JOIN merchants m ON m.merchant_id=p.merchant_id JOIN sku_inventory i ON i.sku_id=s.sku_id
        WHERE p.merchant_id='mall_demo_fashion_a' AND m.status='active' AND s.status='active' AND i.available_qty>=2""")
    skus={r['sku_id'].rsplit('_s',1)[-1]:r for r in catalog}
    required=['179','185','189','171','173','163','165']
    if any(k not in skus for k in required): raise ValueError('Expected live fashion sample SKUs')
    rows=defaultdict(list)
    def add(table,**row): rows[table].append(row)
    local=now.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    midnight=local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)

    def parcel(oid,iid,sku,quantity,sent,delivered=None,aid=None,confirmed=None):
        sid=('ex_' if aid else 'ship_')+iid
        add('shipments',shipment_id=sid,platform_code='mall_demo',order_id=oid,
            tracking_number='TEST-'+sid,carrier='模拟物流',status='delivered' if delivered else 'in_transit',
            shipped_at=sent,delivered_at=delivered,created_at=sent,shipment_kind='exchange' if aid else 'original',
            after_sale_id=aid,receipt_confirmed_at=confirmed)
        add('shipment_items',shipment_id=sid,order_item_id=iid,quantity=quantity,actual_sku_id=sku['sku_id'],actual_spec_snapshot=sku['spec_values'])
        for stage,when in [('shipped',sent),('delivered',delivered)]:
            if when:
                add('shipment_events',event_id=sid+'_'+stage,shipment_id=sid,event_time=when,status=stage,
                    description='本地模拟：'+stage,source_event_id='local_demo_'+sid+'_'+stage,recorded_at=now)
        return sid

    def sale(oid,iid,sku,quantity,kind,requested,status,stage,amount,completed=None):
        aid='sale_'+iid
        add('after_sale_requests',after_sale_id=aid,platform_code='mall_demo',order_id=oid,user_id='user_1',kind=kind,
            status=status,stage=stage,reason='本地模拟售后场景',requested_amount=amount,created_at=requested,
            updated_at=completed or now,completed_at=completed,closed_at=None)
        add('after_sale_items',after_sale_id=aid,order_item_id=iid,quantity=quantity,requested_amount=amount)
        add('after_sale_events',event_id=aid+'_submit',after_sale_id=aid,operation_id=aid+'_submit',kind='submitted',source='local_demo',payload=dumps({'is_demo':True}),created_at=requested)
        add('after_sale_events',event_id=aid+'_state',after_sale_id=aid,operation_id=aid+'_state',kind=status,source='local_demo',payload=dumps({'stage':stage,'is_demo':True}),created_at=completed or now)
        return aid

    def returned(aid,iid,quantity,requested,split=False):
        auth='auth_'+iid
        add('return_authorizations',authorization_id=auth,after_sale_id=aid,
            address_ciphertext=cipher.seal(dumps({'recipient_name':'模拟退货仓','phone':'00000000000','detail':'本地测试地址，不可真实配送'}),'return-address:'+auth),
            key_version=cipher.version,carriers=dumps([{'code':'DEMO','name':'模拟物流'}]),requirements=dumps(['仅用于本地测试']),
            deadline=now+timedelta(days=7),status='active',source='local_demo',updated_at=requested)
        for index,qty in enumerate([1,1] if split else [quantity]):
            rid='ret_'+iid+'_'+str(index)
            sent=requested+timedelta(hours=12+index*12)
            accepted=None if split and index==1 else sent+timedelta(hours=12)
            add('return_shipments',return_shipment_id=rid,authorization_id=auth,carrier_code='DEMO',tracking_number='TEST-'+rid,
                status='in_transit' if accepted is None else 'accepted',operation_id=rid,created_at=sent,updated_at=accepted or sent+timedelta(hours=1))
            add('return_shipment_items',return_shipment_id=rid,after_sale_id=aid,order_item_id=iid,quantity=qty)
            for stage,when in [('collected',sent),('accepted' if accepted else 'in_transit',accepted or sent+timedelta(hours=1))]:
                add('return_shipment_events',event_id=rid+'_'+stage,return_shipment_id=rid,status=stage,
                    description='本地模拟：'+stage,source='local_demo',event_time=when)

    for number,days in [(1,6),(2,7)]:
        oid=PREFIX+str(number);created=midnight-timedelta(days=days)+timedelta(hours=10)
        selected=['179','185','189','171'] if number==1 else ['179','185','171','163']
        total=sum(skus[k]['list_price']*(2 if number==1 and j==3 else 1) for j,k in enumerate(selected))
        add('orders',platform_code='mall_demo',order_id=oid,user_id='user_1',status='after_sale',created_at=created,
            merchant_id='mall_demo_fashion_a',currency='CNY',goods_amount=total,discount_amount=Decimal(0),shipping_amount=Decimal(0),
            payable_amount=total,paid_amount=total,paid_at=created+timedelta(minutes=5),updated_at=now,
            status_before_after_sale='pending_receipt' if number==1 else 'received')
        add('payment_transactions',payment_id='pay_'+oid,platform_code='mall_demo',order_id=oid,user_id='user_1',provider='local_demo',
            provider_txn_id=None,amount=total,currency='CNY',status='succeeded',paid_at=created+timedelta(minutes=5),created_at=created+timedelta(minutes=5))
        sealed={f+'_ciphertext':cipher.seal(cipher.open(address[f+'_ciphertext'],'address:user_1:'+f,address['key_version']),'address:user_1:'+f) for f in ['recipient_name','phone','detail']}
        add('order_address_snapshots',platform_code='mall_demo',order_id=oid,**sealed,
            **{f:address[f] for f in ['province_code','city_code','district_code']},key_version=cipher.version,captured_at=now)
        add('order_events',event_id='event_'+oid,platform_code='mall_demo',order_id=oid,operation_id='event_'+oid,kind='demo_seeded',
            payload=dumps({'is_demo':True,'case':'multi_product_after_sale','inventory_basis':'no_live_stock_deduction','local_date':str(local.date())}),created_at=now)
        for j,key in enumerate(selected):
            sku=skus[key];iid=f'WM{number}-I{j+1}';qty=2 if number==1 and j==3 else 1
            add('order_items',order_item_id=iid,platform_code='mall_demo',order_id=oid,sku_id=sku['sku_id'],product_id=sku['product_id'],
                title_snapshot=sku['name']+'（'+sku['category']+'）',spec_snapshot=sku['spec_values'],quantity=qty,
                unit_price=sku['list_price'],discount_amount=Decimal(0),payable_amount=sku['list_price']*qty)
            if number==1 and j==0: continue
            parcel(oid,iid,sku,qty,created+timedelta(days=1),None if number==1 and j==1 else created+timedelta(days=2))
            if number==1 and j<3: continue
            requested=created+timedelta(days=3)
            if number==1:
                aid=sale(oid,iid,sku,qty,'return_refund',requested,'processing','return_in_transit',sku['list_price']*qty)
                returned(aid,iid,qty,requested,split=True)
            elif j<2:
                amount=Decimal('20.00') if j==0 else sku['list_price']
                completed=created+timedelta(days=5)
                aid=sale(oid,iid,sku,qty,'refund_only' if j==0 else 'return_refund',requested,'completed','completed',amount,completed)
                if j==1: returned(aid,iid,qty,requested)
                add('refund_transactions',refund_id='refund_'+iid,after_sale_id=aid,payment_id='pay_'+oid,provider='local_demo',provider_refund_id=None,
                    amount=amount,currency='CNY',status='succeeded',requested_at=completed-timedelta(hours=1),completed_at=completed)
            else:
                replacement=skus['173' if j==2 else '165'];completed=created+timedelta(days=6) if j==3 else None
                aid=sale(oid,iid,sku,qty,'exchange',requested,'completed' if completed else 'processing','completed' if completed else 'replacement_in_transit',Decimal(0),completed)
                returned(aid,iid,qty,requested)
                sid=parcel(oid,iid,replacement,qty,created+timedelta(days=5),completed-timedelta(hours=1) if completed else None,aid,completed)
                add('service_work_orders',work_order_id='work_'+iid,after_sale_id=aid,kind='exchange',status='completed' if completed else 'processing',
                    details=dumps({'order_item_id':iid,'quantity':qty,'replacement_sku_id':replacement['sku_id'],'replacement_shipment_id':sid,'is_demo':True}),
                    provider_reference=None,source='local_demo',updated_at=completed or now)
    for records in rows.values():
        for record in records:
            for key,value in record.items():
                if isinstance(value,datetime) and key!='deadline' and value>now: raise ValueError('Future scenario time')
    return rows,{'local_date':str(local.date()),'order_ids':[PREFIX+'1',PREFIX+'2'],'insert_counts':{k:len(v) for k,v in rows.items()}}


def run(dsn,cipher,apply=False):
    conn=pymysql.connect(**parse_mysql_dsn(dsn),charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=False)
    try:
        with conn.cursor() as c:
            c.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if c.fetchone()['acquired']!=1: raise TimeoutError('Schema lock busy')
        tx=Transaction(conn,write=apply)
        rows,report=plan(tx,cipher,datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0))
        if not apply: return report
        _apply_008(conn)
        with conn.cursor() as c:
            c.execute('INSERT IGNORE INTO schema_migrations(version,applied_at) VALUES (%s,UTC_TIMESTAMP(6))',(VERSION,))
        conn.commit();conn.begin()
        tx.all("SELECT user_id FROM users WHERE user_id='user_1' FOR UPDATE")
        rows,report=plan(tx,cipher,datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0))
        # Grouped insertion order follows foreign-key dependencies.
        tables=['orders','order_items','payment_transactions','order_address_snapshots','order_events','after_sale_requests','after_sale_items',
                'after_sale_events','return_authorizations','return_shipments','return_shipment_items','return_shipment_events',
                'refund_transactions','shipments','shipment_items','shipment_events','service_work_orders']
        with conn.cursor() as c:
            for table in tables:
                for row in rows.get(table,[]):
                    c.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()))
        conn.commit();return report
    except BaseException:
        conn.rollback();raise
    finally:
        with conn.cursor() as c: c.execute("SELECT RELEASE_LOCK('ecom_mysql_business_schema')")
        conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    cfg=dotenv_values(ROOT/'.env')
    print(dumps(run(cfg['ORDER_DB_DSN'],Cipher(cfg['BUSINESS_DATA_KEYS'],int(cfg['BUSINESS_DATA_KEY_VERSION'])),args.apply)))
