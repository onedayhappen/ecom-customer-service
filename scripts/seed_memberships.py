"""Install membership-only migration and seed ten explicit demo identities.

Preview by default. --apply is atomic for data and refuses partially populated
or conflicting demo membership IDs. Existing orders, coupons and passwords stay intact.
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import dotenv_values
from app.data.business_store import BusinessStore
from app.data.membership_store import refresh_user_summary, membership_facts
from app.services.commerce.common import dumps, utcnow, decoded, ref
from scripts.migrate_mysql_business_schema import _apply_010
from scripts.rename_demo_users import NAMES

PLATFORM='mall_demo'
GROWTH='mall_growth'
VIP='mall_vip'
LEVELS=[('bronze','青铜',0,10,1),('silver','白银',1000,15,1),('gold','黄金',3000,20,2),
    ('platinum','铂金',8000,25,2),('diamond','钻石',20000,30,3),('black_diamond','黑钻',50000,40,3)]
ASSIGNMENTS={1:('platinum','annual'),3:('gold','monthly'),4:('bronze',None),5:('diamond','annual'),
    6:('silver',None),7:('gold','annual'),8:('platinum','monthly'),9:('silver','expired_monthly'),
    10:('black_diamond','annual'),11:('bronze',None)}
STORES=[(1,'fashion','advanced'),(1,'digital','regular'),(3,'digital','advanced'),
    (7,'fashion','regular'),(8,'digital','regular'),(11,'fashion','regular')]


def insert(tx,table,row):
    tx.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()))


def add_membership(tx,user,program,level,starts,ends,now,source='demo_assignment',growth=0):
    mid='demo_'+user+'_'+program
    insert(tx,'memberships',dict(membership_id=mid,user_id=user,platform_code=PLATFORM,program_id=program,
        level_code=level,level_version=1,starts_at=starts,expires_at=ends,status='active',updated_at=now,
        growth_value=growth,evaluated_at=starts if program==GROWTH else None,
        next_evaluate_at=ends if program==GROWTH else None,assignment_source=source))
    insert(tx,'membership_events',dict(event_id=mid,event_type='demo_grant',membership_id=mid,
        to_level=level,effective_at=starts,ends_at=ends,reason='本地演示授予，不表示实际消费升级或缴费',
        rule_version=1,request_id='seed:'+mid))
    return mid


def summary(store):
    result=[]
    with store.transaction() as tx:
        for n in ASSIGNMENTS:
            f=membership_facts(tx,'user_'+str(n),PLATFORM)
            sub=f['vip']['subscription']; g=f['growth_membership']
            result.append({'user_id':'user_'+str(n),'name':NAMES[n],'level':g['name'],
                'vip_status':f['vip']['status'],'plan':sub['plan_code'] if sub else None,
                'ends_at':sub['ends_at'] if sub else None,'store_memberships':len(f['store_memberships'])})
        counts={t:tx.one('SELECT COUNT(*) n FROM '+t)['n'] for t in
            ('membership_programs','membership_levels','memberships','membership_subscriptions',
             'membership_events','membership_growth_ledger','membership_benefits','benefit_entitlements')}
    return {'members':result,'counts':counts}


def run(dsn,apply=False):
    store=BusinessStore(dsn)
    starts=datetime(2026,10,25)-timedelta(hours=8)
    quarter_end=datetime(2027,1,1)-timedelta(hours=8)
    annual_end=datetime(2027,10,25)-timedelta(hours=8)
    month_end=datetime(2026,11,25)-timedelta(hours=8)
    with store.transaction() as tx:
        users={r['user_id']:r['username'] for r in tx.all('SELECT user_id,username FROM users')}
        assert all(users.get('user_'+str(n))==name for n,name in NAMES.items()), 'Demo user mapping changed'
        for merchant in ('mall_demo_fashion_a','mall_demo_digital_a'):
            assert tx.one('SELECT merchant_id FROM merchants WHERE merchant_id=%s',(merchant,))
    if not apply:
        return {'apply':False,'start_local':'2026-10-25 00:00:00+08:00',
            'assignments':{NAMES[n]:{'level':level,'plan':plan} for n,(level,plan) in ASSIGNMENTS.items()},
            'notice':'模拟授予，未收费，会员券配置为planned，尚未发放'}
    with store.transaction(write=True) as tx:
        assert tx.one("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")['acquired']==1
        try:
            _apply_010(tx.connection)
            tx.execute("INSERT IGNORE INTO schema_migrations(version,applied_at) VALUES ('010_membership_programs',UTC_TIMESTAMP(6))")
            tx.connection.commit()
        finally: tx.one("SELECT RELEASE_LOCK('ecom_mysql_business_schema') released")
    with store.transaction(write=True) as tx:
        assert tx.one("SELECT GET_LOCK('ecom_demo_memberships',30) acquired")['acquired']==1
        try:
            existing=tx.all("SELECT membership_id FROM memberships WHERE assignment_source='demo_assignment' FOR UPDATE")
            expected={'demo_user_'+str(n)+'_'+GROWTH for n in ASSIGNMENTS}
            expected|={'demo_user_'+str(n)+'_'+VIP for n,(_,plan) in ASSIGNMENTS.items() if plan}
            expected|={'demo_user_'+str(n)+'_mall_store_'+shop for n,shop,_ in STORES}
            if existing:
                assert {r['membership_id'] for r in existing}==expected, 'Partial demo membership batch requires inspection'
            else:
                now=utcnow()
                assert not tx.one('SELECT program_id FROM membership_programs WHERE program_id=%s',(GROWTH,)), 'Program already configured'
                assert not tx.one("SELECT program_id FROM membership_programs WHERE registration_default=TRUE"), 'Default program already configured'
                for pid,kind,name,merchant in [(GROWTH,'growth','平台成长会员',None),(VIP,'paid','平台VIP',None),
                    ('mall_store_fashion','store','优选服饰店会员','mall_demo_fashion_a'),
                    ('mall_store_digital','store','优选数码店会员','mall_demo_digital_a')]:
                    insert(tx,'membership_programs',dict(program_id=pid,platform_code=PLATFORM,merchant_id=merchant,
                        program_type=kind,name=name,status='active',registration_default=pid==GROWTH,rule_version=1,
                        evaluation_rules=dumps({'schema_version':1,'evaluation':'quarterly','lookback_days':365,
                            'timezone':'Asia/Shanghai','automatic_evaluation_enabled':False} if kind=='growth' else {'schema_version':1})))
                for rank,(code,name,threshold,discount,quota) in enumerate(LEVELS):
                    insert(tx,'membership_levels',dict(program_id=GROWTH,platform_code=PLATFORM,level_code=code,version=1,
                        name=name,rank_value=rank,growth_threshold=threshold,benefits=dumps({'coupon_benefits_status':'planned'}),source='local_demo:membership_v1'))
                    insert(tx,'membership_benefits',dict(benefit_id='growth_'+code,version=1,program_id=GROWTH,level_code=code,
                        level_version=1,benefit_family='monthly_growth_coupon',period_type='month',quota=quota,
                        membership_check='claim_only',rules=dumps({'threshold_amount':200,'discount_amount':discount,'currency':'CNY',
                            'coupon_template_id':None}),status='planned',effective_from=starts))
                for pid,levels in [(VIP,[('vip','VIP会员')]),('mall_store_fashion',[('regular','普通店铺会员'),('advanced','高级店铺会员')]),
                    ('mall_store_digital',[('regular','普通店铺会员'),('advanced','高级店铺会员')])]:
                    for rank,(code,name) in enumerate(levels):
                        insert(tx,'membership_levels',dict(program_id=pid,platform_code=PLATFORM,level_code=code,version=1,
                            name=name,rank_value=rank,growth_threshold=0,benefits='{}',source='local_demo:membership_v1'))
                for amount,discount,quota in [(200,50,2),(500,100,1)]:
                    insert(tx,'membership_benefits',dict(benefit_id='vip_'+str(amount),version=1,program_id=VIP,
                        level_code='vip',level_version=1,benefit_family='monthly_vip_'+str(amount),period_type='month',quota=quota,
                        membership_check='claim_and_use',rules=dumps({'threshold_amount':amount,'discount_amount':discount,
                            'currency':'CNY','coupon_template_id':None}),status='planned',effective_from=starts))
                for n,(level,plan) in ASSIGNMENTS.items():
                    uid='user_'+str(n); value=next(l[2] for l in LEVELS if l[0]==level)
                    add_membership(tx,uid,GROWTH,level,starts,quarter_end,now,growth=value)
                    if value:
                        insert(tx,'membership_growth_ledger',dict(entry_id='demo_growth_'+uid,user_id=uid,program_id=GROWTH,
                            delta=value,event_type='demo_adjustment',event_key='demo_growth:'+uid,effective_at=starts,
                            expires_at=starts+timedelta(days=365)))
                    if plan:
                        sub_start=starts; sub_end=annual_end if plan=='annual' else month_end
                        if plan=='expired_monthly':
                            sub_start=datetime(2026,8,25)-timedelta(hours=8)
                            sub_end=datetime(2026,9,25)-timedelta(hours=8)
                        mid=add_membership(tx,uid,VIP,'vip',sub_start,sub_end,now)
                        insert(tx,'membership_subscriptions',dict(subscription_id='demo_sub_'+uid,membership_id=mid,
                            plan_code='monthly' if plan=='expired_monthly' else plan,starts_at=sub_start,ends_at=sub_end,
                            status='active',activation_mode='demo_grant',payment_ref=None,
                            plan_snapshot=dumps({'schema_version':1,'is_demo':True,'charged':False,'auto_renew':False}),
                            request_id='demo_subscription:'+uid,created_at=now))
                for n,shop,level in STORES:
                    add_membership(tx,'user_'+str(n),'mall_store_'+shop,level,starts,annual_end,now)
                for n in ASSIGNMENTS: refresh_user_summary(tx,'user_'+str(n),PLATFORM,now)
            tx.connection.commit()
        finally: tx.one("SELECT RELEASE_LOCK('ecom_demo_memberships') released")
    return summary(store)


def enable_coupon_benefits(dsn, apply=False, claim_examples=False):
    """Enable the explicitly prepared October coupon batch, without renewing it."""
    from app.services.commerce import CommerceService
    from app.tasks.candidates import BusinessScope
    store=BusinessStore(dsn); now=utcnow()
    with store.transaction(write=apply) as tx:
        definitions=tx.all('SELECT * FROM membership_benefits ORDER BY benefit_id')
        assert len(definitions)==8, 'Unexpected benefit set'
        for b in definitions:
            rule=decoded(b['rules']); cid=rule.get('coupon_template_id')
            assert cid and cid.startswith('demo26_coupon_member_'), 'Prepare and review the coupon batch first'
            t=tx.one('SELECT * FROM coupon_templates WHERE coupon_id=%s'+(' FOR UPDATE' if apply else ''),(cid,))
            assert t and t['claim_starts_at']<=now<t['claim_ends_at'], 'Do not silently renew an expired batch'
            assert float(t['threshold_amount'])==rule['threshold_amount'] and float(t['discount_amount'])==rule['discount_amount']
            if not apply: continue
            label=tx.one('SELECT name FROM membership_levels WHERE program_id=%s AND level_code=%s AND version=%s',
                (b['program_id'],b['level_code'],b['level_version']))['name']
            title=f"{label}月度满{rule['threshold_amount']}减{rule['discount_amount']}（模拟）"
            tx.execute("UPDATE coupon_templates SET status='active',name=%s WHERE coupon_id=%s",(title,cid))
            rule.update(issuance_mode='self_claim',automatic_claim_enabled=True)
            tx.execute("UPDATE membership_benefits SET status='active',rules=%s WHERE benefit_id=%s AND version=%s",(dumps(rule),b['benefit_id'],b['version']))
            tx.execute("UPDATE membership_levels SET benefits=JSON_SET(benefits,'$.coupon_benefits_status','active') WHERE program_id=%s AND level_code=%s AND version=%s",
                (b['program_id'],b['level_code'],b['level_version']))
            tx.execute("UPDATE benefit_entitlements SET status='active',evidence_snapshot=JSON_SET(evidence_snapshot,'$.automatic_claim_enabled',CAST('true' AS JSON)) WHERE benefit_id=%s AND benefit_version=%s AND status='planned' AND starts_at<=%s AND expires_at>%s",
                (b['benefit_id'],b['version'],now,now))
    if apply and claim_examples:
        for n,(level,plan) in ASSIGNMENTS.items():
            user='user_'+str(n)
            benefits=['growth_'+level]
            if plan in ('monthly','annual'): benefits.append('vip_200')
            if n==1: benefits.append('vip_500')
            svc=CommerceService(store,user,scope=BusinessScope(platform_code=PLATFORM))
            for benefit in benefits:
                svc.claim_coupon({'coupon_ref':ref('coupon_template','demo26_coupon_member_'+benefit)},
                    'demo26:member-claim:'+user+':'+benefit)
    return {'apply':apply,'enabled_benefits':8 if apply else 0,'claim_examples':claim_examples,
        'notice':'固定月份模板；不自动续期。样例领取走真实资格、额度及幂等事务。'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--enable-coupons',action='store_true',help='Enable the prepared member coupon batch')
    p.add_argument('--claim-examples',action='store_true',help='Claim 17 explicit demo examples; requires --enable-coupons')
    args=p.parse_args()
    if args.claim_examples and not args.enable_coupons: p.error('--claim-examples requires --enable-coupons')
    dsn=dotenv_values(ROOT/'.env')['ORDER_DB_DSN']
    print(dumps(enable_coupon_benefits(dsn,args.apply,args.claim_examples) if args.enable_coupons else run(dsn,args.apply)))
