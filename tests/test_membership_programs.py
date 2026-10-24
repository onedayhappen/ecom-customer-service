"""Membership identities and demo migration in disposable databases."""
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest

from tests.test_core_tools import database, svc
from app.data.auth_store import MySqlAuthStore
from app.data.membership_store import membership_facts
from app.services.commerce import CommerceService
from app.tasks.candidates import BusinessScope
from scripts.seed_memberships import run, NAMES
from app.services.commerce.common import BusinessError,ref,utcnow,decoded,dumps


@pytest.fixture
def seeded(svc):
    with svc.store.transaction(write=True) as tx:
        for n,name in NAMES.items():
            tx.execute("INSERT INTO users(user_id,username,username_key,status,created_at,updated_at) VALUES (%s,%s,%s,'active',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",('user_'+str(n),name,name.lower()))
        for mid in ('mall_demo_fashion_a','mall_demo_digital_a'):
            tx.execute("INSERT INTO merchants(merchant_id,name,category,description,status) VALUES (%s,%s,'测试','测试','active')",(mid,mid))
    run(svc.store.dsn,apply=True)
    return CommerceService(svc.store,'user_1',scope=BusinessScope(platform_code='mall_demo'))


def test_seed_idempotence_registration_and_real_query(seeded):
    before=run(seeded.store.dsn,apply=True)
    after=run(seeded.store.dsn,apply=True)
    assert before==after
    assert after['counts']['memberships']==23
    assert after['counts']['membership_subscriptions']==7
    assert after['counts']['benefit_entitlements']==0
    out=seeded.read('get_membership',{})['data']
    assert out['growth_membership']['name']=='铂金'
    assert out['vip']['subscription']['plan_code']=='annual'
    assert len(out['store_memberships'])==2
    assert all(b['status']=='planned' for b in out['benefit_definitions'])
    uid=MySqlAuthStore(seeded.store.dsn).create_user('new-member','hash-not-used-in-this-test')
    with seeded.store.transaction() as tx:
        f=membership_facts(tx,uid,'mall_demo')
        assert f['membership']['level_code']=='bronze'
        assert not f['vip']['is_vip']
        assert tx.one('SELECT growth_level_code FROM users WHERE user_id=%s',(uid,))['growth_level_code']=='bronze'
        assert tx.one('SELECT COUNT(*) n FROM membership_subscriptions')['n']==7


def test_time_bounds_suspension_and_platform_ownership(seeded):
    with seeded.store.transaction() as tx:
        before=membership_facts(tx,'user_1','mall_demo',datetime(2026,10,24,15,59))
        assert not before['vip']['is_vip']
        assert before['vip']['status']=='scheduled'
        last=membership_facts(tx,'user_1','mall_demo',datetime(2027,10,24,15,59,59))
        assert last['vip']['is_vip']
        ended=membership_facts(tx,'user_1','mall_demo',datetime(2027,10,24,16))
        assert not ended['vip']['is_vip'] and ended['vip']['status']=='expired'
        sarah=membership_facts(tx,'user_9','mall_demo',datetime(2026,10,25))
        assert not sarah['vip']['is_vip'] and sarah['vip']['status']=='expired'
        other=membership_facts(tx,'user_1','shop',datetime(2026,10,25))
        assert other['membership'] is None and not other['vip']['records']
    with seeded.store.transaction(write=True) as tx:
        tx.execute("UPDATE memberships SET status='suspended' WHERE user_id='user_1' AND program_id='mall_vip'")
    out=seeded.read('get_membership',{})['data']
    assert not out['vip']['is_vip']
    assert seeded.read('get_my_account',{})['data']['membership_summary']['vip_plan_code'] is None


def test_overlapping_subscription_is_unknown_not_extra_benefits(seeded):
    with seeded.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO membership_subscriptions(subscription_id,membership_id,plan_code,starts_at,ends_at,status,activation_mode,plan_snapshot,request_id,created_at) SELECT 'conflict',membership_id,plan_code,starts_at,ends_at,status,activation_mode,plan_snapshot,'conflict',created_at FROM membership_subscriptions WHERE subscription_id='demo_sub_user_1'")
    out=seeded.read('get_membership',{})['data']
    assert out['vip']['status']=='unknown' and out['vip']['is_vip'] is None
    assert out['vip']['subscription'] is None
    assert not any(b['membership_active'] for b in out['benefit_definitions'] if b['program_id']=='mall_vip')


@pytest.fixture
def member_coupons(seeded):
    now=utcnow()
    with seeded.store.transaction(write=True) as tx:
        for b in tx.all('SELECT * FROM membership_benefits'):
            rule=decoded(b['rules']); cid='member_'+b['benefit_id']
            tx.execute('''INSERT INTO coupon_templates(coupon_id,platform_code,name,kind,currency,
                threshold_amount,discount_amount,total_limit,per_user_limit,claim_starts_at,claim_ends_at,
                valid_days,status,coupon_type) VALUES (%s,'mall_demo',%s,'amount_off','CNY',%s,%s,100,%s,%s,%s,7,'active','platform')''',
                (cid,cid,rule['threshold_amount'],rule['discount_amount'],b['quota'],now-timedelta(minutes=1),now+timedelta(days=30)))
            tx.execute("INSERT INTO coupon_scopes VALUES (%s,'all','*')",(cid,))
            rule['coupon_template_id']=cid
            tx.execute("UPDATE membership_benefits SET status='active',rules=%s WHERE benefit_id=%s AND version=%s",(dumps(rule),b['benefit_id'],b['version']))
        tx.execute("INSERT INTO business_policy_versions SELECT 'member_pricing',version,action,'mall_demo',channel,rules,effective_from,effective_until,source,status FROM business_policy_versions WHERE policy_id='pricing'")
    return seeded


def member_claim(svc,benefit,request):
    return svc.claim_coupon({'coupon_ref':ref('coupon_template','member_'+benefit)},request)


def test_member_claim_quota_identity_and_idempotence(member_coupons):
    s=member_coupons
    first=member_claim(s,'growth_platinum','growth-first')
    assert member_claim(s,'growth_platinum','growth-first')==first
    member_claim(s,'growth_platinum','growth-second')
    with pytest.raises(BusinessError): member_claim(s,'growth_platinum','growth-third')
    member_claim(s,'vip_200','vip-first'); member_claim(s,'vip_200','vip-second')
    member_claim(s,'vip_500','vip-large')
    for user in ['user_4','user_9']:
        other=CommerceService(s.store,user,scope=s.scope)
        with pytest.raises(BusinessError,match='会员身份'): member_claim(other,'vip_200',user)
    with pytest.raises(BusinessError): member_claim(s,'growth_diamond','wrong-level')
    with s.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM user_coupons')['n']==5
        assert tx.one('SELECT SUM(claimed_count) n FROM benefit_entitlements')['n']==5
        assert tx.one('SELECT COUNT(*) n FROM benefit_entitlements')['n']==3
        for row in tx.all('SELECT rules_snapshot FROM coupon_claim_rules'):
            assert decoded(row['rules_snapshot'])['membership']['entitlement_id']
    assert len(s.read('get_membership',{})['data']['benefit_entitlements'])==3


def test_vip_use_rechecked_but_growth_right_survives_level_change(member_coupons):
    s=member_coupons
    vip=member_claim(s,'vip_200','vip-use')['data']['coupon_ref']
    growth=member_claim(s,'growth_platinum','growth-use')['data']['coupon_ref']
    lines=[dict(sku_id='s1',product_id='p',merchant_id='m',amount=Decimal('300'))]
    with s.store.transaction() as tx:
        assert s.coupon_amount(tx,vip,lines,'mall_demo','CNY')['discount_amount']==50
    with s.store.transaction(write=True) as tx:
        tx.execute("UPDATE memberships SET status='suspended' WHERE user_id='user_1' AND program_id='mall_vip'")
        tx.execute("UPDATE memberships SET level_code='bronze' WHERE user_id='user_1' AND program_id='mall_growth'")
    with s.store.transaction() as tx:
        with pytest.raises(BusinessError,match='会员资格'): s.coupon_amount(tx,vip,lines,'mall_demo','CNY')
        assert s.coupon_amount(tx,growth,lines,'mall_demo','CNY')['discount_amount']==25
    result=s.read('check_coupon',{'coupon_ref':vip,'target':{'items':[{'sku_id':'s1','quantity':30}]}})
    assert result['data']['eligibility']=='ineligible'
    with pytest.raises(BusinessError): member_claim(s,'growth_bronze','downgrade-extra')


def test_cross_template_monthly_family_cannot_double_issue(member_coupons):
    s=member_coupons
    with s.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO coupon_templates SELECT 'member_vip_alias',merchant_id,platform_code,name,kind,currency,threshold_amount,discount_amount,discount_bps,cap_amount,total_limit,per_user_limit,claim_starts_at,claim_ends_at,valid_days,fixed_expires_at,status,coupon_type FROM coupon_templates WHERE coupon_id='member_vip_200'")
        tx.execute("INSERT INTO coupon_scopes VALUES ('member_vip_alias','all','*')")
        tx.execute("INSERT INTO membership_benefits SELECT 'vip_alias',version,program_id,level_code,level_version,benefit_family,period_type,quota,membership_check,JSON_SET(rules,'$.coupon_template_id','member_vip_alias'),status,effective_from,effective_until FROM membership_benefits WHERE benefit_id='vip_200'")
    def attempt(i):
        try:
            member_claim(s,'vip_200' if i%2 else 'vip_alias','parallel-'+str(i))
            return 'claimed'
        except BusinessError as e: return e.code
    with ThreadPoolExecutor(4) as pool:
        assert sorted(pool.map(attempt,range(4)))==['claimed','claimed','not_eligible','not_eligible']
    with s.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM benefit_entitlements')['n']==1
        assert tx.one('SELECT claimed_count FROM benefit_entitlements')['claimed_count']==2


def test_member_claim_rolls_back_coupon_and_quota_together(member_coupons,monkeypatch):
    s=member_coupons
    def fail_receipt(*args):
        raise RuntimeError('simulated receipt failure')
    monkeypatch.setattr(s,'record_receipt',fail_receipt)
    with pytest.raises(RuntimeError): member_claim(s,'vip_200','rollback')
    with s.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM user_coupons')['n']==0
        assert tx.one('SELECT COUNT(*) n FROM coupon_claim_rules')['n']==0
        assert tx.one('SELECT COUNT(*) n FROM benefit_entitlements')['n']==0


def test_vip_coupon_checks_expiry_even_when_wallet_has_time_left(member_coupons):
    s=member_coupons
    coupon=member_claim(s,'vip_200','expires-early')['data']['coupon_ref']
    with s.store.transaction(write=True) as tx:
        tx.execute("UPDATE membership_subscriptions SET ends_at=%s WHERE membership_id='demo_user_1_mall_vip'",(utcnow()-timedelta(seconds=1),))
    with s.store.transaction() as tx:
        with pytest.raises(BusinessError,match='会员资格'):
            s.coupon_amount(tx,coupon,[dict(sku_id='s1',product_id='p',merchant_id='m',amount=Decimal('300'))],'mall_demo','CNY')
    with pytest.raises(BusinessError): member_claim(s,'vip_200','expired-claim')
