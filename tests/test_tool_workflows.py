# 文件说明：通过离线 SQL 回归测试工具工作流、查询分页与公开字段范围。
"""Offline regressions for tool workflows; SQLite exercises portable read SQL only."""
from contextlib import contextmanager
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.services.commerce import CommerceService, service_factory
from app.services.commerce.common import BusinessError, ref
from app.tasks.reply_observations import binding_verified
from app.tasks.tool_facts import render
from app.tools.registry import execute_tool
from app.tools.request_context import reset_current_user_id, set_current_user_id


class ReadStore:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        yield ReadTransaction(self.connection)


class ReadTransaction:
    write = False

    def __init__(self, connection):
        self.connection = connection
        self.tables = set()

    def all(self, sql, args=()):
        assert sql.lstrip().startswith('SELECT')
        self.tables.update(re.findall(r'\b(?:FROM|JOIN)\s+([a-z_][a-z_0-9]*)', sql, re.I))
        return [dict(row) for row in self.connection.execute(sql.replace('%s', '?'), args)]

    def one(self, sql, args=()):
        return next(iter(self.all(sql, args)), None)


@pytest.fixture
def commerce():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.create_function('JSON_UNQUOTE', 1, lambda value: value)
    connection.executescript('''
        CREATE TABLE orders(platform_code TEXT, order_id TEXT, user_id TEXT,
                            merchant_id TEXT, status TEXT, paid_amount INTEGER);
        INSERT INTO orders VALUES ('shop','paid','alice','m','paid',30);
        INSERT INTO orders VALUES ('other','paid','alice','m','paid',30);
        INSERT INTO orders VALUES ('shop','private','bob','m','paid',30);
        CREATE TABLE commerce_provider_requests(request_id TEXT, user_id TEXT,
            platform_code TEXT, order_id TEXT, kind TEXT, status TEXT,
            provider_reference TEXT, created_at TEXT, updated_at TEXT,
            payload_ciphertext TEXT, result TEXT, lease_token TEXT);
        CREATE TABLE order_items(order_item_id TEXT, platform_code TEXT, order_id TEXT,
                                 quantity INTEGER, spec_snapshot TEXT);
        INSERT INTO order_items VALUES ('item-a','shop','paid',2,'{}');
        INSERT INTO order_items VALUES ('item-b','shop','paid',1,'{}');
        CREATE TABLE after_sale_requests(after_sale_id TEXT, platform_code TEXT,
            order_id TEXT, kind TEXT, status TEXT, stage TEXT, completed_at TEXT);
        INSERT INTO after_sale_requests VALUES ('sale','shop','paid','refund_only','new_status',NULL,NULL);
        CREATE TABLE after_sale_items(after_sale_id TEXT, order_item_id TEXT, quantity INTEGER);
        INSERT INTO after_sale_items VALUES ('sale','item-b',1);
        CREATE TABLE shipments(shipment_id TEXT, platform_code TEXT, order_id TEXT,
            shipment_kind TEXT, status TEXT, shipped_at TEXT, delivered_at TEXT);
        CREATE TABLE shipment_items(shipment_id TEXT, order_item_id TEXT, quantity INTEGER);
        CREATE TABLE service_work_orders(details TEXT, after_sale_id TEXT, kind TEXT);
    ''')
    for i in range(23):
        connection.execute('INSERT INTO commerce_provider_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            f'dispatch-{i:02}', 'alice', 'shop', 'paid', 'dispatch', 'accepted',
            f'receipt-{i:02}', '2026-10-01T00:00:00', '2026-10-02T00:00:00',
            'PRIVATE-PAYLOAD', 'PRIVATE-RESULT', 'PRIVATE-LEASE'))
    for ident, user, platform, order, kind in [
        ('wrong-platform', 'alice', 'other', 'paid', 'dispatch'),
        ('wrong-kind', 'alice', 'shop', 'paid', 'repair'),
        ('wrong-owner', 'bob', 'shop', 'private', 'dispatch'),
    ]:
        connection.execute('INSERT INTO commerce_provider_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            ident, user, platform, order, kind, 'completed', 'private-ref',
            '2026-10-01', '2026-10-02', '', '', ''))
    svc = CommerceService(ReadStore(connection), 'alice',
                          scope=SimpleNamespace(platform_code='shop', store_id='m'),
                          secret='offline-test-cursor-secret')
    try:
        yield svc, connection
    finally:
        connection.close()


def test_dispatch_status_public_tool_pages_without_provider_or_private_fields(commerce, monkeypatch):
    svc, connection = commerce
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    token = set_current_user_id('alice')
    try:
        with service_factory(lambda user, scope: svc):
            args = {'order_ref': ref('order', 'shop', 'paid')}
            first = json.loads(execute_tool('get_dispatch_status', args))
            assert first['success'], first
            assert first['data']['total'] == 23 and len(first['data']['items']) == 20
            # Progress changes between pages must not make continuation impossible.
            connection.execute("UPDATE commerce_provider_requests SET status='completed' WHERE request_id='dispatch-22'")
            last = json.loads(execute_tool('get_dispatch_status', dict(args, cursor=first['next_cursor'])))
            assert last['success'] and last['next_cursor'] is None
            rows = first['data']['items'] + last['data']['items']
            assert [r['request_id'] for r in rows] == [f'dispatch-{i:02}' for i in range(23)]
            assert rows[-1]['status'] == 'completed'
            assert 'PRIVATE' not in json.dumps([first, last])
            assert first['data']['order_id'] == 'paid' and first['data']['platform_code'] == 'shop'
    finally:
        reset_current_user_id(token)


@pytest.mark.parametrize('platform,order', [('shop','private'), ('other','paid'), ('shop','absent')])
def test_dispatch_status_checks_order_owner_and_scope(commerce, platform, order):
    svc, _ = commerce
    with pytest.raises(BusinessError) as error:
        svc.read('get_dispatch_status', {'order_ref': ref('order', platform, order)})
    assert error.value.code == 'not_found'


def test_dispatch_cursor_cannot_cross_user(commerce):
    svc, _ = commerce
    args = {'order_ref': ref('order', 'shop', 'paid')}
    page = svc.read('get_dispatch_status', args)
    other = CommerceService(svc.store, 'bob', scope=svc.scope, secret=svc.secret)
    with pytest.raises(BusinessError):
        other.read('get_dispatch_status', dict(args, cursor=page['next_cursor']))


def test_dispatch_empty_and_unknown_status_are_not_successful_fulfillment(commerce):
    svc, connection = commerce
    connection.execute("UPDATE commerce_provider_requests SET status='vendor_new_status'")
    args = {'order_ref': ref('order', 'shop', 'paid')}
    page = svc.read('get_dispatch_status', args)
    assert page['data']['items'][0]['status'] == 'unknown'
    text = '\n'.join(render('get_dispatch_status', page))
    assert '未知' in text and '不等于已经发货' in text
    connection.execute("DELETE FROM commerce_provider_requests WHERE kind='dispatch'")
    empty = svc.read('get_dispatch_status', args)
    assert empty['status'] == 'no_match'
    assert '未查到' in '\n'.join(render('get_dispatch_status', empty))


def test_dispatch_reply_rejects_wrong_order_binding():
    record = SimpleNamespace(details={'platform_code':'shop', 'order_id':'paid'})
    receipt = {'tool':'get_dispatch_status', 'arguments':{'order_ref':ref('order','shop','paid')},
               'result':{'success':True, 'data':{'platform_code':'shop','order_id':'other','items':[]}}}
    assert not binding_verified(record, receipt)
    receipt['result']['data']['order_id'] = 'paid'
    assert binding_verified(record, receipt)


def test_unknown_sale_only_obscures_its_own_item(commerce):
    svc, _ = commerce
    result = svc.read('get_order_items', {'order_ref':ref('order','shop','paid')})
    first, second = result['data']['items']
    assert first['after_sale_occupied'] == 0
    assert first['after_sale_status'] == 'none'
    assert second['after_sale_occupied'] is None
    assert second['after_sale_status'] == 'unknown'


def test_validation_returns_field_paths_without_echoing_sensitive_values(monkeypatch):
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    result = json.loads(execute_tool('prepare_invoice', {
        'order_ref':'order', 'invoice_type':'company', 'title':'PRIVATE-TITLE'}))
    assert result['error']['code'] == 'invalid_arguments'
    assert result['error']['validation_errors']
    result = json.loads(execute_tool('quote_checkout', {
        'items':[{'sku_id':'sku', 'quantity':True}], 'address_id':'1', 'password':'PRIVATE-PASSWORD'}))
    errors = result['error']['validation_errors']
    assert {'path':['items',0,'quantity'], 'type':'int_type'} in errors
    assert 'PRIVATE' not in json.dumps(result)
