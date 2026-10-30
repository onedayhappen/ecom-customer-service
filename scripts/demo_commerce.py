# 文件说明：初始化隔离演示业务对象，并按显式参数演练确认提交和渠道推进。
"""Seed isolated demo objects and exercise real confirmed submissions and the worker.

Uses ORDER_DB_DSN after normal migrations. It never relabels existing orders.
The output contains demo identifiers only. Channels must explicitly be simulator.
"""
import argparse
from datetime import timedelta
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config.settings import settings
from app.data.business_store import BusinessStore
from app.services.commerce import service
from app.services.commerce.common import dumps, ref, utcnow
from app.services.commerce.providers import provider_mode
from scripts.commerce_worker import run_once


def seed(store, user):
    now=utcnow(); suffix=uuid4().hex[:12]; platform='demo'; merchant='demo-'+suffix
    with store.transaction(write=True) as tx:
        if not tx.one('SELECT user_id FROM users WHERE user_id=%s',(user,)):
            raise ValueError('请先在应用注册演示账号，再传入该账号 user_id')
        tx.execute('INSERT INTO merchants(merchant_id,name,category,description,status) VALUES (%s,%s,%s,%s,%s)',
                   (merchant,'本地演示商家','演示商品','仅本地演示，无真实交易','active'))
        tx.execute('INSERT INTO products(product_id,merchant_id,name,category,description,specs) VALUES (%s,%s,%s,%s,%s,%s)',
                   (merchant,merchant,'演示耳机','演示商品','用于本地售后与票据流程','{}'))
        tx.execute('INSERT INTO product_skus(sku_id,product_id,sku_code,spec_values,list_price,currency,status,created_at,updated_at) VALUES (%s,%s,%s,%s,10,%s,%s,%s,%s)',
                   (merchant,merchant,merchant,'{}','CNY','active',now,now))
        for kind in ('refund_only','return_refund','invoice'):
            if not tx.one("SELECT policy_id FROM business_policy_versions WHERE action=%s AND platform_code='demo' AND status='active'",(kind,)):
                rules=dict(schema_version=1,allowed_statuses=['paid'],local_authority=True)
                tx.execute('INSERT INTO business_policy_versions(policy_id,version,action,platform_code,rules,effective_from,source,status) VALUES (%s,1,%s,%s,%s,%s,%s,%s)',
                           ('demo-'+kind,kind,platform,dumps(rules),now-timedelta(days=1),'local_demo','active'))
        orders={}
        for kind in ('refund_only','return_refund','invoice'):
            oid='demo-'+kind+'-'+suffix; item='item-'+oid
            tx.execute('''INSERT INTO orders(platform_code,order_id,user_id,status,created_at,merchant_id,currency,goods_amount,discount_amount,shipping_amount,payable_amount,paid_amount,paid_at,updated_at,is_demo)
                VALUES (%s,%s,%s,'paid',%s,%s,'CNY',30,0,0,30,30,%s,%s,TRUE)''',(platform,oid,user,now,merchant,now,now))
            tx.execute('''INSERT INTO order_items(order_item_id,platform_code,order_id,sku_id,product_id,title_snapshot,spec_snapshot,quantity,unit_price,discount_amount,payable_amount)
                VALUES (%s,%s,%s,%s,%s,%s,'{}',3,10,0,30)''',(item,platform,oid,merchant,merchant,'演示耳机'))
            tx.execute('''INSERT INTO payment_transactions(payment_id,platform_code,order_id,user_id,provider,provider_txn_id,amount,currency,status,created_at)
                VALUES (%s,%s,%s,%s,'simulated_provider',%s,30,'CNY','succeeded',%s)''',('pay-'+oid,platform,oid,user,'demo-pay-'+oid,now))
            orders[kind]=dict(order_id=oid,order_ref=ref('order',platform,oid),order_item_id=item)
    return orders


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user',required=True,help='已注册的演示账号 user_id')
    parser.add_argument('--submit',action='store_true',help='显式批准脚本演示申请；不替代聊天中的用户确认')
    args=parser.parse_args()
    if provider_mode()!='simulator': parser.error('需要 COMMERCE_PROVIDER_MODE=simulator')
    store=BusinessStore(settings.order_db_dsn); orders=seed(store,args.user)
    svc=service(args.user); results={}
    if args.submit:
        for kind,order in orders.items():
            action='prepare_invoice' if kind=='invoice' else 'prepare_after_sale'
            fields=(dict(order_ref=order['order_ref'],invoice_type='personal',title='本地演示用户') if kind=='invoice'
                    else dict(kind=kind,items=[dict(order_item_id=order['order_item_id'],quantity=1)],reason='本地演示申请'))
            prepared=svc.preview(action,fields)
            results[kind]=svc.submit(action,prepared['arguments'],'demo-op-'+uuid4().hex,prepared['business_version'])
        run_once(store)
    print(dumps(dict(is_demo=True,user_id=args.user,orders=orders,submitted=results)))


if __name__=='__main__': main()
