"""Real MySQL search regressions in a unique disposable database, never the app DB."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import os
from uuid import uuid4

import pymysql
import pytest

from app.data.business_store import Transaction
from app.data.order_dao import parse_mysql_dsn
from app.services.commerce import CommerceService
from app.services.commerce.common import BusinessError, dumps, ref
from app.tasks.candidates import BusinessScope
from app.tools.catalog import BY_NAME


@pytest.fixture
def search_db():
    dsn = os.environ.get('CORE_TOOLS_TEST_MYSQL_ADMIN')
    if not dsn:
        pytest.skip('CORE_TOOLS_TEST_MYSQL_ADMIN must explicitly enable disposable MySQL tests')
    params = parse_mysql_dsn(dsn)
    params.pop('database', None)
    connection = pymysql.connect(**params, charset='utf8mb4', autocommit=False)
    database = 'test_order_search_' + uuid4().hex
    try:
        with connection.cursor() as cursor:
            cursor.execute('CREATE DATABASE `' + database + '` CHARACTER SET utf8mb4')
            connection.select_db(database)
            cursor.execute('''CREATE TABLE orders (
                platform_code VARCHAR(100),order_id VARCHAR(100),user_id VARCHAR(100),merchant_id VARCHAR(100),
                status VARCHAR(100),created_at DATETIME,paid_amount DECIMAL(12,2),currency VARCHAR(3),
                PRIMARY KEY(platform_code,order_id)) CHARACTER SET utf8mb4''')
            cursor.execute('''CREATE TABLE order_items (
                order_item_id VARCHAR(100) PRIMARY KEY,platform_code VARCHAR(100),order_id VARCHAR(100),
                title_snapshot TEXT,spec_snapshot JSON,quantity INT) CHARACTER SET utf8mb4''')
            cursor.execute('''CREATE TABLE after_sale_requests (
                after_sale_id VARCHAR(100) PRIMARY KEY,platform_code VARCHAR(100),order_id VARCHAR(100),
                user_id VARCHAR(100),created_at DATETIME,status VARCHAR(100)) CHARACTER SET utf8mb4''')
            cursor.execute('''CREATE TABLE after_sale_items (
                after_sale_id VARCHAR(100),order_item_id VARCHAR(100),quantity INT) CHARACTER SET utf8mb4''')
        tx = Transaction(connection, write=True)

        class Store:
            @contextmanager
            def transaction(self, **kwargs):
                yield tx

        svc = CommerceService(Store(), 'alice', scope=BusinessScope(platform_code='shop'), secret='search-test')

        def order(oid, *, title='鞋', specs=None, quantity=1, amount='100', currency='CNY',
                  user='alice', platform='shop', merchant='m', day=0):
            tx.execute('INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                       (platform, oid, user, merchant, 'paid', datetime(2026, 10, 20)+timedelta(days=day), amount, currency))
            tx.execute('INSERT INTO order_items VALUES (%s,%s,%s,%s,%s,%s)',
                       ('i-'+oid, platform, oid, title, dumps(specs or {}), quantity))

        def read(name='find_orders', **args):
            validated = BY_NAME[name].arguments.model_validate(args).model_dump(by_alias=True, exclude_none=True)
            return svc.read(name, validated)

        yield tx, order, read, svc
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute('DROP DATABASE IF EXISTS `' + database + '`')
        connection.close()


def ids(result):
    return [row['order_id'] for row in result['data']['items']]


def test_exact_specs_quantity_amount_and_ownership(search_db):
    tx, order, read, svc = search_db
    red = {'颜色': '红色', '尺寸': '38'}
    order('target', specs=red, quantity=2, amount='200')
    order('half-size', specs={**red, '尺寸': '38.5'}, quantity=2, amount='200')
    order('split', specs={'颜色': '蓝色', '尺寸': '38'}, quantity=2, amount='200')
    tx.execute("INSERT INTO order_items VALUES ('shirt','shop','split','衬衫',%s,2)", (dumps(red),))
    order('other-user', specs=red, quantity=2, amount='200', user='bob')
    order('other-platform', specs=red, quantity=2, amount='200', platform='elsewhere')
    order('wrong-currency', specs=red, quantity=2, amount='200', currency='USD')
    order('wrong-quantity', specs=red, quantity=1, amount='200')
    order('wrong-paid', specs=red, quantity=2, amount='201')
    args = dict(query='鞋', specs=[{'name': k, 'value': v} for k, v in red.items()], item_quantity=2,
                paid_amount_range={'minimum': '200', 'maximum': '200', 'currency': 'CNY'})
    result = read(**args)
    assert ids(result) == ['target']
    assert result['data']['items'][0]['purchase_summary'][0]['query_match'] is True
    assert ids(read(order_id='target')) == ['target']
    assert read(query='%')['data']['total'] == 0
    assert read(query="' OR 1=1 --")['data']['total'] == 0
    assert read(order_id='target', item_quantity=1)['data']['total'] == 0
    svc.scope = BusinessScope(platform_code='shop', store_id='other')
    assert read()['data']['total'] == 0


def test_hints_rank_before_paging_without_excluding_and_bind_cursor(search_db):
    tx, order, read, _ = search_db
    for index in range(12):
        order('recent-'+str(index), day=2, specs={'颜色': '蓝色'})
    order('old-match', day=-7, specs={'颜色': '红色'}, amount='200', quantity=2)
    order('foreign-match', day=-7, specs={'颜色': '红色'}, amount='200', quantity=2, user='bob')
    args = dict(query='鞋', hints={'specs': [{'name': '颜色', 'value': '红色'}], 'item_quantity': 2,
                'paid_amount_range': {'minimum': '190', 'maximum': '210', 'currency': 'CNY'},
                'created_range': {'from': '2026-10-13T00:00:00Z', 'to': '2026-10-14T00:00:00Z'}})
    first = read(**args)
    assert first['data']['total'] == 13 and ids(first)[0] == 'old-match'
    row = first['data']['items'][0]
    assert row['hint_score'] == 3 and row['unmatched_hints'] == []
    assert row['purchase_summary'][0]['hint_match'] is True
    assert set(first['data']['items'][1]['unmatched_hints']) == {'item', 'created_range', 'paid_amount_range'}
    second = read(**args, cursor=first['next_cursor'])
    assert len(set(ids(first)+ids(second))) == 13 and second['data']['has_more'] is False
    with pytest.raises(BusinessError, match='游标'):
        read(query='鞋', hints={'item_quantity': 3}, cursor=first['next_cursor'])
    # A red shirt on the same order cannot satisfy a red-shoe hint.
    tx.execute("INSERT INTO order_items VALUES ('shirt','shop','recent-0','衬衫',%s,2)", (dumps({'颜色': '红色'}),))
    candidate = read(order_id='recent-0', query='鞋', hints={'specs': [{'name': '颜色', 'value': '红色'}]})['data']['items'][0]
    assert candidate['hint_matches']['item'] is False


def test_date_boundaries_null_amount_and_empty_hints(search_db):
    _, order, read, _ = search_db
    order('start', day=0, amount='0')
    order('end', day=1, amount=None)
    dates = {'from': '2026-10-20T08:00:00+08:00', 'to': '2026-10-21T08:00:00+08:00'}
    assert ids(read(created_range=dates)) == ['start']
    assert ids(read(paid_amount_range={'maximum': '0', 'currency': 'CNY'})) == ['start']
    assert len(ids(read(hints={}))) == 2
    unknown = read(order_id='end', hints={'paid_amount_range': {'maximum': '0', 'currency': 'CNY'}})['data']['items'][0]
    assert unknown['hint_matches']['paid_amount_range'] is False


def test_after_sale_multiword_same_item_and_exact_id(search_db):
    tx, order, read, _ = search_db
    for oid, color, size, user in [('a', '红色', '38', 'alice'), ('b', '红色', '38.5', 'alice'), ('c', '红色', '38', 'bob')]:
        order(oid, title='NIKE 运动鞋', specs={'颜色': color, '尺寸': size}, user=user)
        tx.execute('INSERT INTO after_sale_requests VALUES (%s,%s,%s,%s,%s,%s)',
                   ('sale-'+oid, 'shop', oid, user, datetime(2026,10,25), 'approved'))
        tx.execute('INSERT INTO after_sale_items VALUES (%s,%s,1)', ('sale-'+oid, 'i-'+oid))
    result = read('find_after_sales', query='NIKE 运动鞋', specs=[{'name': '尺寸', 'value': '38'}])
    assert [r['after_sale_id'] for r in result['data']['items']] == ['sale-a']
    assert read('find_after_sales', after_sale_id='sale-c')['data']['total'] == 0
    assert read('find_after_sales', order_ref=ref('order','shop','b'), after_sale_id='sale-a')['data']['total'] == 0
    assert read('find_after_sales', query='sale-a')['data']['total'] == 1
