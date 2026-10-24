"""Coupon entitlement and stock checks against disposable MySQL only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest

from tests.test_core_tools import database, svc, read
from app.data.auth_store import MySqlAuthStore
from app.services.commerce import CommerceService
from app.services.commerce.common import BusinessError, ref, utcnow, dumps


def test_registration_grants_once_and_imported_users_are_not_new(svc):
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE coupon_templates SET coupon_type='newcomer' WHERE coupon_id='c'")
    with pytest.raises(BusinessError, match='首次注册'):
        svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'old-user')
    assert read(svc,'list_claimable_coupons')['data']['total']==0
    uid=MySqlAuthStore(svc.store.dsn).create_user('fresh-account','test-hash-not-used-for-login')
    fresh=CommerceService(svc.store,uid,scope=svc.scope)
    args={'coupon_ref':ref('coupon_template','c')}
    first=fresh.claim_coupon(args,'first')
    assert fresh.claim_coupon(args,'first')==first
    with svc.store.transaction(write=True) as tx:
        # A second campaign cannot grant the same registration benefit again.
        tx.execute("INSERT INTO coupon_templates(coupon_id,platform_code,name,kind,currency,threshold_amount,discount_amount,total_limit,per_user_limit,claim_starts_at,claim_ends_at,valid_days,status,coupon_type) SELECT 'c2',platform_code,name,kind,currency,threshold_amount,discount_amount,total_limit,per_user_limit,claim_starts_at,claim_ends_at,valid_days,status,coupon_type FROM coupon_templates WHERE coupon_id='c'")
        tx.execute("INSERT INTO coupon_scopes VALUES ('c2','all','*')")
    with pytest.raises(BusinessError, match='首次注册'):
        fresh.claim_coupon({'coupon_ref':ref('coupon_template','c2')},'second')


def test_last_coupon_cannot_be_claimed_by_two_users(svc):
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE coupon_templates SET total_limit=1 WHERE coupon_id='c'")
    def claim(user):
        try:
            CommerceService(svc.store,user,scope=svc.scope).claim_coupon({'coupon_ref':ref('coupon_template','c')},'last-'+user)
            return 'claimed'
        except BusinessError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim,['alice','bob']))==['claimed','not_eligible']
    with svc.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM user_coupons')['n']==1


def test_shop_scope_and_relative_expiry(svc):
    coupon=svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'scope')['data']['coupon_ref']
    with svc.store.transaction() as tx:
        held=tx.one('SELECT claimed_at,expires_at FROM user_coupons')
        assert (held['expires_at']-held['claimed_at']).days==7
        with pytest.raises(BusinessError):
            svc.coupon_amount(tx,coupon,[{'sku_id':'other','product_id':'other','merchant_id':'other','amount':20}], 'shop','CNY')


def test_promotions_must_explicitly_allow_coupon(svc):
    coupon=svc.claim_coupon({'coupon_ref':ref('coupon_template','c')},'promotion')['data']['coupon_ref']
    now=utcnow()
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='pricing'",(dumps({'schema_version':1,'promotions_with_coupons':True}),))
        tx.execute("INSERT INTO promotion_rules(promotion_id,version,platform_code,name,kind,currency,minimum_amount,discount_amount,starts_at,ends_at,status,allows_coupon) VALUES ('promo',1,'shop','满20减2','amount_off','CNY',20,2,%s,%s,'active',TRUE)", (now-timedelta(days=1),now+timedelta(days=1)))
        tx.execute("INSERT INTO promotion_scopes(promotion_id,version,scope_type,scope_id) VALUES ('promo',1,'all','*')")
    args={'items':[{'sku_id':'s1','quantity':3}],'coupon_refs':[coupon]}
    assert read(svc,'estimate_purchase',**args)['data']['goods_after_discount']==23
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE promotion_rules SET allows_coupon=FALSE")
    with pytest.raises(BusinessError,match='该活动不允许'):
        read(svc,'estimate_purchase',**args)
