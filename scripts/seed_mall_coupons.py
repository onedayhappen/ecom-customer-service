"""Adapt five pinned mall coupon rules to current demo catalog and ten users.

Default previews. --apply installs required existing 003 business tables and
inserts one batch atomically. No historical orders/payments are modified.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import re
import sys
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pymysql
from dotenv import dotenv_values
from app.data.business_store import Transaction
from app.data.order_dao import parse_mysql_dsn
from app.services.commerce.common import dumps
from scripts.seed_mall_catalog import URL,SHA256,parse_rows
from scripts.rename_demo_users import NAMES

PREFIX='mall_demo_coupon_'
POLICY='mall_demo_coupon_pricing'
PLATFORM='mall_demo'


def load_source():
    sql=urlopen(URL,timeout=30).read().decode('utf-8-sig')
    if hashlib.sha256(sql.encode()).hexdigest()!=SHA256:
        raise ValueError('Upstream source changed; inspect before importing')
    return {t:parse_rows(sql,t) for t in ['sms_coupon','sms_coupon_product_relation',
        'sms_coupon_product_category_relation','pms_product','sms_coupon_history']}


def plan(tx,source,now):
    if tx.one('SELECT coupon_id FROM coupon_templates WHERE LEFT(coupon_id,%s)=%s LIMIT 1',(len(PREFIX),PREFIX)):
        raise ValueError('Coupon batch already exists; refusing duplicate import or time refresh')
    if {r['id'] for r in source['sms_coupon']}!={'27','28','29','30','31'}:
        raise ValueError('Unexpected coupon source coverage')
    if tx.one("SELECT policy_id FROM business_policy_versions WHERE action='pricing' AND platform_code=%s AND status='active'",(PLATFORM,)):
        raise ValueError('Existing pricing policy requires reconciliation')
    users=tx.all('SELECT user_id,username FROM users ORDER BY user_id')
    expected={f'user_{n}':name for n,name in NAMES.items()}
    users=[u for u in users if u['user_id'] in expected]
    if len(users)!=10 or any(u['username']!=expected[u['user_id']] for u in users):
        raise ValueError('Expected ten renamed demo users')
    products=tx.all('SELECT product_id,merchant_id FROM products WHERE product_id LIKE %s',('mall_demo_%',))
    mapped=defaultdict(list)
    for p in products:
        match=re.search(r'_p(\d+)$',p['product_id'])
        if match: mapped[match[1]].append(p['product_id'])
    today=now.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(hour=0,minute=0,second=0,microsecond=0)
    starts=today.astimezone(timezone.utc).replace(tzinfo=None)
    rows=defaultdict(list);summary=[]
    for coupon in source['sms_coupon']:
        cid=PREFIX+coupon['id'];days=3 if coupon['id']=='31' else 30
        ends=starts+timedelta(days=days)
        # Source platform is a client/channel flag. The local sales platform is mall_demo.
        template=dict(coupon_id=cid,merchant_id=None,platform_code=PLATFORM,name=coupon['name']+'（模拟）',
            kind='amount_off',currency='CNY',threshold_amount=Decimal(coupon['min_point']),discount_amount=Decimal(coupon['amount']),
            discount_bps=None,cap_amount=None,total_limit=int(coupon['publish_count']),per_user_limit=int(coupon['per_limit']),
            claim_starts_at=starts,claim_ends_at=ends,valid_days=None,fixed_expires_at=ends,status='active')
        if template['total_limit']<len(users) or template['per_user_limit']<1:
            raise ValueError('Coupon issuance limit insufficient')
        if coupon['use_type']=='0': scopes=[dict(scope_type='all',scope_id='*')]
        else:
            if coupon['use_type']=='1':
                categories={r['product_category_id'] for r in source['sms_coupon_product_category_relation'] if r['coupon_id']==coupon['id']}
                ids={p['id'] for p in source['pms_product'] if p['product_category_id'] in categories and p['delete_status']=='0' and p['publish_status']=='1'}
            elif coupon['use_type']=='2':
                ids={r['product_id'] for r in source['sms_coupon_product_relation'] if r['coupon_id']==coupon['id']}
            else: raise ValueError('Unsupported source coupon scope')
            if not ids or any(len(mapped[i])!=3 for i in ids): raise ValueError('Missing or ambiguous catalog mapping')
            scopes=[dict(scope_type='product',scope_id=p) for p in sorted({p for i in ids for p in mapped[i]})]
        rows['coupon_templates'].append(template)
        rows['coupon_scopes'] += [dict(coupon_id=cid,**s) for s in scopes]
        for user in users:
            uid='coupon_'+coupon['id']+'_'+user['user_id']
            rows['user_coupons'].append(dict(user_coupon_id=uid,coupon_id=cid,user_id=user['user_id'],status='available',
                claimed_at=now,expires_at=ends,used_at=None,version=0))
            rows['coupon_claim_rules'].append(dict(user_coupon_id=uid,rules_snapshot=dumps({'template':template,'scopes':scopes,
                'source':{'dataset':'macrozheng/mall','coupon_id':coupon['id'],'sha256':SHA256},
                'adaptation':{'is_demo':True,'dates':'current_local_campaign','wallet':'one_assigned_per_demo_user','historical_redemptions_imported':False}}),captured_at=now))
        summary.append(dict(coupon_id=cid,name=template['name'],threshold=template['threshold_amount'],discount=template['discount_amount'],
            scope_count=len(scopes),per_user_limit=template['per_user_limit'],issued=len(users),remaining=template['total_limit']-len(users),
            expires_local=ends.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat()))
    rows['business_policy_versions'].append(dict(policy_id=POLICY,version=1,action='pricing',platform_code=PLATFORM,channel=None,
        rules=dumps({'schema_version':1,'threshold_basis':'goods_after_promotions','coupon_stacking':False,'promotions_with_coupons':False}),
        effective_from=starts,effective_until=None,source='local_demo:mall_coupon_seed',status='active'))
    return rows,{'local_date':str(today.date()),'insert_counts':{k:len(v) for k,v in rows.items()},
        'source_history_not_imported':len(source['sms_coupon_history']),'coupons':summary}


def run(dsn,source,apply=False):
    conn=pymysql.connect(**parse_mysql_dsn(dsn),charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=False)
    try:
        with conn.cursor() as c:
            c.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if c.fetchone()['acquired']!=1: raise TimeoutError('Seed lock busy')
        tx=Transaction(conn,write=apply)
        rows,report=plan(tx,source,datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0))
        if not apply: return report
        # Only needed existing business definitions; no provenance tables or version marking.
        text=(ROOT/'migrations/mysql/003_core_tools.sql').read_text(encoding='utf-8-sig')
        with conn.cursor() as c:
            for table in ['coupon_claim_rules','business_operation_receipts','promotion_scopes']:
                match=re.search(r'CREATE TABLE IF NOT EXISTS '+table+r'\s*\(.*?;',text,re.S)
                if not match: raise ValueError('Missing dependency DDL')
                c.execute(match[0])
        conn.commit();conn.begin()
        tx.all('SELECT user_id FROM users ORDER BY user_id FOR UPDATE')
        tx.all('SELECT coupon_id FROM coupon_templates ORDER BY coupon_id FOR UPDATE')
        rows,report=plan(tx,source,datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0))
        with conn.cursor() as c:
            for table,records in rows.items():
                for row in records:
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
    print(dumps(run(cfg['ORDER_DB_DSN'],load_source(),args.apply)))
