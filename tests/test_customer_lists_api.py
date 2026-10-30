# 文件说明：测试客户资料、订单与持券列表的鉴权、筛选和分页接口。
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.commerce import CommerceService
import pymysql
import pytest
from app.services.commerce.common import BusinessError
from app.config.settings import settings
from tests.api_fixtures import api


def test_profile_uses_current_account_and_membership_facts(api, monkeypatch):
    client, headers, *_ = api
    monkeypatch.setattr(settings, 'api_allow_legacy_jwt', True)
    calls = []

    class ProfileTransaction:
        def one(self, sql, args):
            assert sql == 'SELECT username,created_at FROM users WHERE user_id=%s'
            if args[0] == 'missing': return None
            if args[0] == 'unavailable': raise pymysql.OperationalError('database unavailable')
            return dict(username={'alice': '爱丽丝', 'bob': '鲍勃'}[args[0]], created_at=datetime(2026, 10, 1))

        def all(self, sql, args):
            assert sql == 'SELECT DISTINCT platform_code FROM memberships WHERE user_id=%s ORDER BY platform_code'
            return [dict(platform_code='mall'), dict(platform_code='shop')] if args[0] == 'alice' else []

    @contextmanager
    def transaction():
        yield ProfileTransaction()

    def facts(tx, user, platform):
        calls.append((user, platform))
        return dict(platform_code=platform, growth_membership=dict(name='铂金', effective_status='active'),
                    vip=dict(status='expired', is_vip=False), store_memberships=[])

    monkeypatch.setattr('app.api.commerce.service', lambda user: SimpleNamespace(user=user, store=SimpleNamespace(transaction=transaction)))
    monkeypatch.setattr('app.services.commerce.customer_views.membership_facts', facts)
    assert client.get('/api/commerce/profile').status_code == 401
    response = client.get('/api/commerce/profile?user_id=bob', headers=headers('alice'))
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert response.json()['username'] == '爱丽丝'
    assert calls == [('alice', 'mall'), ('alice', 'shop')]
    assert response.json()['memberships'][0]['vip']['status'] == 'expired'
    assert client.get('/api/commerce/profile', headers=headers('bob')).json()['memberships'] == []
    assert client.get('/api/commerce/profile', headers=headers('missing')).status_code == 404
    assert client.get('/api/commerce/profile', headers=headers('unavailable')).status_code == 503


def install(monkeypatch):
    monkeypatch.setattr(settings, 'api_allow_legacy_jwt', True)
    calls = []

    @contextmanager
    def transaction():
        yield object()

    class Service:
        store = SimpleNamespace(transaction=transaction)

        def __init__(self, user):
            self.user = user

        def find_orders(self, tx, **args):
            calls.append((self.user, args))
            if args['cursor'] == 'bad':
                raise BusinessError('invalid_cursor', '分页游标无效或已过期')
            return {'data': {'items': [{'order_id': self.user + '-order', 'platform_code': 'shop'}], 'total': 1}, 'next_cursor': None}

        def order_rows(self, tx, table, order):
            return [dict(order_item_id='item', title_snapshot='耳机', spec_snapshot='{"颜色":"白色"}', quantity=2)]

        def list_my_coupons(self, tx, **args):
            calls.append((self.user, args))
            return {'data': {'items': [dict(user_coupon_id=self.user + '-coupon', status='available')], 'total': 1}, 'next_cursor': None}

        def coupon(self, tx, reference):
            return dict(name='当前规则', kind='amount_off', discount_amount=5), {}, [dict(scope_type='all')]

        def frozen_coupon(self, tx, template, instance, scopes):
            return {**template, 'name': '领取时规则', 'discount_amount': 20}, scopes

    monkeypatch.setattr('app.api.commerce.service', Service)
    return calls


def test_lists_require_auth_and_use_authenticated_identity(api, monkeypatch):
    client, headers, *_ = api
    calls = install(monkeypatch)
    for path in ['/api/commerce/orders', '/api/commerce/coupons']:
        assert client.get(path).status_code == 401
        response = client.get(path + '?user_id=someone-else', headers=headers('alice'))
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        assert calls[-1][0] == 'alice'
    assert response.json()['items'][0]['template']['discount_amount'] == 20
    assert response.json()['items'][0]['rules_basis'] == 'claim_snapshot'
    assert calls[-1][1]['exclude_expired'] is True
    assert calls[-1][1]['coupon_type'] == 'all'


def test_order_filters_cursor_and_product_serialization(api, monkeypatch):
    client, headers, *_ = api
    calls = install(monkeypatch)
    for status in ['all', 'pending_payment', 'paid', 'pending_receipt', 'after_sale']:
        response = client.get('/api/commerce/orders', params=dict(status=status, cursor='page-two'), headers=headers())
        assert response.status_code == 200
        assert calls[-1][1] == dict(status=status, cursor='page-two')
        assert response.json()['items'][0]['items'][0]['spec_snapshot'] == {'颜色': '白色'}
    assert client.get('/api/commerce/orders?status=invalid', headers=headers()).status_code == 422
    response = client.get('/api/commerce/orders?cursor=bad', headers=headers())
    assert response.status_code == 400
    assert response.json()['detail'] == '分页游标无效或已过期'


@pytest.mark.parametrize('path,error,status,detail', [
    ('profile', BusinessError('not_found', '会员资料暂不可用'), 503, '会员资料暂不可用'),
    ('orders', BusinessError('invalid_cursor', '游标无效'), 400, '游标无效'),
    ('coupons', BusinessError('state_changed', '查询结果已变化'), 400, '查询结果已变化'),
    ('coupons', pymysql.OperationalError('private database details'), 503, '暂时无法加载，请稍后重试'),
])
def test_customer_list_errors_preserve_status_and_redaction(api, monkeypatch, path, error, status, detail):
    client, headers, *_ = api
    monkeypatch.setattr(settings, 'api_allow_legacy_jwt', True)

    @contextmanager
    def transaction():
        raise error
        yield

    monkeypatch.setattr('app.api.commerce.service', lambda user: SimpleNamespace(
        user=user, store=SimpleNamespace(transaction=transaction)))
    response = client.get('/api/commerce/' + path, headers=headers())
    assert response.status_code == status
    assert response.json() == {'detail': detail}


def test_coupon_expiry_types_and_pagination_with_owned_wallet(api, monkeypatch):
    client, headers, *_ = api
    monkeypatch.setattr(settings, 'api_allow_legacy_jwt', True)
    now = datetime(2026, 10, 25, 12)
    monkeypatch.setattr('app.services.commerce.rules.utcnow', lambda: now)
    rows, templates, snapshots = [], {}, {}

    def add(cid, kind='shop', status='available', expires=None, user='alice', frozen_type=None):
        rows.append(dict(user_coupon_id=cid, coupon_id=cid, user_id=user, status=status,
                         expires_at=expires or now + timedelta(days=7)))
        templates[cid] = dict(coupon_id=cid, coupon_type=kind, name=cid, kind='amount_off',
                              currency='CNY', discount_amount=10, threshold_amount=100, platform_code='shop')
        if frozen_type:
            snapshots[cid] = dict(template={**templates[cid], 'coupon_type': frozen_type}, scopes=[dict(scope_type='all')])

    # Expired coupons precede valid coupons; filtering must happen before pagination.
    for i in range(23):
        add(f'expired-{i}', expires=now - timedelta(days=1))
    for state in ['used', 'reserved', 'invalid']:
        add('expired-' + state, status=state, expires=now - timedelta(seconds=1))
    add('boundary', expires=now)
    add('marked-expired', status='expired')
    for i in range(25):
        add(f'shop-{i:02}')
    add('official', kind='platform', status='used')
    add('category', kind='category')
    add('newcomer', kind='newcomer')
    add('general', kind='general')
    add('frozen-shop', kind='platform', frozen_type='shop')
    add('other-user', user='bob')

    class WalletTransaction:
        write = False

        def all(self, sql, args=()):
            if 'FROM user_coupons WHERE user_id=' in sql:
                return [dict(row) for row in sorted(rows, key=lambda r: (r['expires_at'], r['user_coupon_id'])) if row['user_id'] == args[0]]
            if 'FROM coupon_scopes' in sql:
                return [dict(scope_type='all')]
            raise AssertionError(sql)

        def one(self, sql, args=()):
            if 'FROM user_coupons WHERE user_coupon_id=' in sql:
                return next((dict(r) for r in rows if (r['user_coupon_id'], r['user_id']) == args), None)
            if 'FROM coupon_templates' in sql:
                return dict(templates[args[0]])
            if 'FROM coupon_claim_rules' in sql:
                return {'rules_snapshot': snapshots[args[0]]} if args[0] in snapshots else None
            raise AssertionError(sql)

    @contextmanager
    def transaction():
        yield WalletTransaction()

    monkeypatch.setattr('app.api.commerce.service', lambda user: CommerceService(
        SimpleNamespace(transaction=transaction), user, secret='test-customer-wallet-cursor-secret'))

    def get(**params):
        response = client.get('/api/commerce/coupons', params=params, headers=headers('alice'))
        assert response.status_code == 200, response.text
        return response.json()

    first = get()
    assert first['total'] == 30 and len(first['items']) == 20
    second = get(cursor=first['next_cursor'])
    assert len(second['items']) == 10 and second['next_cursor'] is None
    all_items = first['items'] + second['items']
    assert len({c['user_coupon_id'] for c in all_items}) == 30
    assert all(c['expires_at'] > now.isoformat() and c['status'] != 'expired' for c in all_items)
    assert not any(c['user_coupon_id'] == 'other-user' for c in all_items)
    shop = get(coupon_type='shop')
    assert shop['total'] == 26 and len(shop['items']) == 20
    rest = get(coupon_type='shop', cursor=shop['next_cursor'])
    assert len(rest['items']) == 6 and rest['next_cursor'] is None
    assert all(c['coupon_type'] == 'shop' for c in shop['items'] + rest['items'])
    for kind in ['platform', 'category', 'newcomer', 'general']:
        result = get(coupon_type=kind)
        assert result['total'] == 1 and result['items'][0]['coupon_type'] == kind
    assert get(coupon_type='platform')['items'][0]['status'] == 'used'
    assert client.get('/api/commerce/coupons', params=dict(coupon_type='platform', cursor=shop['next_cursor']), headers=headers('alice')).status_code == 400
    assert client.get('/api/commerce/coupons?coupon_type=invalid', headers=headers('alice')).status_code == 422
