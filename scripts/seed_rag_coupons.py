# 文件说明：初始化与政策说明对应的演示活动和优惠券，支持预览与幂等应用。
"""Seed RAG-aligned demo campaigns. Preview by default; --apply is repeatable.

Existing wallets keep their frozen rules. Claims use the production transaction
and stable request IDs, so a retry neither replenishes stock nor refreshes dates.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from app.data.business_store import BusinessStore
from app.services.commerce import CommerceService
from app.services.commerce.common import dumps, ref, utcnow, decoded, digest
from app.tasks.candidates import BusinessScope
from scripts.migrate_mysql_business_schema import _apply_009
from scripts.rename_demo_users import NAMES

PREFIX = 'rag_coupon_'
PLATFORM = 'mall_demo'
DIGITAL_CATEGORIES = ('手机通讯', '平板电脑', '笔记本', '硬盘')
CAMPAIGNS = (
    ('newcomer', '新人满50减10', 'newcomer', None, 50, 10, 7, 100),
    ('fashion', '优选服饰店满100减5', 'shop', 'mall_demo_fashion_a', 100, 5, 7, 100),
    ('digital_shop', '优选数码店满200减15', 'shop', 'mall_demo_digital_a', 200, 15, 30, 100),
    ('platform_200', '秋季大促平台满200减20（模拟）', 'platform', None, 200, 20, None, 200),
    ('platform_500', '秋季大促平台满500减60（模拟）', 'platform', None, 500, 60, None, 100),
    ('digital_category', '数码品类满1000减100', 'category', None, 1000, 100, 3, 100),
)
# Explicit demo claim examples, not automatic distribution on registration/login.
CLAIMS = [(f'user_{n}', CAMPAIGNS[1 + i % 5][0]) for i, n in enumerate(NAMES)]
CLAIMS.append(('user_1', 'digital_category'))


def report(store):
    with store.transaction() as tx:
        rows = tx.all('''SELECT t.coupon_id,t.name,t.coupon_type,t.total_limit,t.per_user_limit,
            t.valid_days,t.fixed_expires_at,t.claim_ends_at,t.status,COUNT(u.user_coupon_id) issued,
            t.total_limit-COUNT(u.user_coupon_id) remaining
            FROM coupon_templates t LEFT JOIN user_coupons u ON t.coupon_id=u.coupon_id
            WHERE LEFT(t.coupon_id,%s)=%s GROUP BY t.coupon_id ORDER BY t.coupon_id''', (len(PREFIX), PREFIX))
        return {'campaigns': rows, 'counts': {table: tx.one('SELECT COUNT(*) n FROM '+table)['n']
            for table in ('coupon_templates','coupon_scopes','user_coupons','coupon_claim_rules','coupon_redemptions')}}


def run(dsn, apply=False):
    store = BusinessStore(dsn)
    with store.transaction() as tx:
        users = {u['user_id']: u['username'] for u in tx.all('SELECT user_id,username FROM users')}
        assert all(users.get(f'user_{n}') == name for n, name in NAMES.items()), 'Demo users differ'
        products = tx.all('SELECT product_id FROM products WHERE category IN (%s,%s,%s,%s) ORDER BY product_id', DIGITAL_CATEGORIES)
        assert products, 'Digital catalog missing'
        for merchant in ('mall_demo_fashion_a', 'mall_demo_digital_a'):
            assert tx.one('SELECT merchant_id FROM merchants WHERE merchant_id=%s', (merchant,)), 'Merchant missing'
        existing = tx.all('SELECT coupon_id FROM coupon_templates WHERE LEFT(coupon_id,%s)=%s', (len(PREFIX), PREFIX))
        assert len(existing) in (0, len(CAMPAIGNS)), 'Partial/conflicting campaign batch requires inspection'
        if not apply:
            return {'apply': False, 'new_templates': 0 if existing else len(CAMPAIGNS),
                'digital_products': len(products), 'demo_claims': CLAIMS,
                'legacy_action': 'stop new claims; preserve existing wallets and frozen rules'}

    # Same advisory lock as schema migrations. DDL commits independently in MySQL.
    with store.transaction(write=True) as tx:
        assert tx.one("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")['acquired'] == 1
        try:
            _apply_009(tx.connection)
            tx.execute("INSERT IGNORE INTO schema_migrations(version,applied_at) VALUES ('009_coupon_eligibility',UTC_TIMESTAMP(6))")
        finally:
            tx.one("SELECT RELEASE_LOCK('ecom_mysql_business_schema') released")

    with store.transaction(write=True) as tx:
        # Hold the campaign lock until COMMIT, also preventing two seed processes.
        assert tx.one("SELECT GET_LOCK('ecom_rag_coupon_seed',30) acquired")['acquired'] == 1
        try:
            existing = tx.all('SELECT coupon_id FROM coupon_templates WHERE LEFT(coupon_id,%s)=%s FOR UPDATE', (len(PREFIX), PREFIX))
            if not existing:
                now = utcnow()
                old = tx.all("SELECT policy_id,version FROM business_policy_versions WHERE action='pricing' AND platform_code=%s AND status='active' FOR UPDATE", (PLATFORM,))
                assert [(p['policy_id'],p['version']) for p in old] == [('mall_demo_coupon_pricing',1)], 'Unexpected pricing policy'
                for suffix, name, kind, merchant, threshold, discount, days, limit in CAMPAIGNS:
                    end = now + timedelta(days=7 if kind=='platform' else 30)
                    tx.execute('''INSERT INTO coupon_templates(coupon_id,name,coupon_type,merchant_id,
                        platform_code,kind,currency,threshold_amount,discount_amount,total_limit,per_user_limit,
                        claim_starts_at,claim_ends_at,valid_days,fixed_expires_at,status)
                        VALUES (%s,%s,%s,%s,%s,'amount_off','CNY',%s,%s,%s,1,%s,%s,%s,%s,'active')''',
                        (PREFIX+suffix,name,kind,merchant,PLATFORM,threshold,discount,limit,now,end,days,None if days else end))
                    scopes = [('merchant',merchant)] if merchant else [('all','*')]
                    if kind == 'category': scopes = [('product',p['product_id']) for p in products]
                    for scope_type, scope_id in scopes:
                        tx.execute('INSERT INTO coupon_scopes(coupon_id,scope_type,scope_id) VALUES (%s,%s,%s)', (PREFIX+suffix,scope_type,scope_id))
                tx.execute("UPDATE coupon_templates SET status='inactive' WHERE LEFT(coupon_id,17)='mall_demo_coupon_'")
                tx.execute("UPDATE business_policy_versions SET status='inactive',effective_until=%s WHERE policy_id='mall_demo_coupon_pricing' AND version=1", (now,))
                tx.execute('''INSERT INTO business_policy_versions(policy_id,version,action,platform_code,rules,effective_from,source,status)
                    VALUES ('mall_demo_coupon_pricing',2,'pricing',%s,%s,%s,%s,'active')''',
                    (PLATFORM,dumps({'schema_version':1,'coupon_stacking':False,'threshold_basis':'goods_after_promotions',
                        'promotions_with_coupons':True}),now,'local_demo:rag_coupon_rules'))
            else:
                assert {r['coupon_id'] for r in existing} == {PREFIX+c[0] for c in CAMPAIGNS}
            tx.connection.commit()
        finally:
            tx.one("SELECT RELEASE_LOCK('ecom_rag_coupon_seed') released")

    for user, suffix in CLAIMS:
        service = CommerceService(store,user,scope=BusinessScope(platform_code=PLATFORM))
        service.claim_coupon({'coupon_ref':ref('coupon_template',PREFIX+suffix)},'rag-demo-claim:'+user+':'+suffix)
    return report(store)


def normalize_existing(dsn, apply=False):
    """Correct unredeemed demo coupons and snapshots after explicit migration approval."""
    store=BusinessStore(dsn)
    types={'mall_demo_coupon_27':'platform','mall_demo_coupon_28':'category',
        'mall_demo_coupon_29':'general','mall_demo_coupon_30':'general','mall_demo_coupon_31':'platform'}
    names={'mall_demo_coupon_29':'苹果指定商品满4000减600（模拟）',
        'mall_demo_coupon_30':'小米指定商品满2000减200（模拟）'}
    changed_templates=changed_wallets=0
    with store.transaction(write=apply) as tx:
        suffix=' FOR UPDATE' if apply else ''
        rows=tx.all("SELECT * FROM coupon_templates WHERE LEFT(coupon_id,17)='mall_demo_coupon_' OR LEFT(coupon_id,11)='rag_coupon_' ORDER BY coupon_id"+suffix)
        assert len(rows)==11, 'Unexpected legacy coupon set'
        for t in rows:
            cid=t['coupon_id']; fields=dict(coupon_type=types.get(cid,t['coupon_type']),per_user_limit=1,
                name=names.get(cid,t['name'] if '（模拟）' in t['name'] else t['name']+'（模拟）'))
            changed=any(t[k]!=v for k,v in fields.items())
            if changed:
                changed_templates+=1
                if apply:
                    tx.execute('UPDATE coupon_templates SET coupon_type=%s,per_user_limit=%s,name=%s WHERE coupon_id=%s',
                        (fields['coupon_type'],fields['per_user_limit'],fields['name'],cid))
            wallets=tx.all('''SELECT u.user_coupon_id,u.user_id,u.status,r.rules_snapshot
                FROM user_coupons u JOIN coupon_claim_rules r ON r.user_coupon_id=u.user_coupon_id
                WHERE u.coupon_id=%s ORDER BY u.user_coupon_id'''+suffix,(cid,))
            for w in wallets:
                saved=decoded(w['rules_snapshot']); old=saved['template']
                changes={k:{'before':old.get(k),'after':v} for k,v in fields.items() if old.get(k)!=v}
                if not changes: continue
                assert w['status'] not in ('reserved','used'), 'Do not rewrite transaction-bound coupons'
                assert not tx.one('SELECT redemption_id FROM coupon_redemptions WHERE user_coupon_id=%s LIMIT 1',(w['user_coupon_id'],)), 'Coupon has redemption history'
                changed_wallets+=1
                if apply:
                    saved['template'].update(fields)
                    saved['data_correction']={'batch':'coupon_design_alignment_20261025','source':'local_demo',
                        'corrected_at':utcnow(),'changes':changes,'amount_and_expiry_unchanged':True}
                    tx.execute('UPDATE coupon_claim_rules SET rules_snapshot=%s WHERE user_coupon_id=%s',(dumps(saved),w['user_coupon_id']))
                    tx.execute('UPDATE user_coupons SET version=version+1 WHERE user_coupon_id=%s',(w['user_coupon_id'],))
                    result={'user_coupon_id':w['user_coupon_id'],'changes':changes,'is_demo':True}
                    tx.execute('''INSERT INTO business_operation_receipts(operation_id,user_id,action,arguments_digest,result,created_at)
                        VALUES (%s,%s,'correct_demo_coupon',%s,%s,%s)''',
                        ('coupon-align:'+w['user_coupon_id'],w['user_id'],digest(result),dumps(result),utcnow()))
    return {'apply':apply,'templates_corrected':changed_templates,'wallet_snapshots_corrected':changed_wallets,
        'notice':'只修正分类、名称及普通券限领次数；不改变金额、适用商品、持券人或有效期。重复执行不增发。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--normalize-existing',action='store_true',help='Correct legacy demo templates and unredeemed wallet snapshots')
    args = parser.parse_args()
    dsn=dotenv_values(ROOT/'.env')['ORDER_DB_DSN']
    print(dumps(normalize_existing(dsn,args.apply) if args.normalize_existing else run(dsn,args.apply)))
