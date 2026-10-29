"""Real MySQL scale, isolation and aggregate regressions for collection reads."""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.data.business_store import Transaction
from app.services.commerce import CommerceService
from app.services.commerce.common import BusinessError, dumps, ref, utcnow
from app.tasks.candidates import BusinessScope
from tests.test_core_tools import database, svc, read  # noqa: F401


def bulk(tx, sql, rows):
    with tx.connection.cursor() as cursor:
        cursor.executemany(sql, rows)


def monitor(monkeypatch):
    calls = []
    original = Transaction.all

    def bounded(self, sql, args=()):
        rows = original(self, sql, args)
        calls.append((sql, len(rows)))
        assert len(rows) <= 100, (sql, len(rows))
        return rows

    monkeypatch.setattr(Transaction, 'all', bounded)
    return calls


def test_history_over_ten_thousand_is_filtered_and_paged_in_sql(svc, monkeypatch):
    now = utcnow(); count = 10005
    with svc.store.transaction(write=True) as tx:
        bulk(tx, '''INSERT INTO orders(platform_code,order_id,user_id,status,created_at,merchant_id,currency,goods_amount,
            discount_amount,shipping_amount,payable_amount,paid_amount,updated_at)
            VALUES ('shop',%s,'alice','paid',%s,'m','CNY',30,0,0,30,30,%s)''',
            [(f'h-{i:05}', now, now) for i in range(count)])
        bulk(tx, '''INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,created_at,updated_at)
            VALUES (%s,'shop','paid','alice','refund_only','processing','test',%s,%s)''',
            [(f'a-{i:05}', now, now) for i in range(count)])
        bulk(tx, "INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity) VALUES (%s,'i-paid',1)",
            [(f'a-{i:05}',) for i in range(count)])
        bulk(tx, '''INSERT INTO invoice_requests(invoice_id,platform_code,order_id,user_id,invoice_type,title_ciphertext,
            key_version,status,request_id,created_at) VALUES (%s,'shop','paid','alice','personal','private',1,'pending',%s,%s)''',
            [(f'v-{i:05}', f'v-{i:05}', now) for i in range(count)])
        bulk(tx, '''INSERT INTO user_coupons(user_coupon_id,coupon_id,user_id,status,claimed_at,expires_at)
            VALUES (%s,'c','alice','available',%s,%s)''', [(f'c-{i:05}', now, now + timedelta(days=1)) for i in range(count)])
        template = tx.one("SELECT * FROM coupon_templates WHERE coupon_id='c'")
        snapshot = dumps(dict(template=template, scopes=[dict(scope_type='all', scope_id='*')]))
        bulk(tx, 'INSERT INTO coupon_claim_rules(user_coupon_id,rules_snapshot,captured_at) VALUES (%s,%s,%s)',
            [(f'c-{i:05}', snapshot, now) for i in range(count)])
        # A later edit to the live template must not change frozen-name matching.
        tx.execute("UPDATE coupon_templates SET name='改名' WHERE coupon_id='c'")
    calls = monitor(monkeypatch)
    cases = [
        ('find_orders', dict(query='h-', status='paid'), 'order_id', 'orders', 'h-', 10),
        ('find_after_sales', dict(query='耳机', status='processing'), 'after_sale_id', 'after_sale_requests', 'a-', 10),
        ('get_invoices', dict(target_ref=ref('order', 'shop', 'paid')), 'invoice_id', 'invoice_requests', 'v-', 20),
        ('find_my_coupons', dict(query='满十减五', status='available', subject_ref=ref('sku', 's1')), 'user_coupon_id', 'user_coupons', 'c-', 10),
    ]
    for tool, args, key, table, prefix, size in cases:
        calls.clear()
        first = read(svc, tool, **args)
        assert first['data']['total'] == count, tool
        assert [row[key] for row in first['data']['items']] == [f'{prefix}{i:05}' for i in range(size)]
        assert len(calls) <= 8  # Includes SQL counts and ownership checks; no per-record fetches.
        with svc.store.transaction(write=True) as tx:
            tx.execute(f'UPDATE {table} SET status=%s WHERE {key}=%s', ('cancelled' if table != 'user_coupons' else 'used', prefix + '00000'))
        second = read(svc, tool, **dict(args, cursor=first['next_cursor']))
        assert [row[key] for row in second['data']['items']] == [f'{prefix}{i:05}' for i in range(size, size * 2)]
        assert not set(row[key] for row in first['data']['items']) & set(row[key] for row in second['data']['items'])
        with pytest.raises(BusinessError, match='游标'):
            read(svc, tool, **dict(args, cursor=first['next_cursor'] + 'tampered'))
    assert read(svc, 'find_my_coupons', query='改名')['data']['total'] == 0
    assert read(svc, 'find_orders', query='%')['data']['total'] == 0  # Literal LIKE metacharacter.
    assert read(svc, 'find_after_sales', query='_')['data']['total'] == 0
    for scope in (BusinessScope(platform_code='elsewhere'), BusinessScope(platform_code='shop', store_id='elsewhere')):
        scoped = CommerceService(svc.store, 'alice', scope=scope, secret=svc.secret)
        for tool in ('find_orders', 'find_after_sales', 'find_my_coupons', 'get_invoices'):
            assert read(scoped, tool)['data']['total'] == 0
    other = CommerceService(svc.store, 'bob', scope=svc.scope, secret=svc.secret)
    for tool in ('find_after_sales', 'find_my_coupons', 'get_invoices'):
        assert read(other, tool)['data']['total'] == 0


def test_large_order_and_full_financial_and_fulfillment_aggregates(svc, monkeypatch):
    now = utcnow(); count = 10005; order_ref = ref('order', 'shop', 'paid')
    with svc.store.transaction(write=True) as tx:
        bulk(tx, '''INSERT INTO order_items(order_item_id,platform_code,order_id,sku_id,product_id,title_snapshot,spec_snapshot,
            quantity,unit_price,discount_amount,payable_amount) VALUES (%s,'shop','paid','s1','p','耳机','{}',3,10,0,30)''',
            [(f'big-{i:05}',) for i in range(count)])
        bulk(tx, '''INSERT INTO payment_transactions(payment_id,platform_code,order_id,user_id,provider,amount,currency,status,created_at)
            VALUES (%s,'shop','paid','alice','test',2,'CNY','succeeded',%s)''', [(f'pay-{i:05}', now) for i in range(count)])
        tx.execute('''INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,created_at,updated_at)
            VALUES ('sale','shop','paid','alice','refund_only','completed','test',%s,%s)''', (now, now))
        bulk(tx, '''INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity) VALUES ('sale',%s,1)''', [(f'big-{i:05}',) for i in range(count)])
        bulk(tx, '''INSERT INTO refund_transactions(refund_id,after_sale_id,amount,currency,status,requested_at)
            VALUES (%s,'sale',1,'CNY','succeeded',%s)''', [(f'r-{i:05}', now) for i in range(count)])
        bulk(tx, '''INSERT INTO shipments(shipment_id,platform_code,order_id,status,shipped_at,delivered_at,created_at)
            VALUES (%s,'shop','paid','delivered',%s,%s,%s)''', [(f'ship-{i:05}', now, now, now) for i in range(count)])
        bulk(tx, "INSERT INTO shipment_items(shipment_id,order_item_id,quantity) VALUES (%s,'big-00000',1)", [(f'ship-{i:05}',) for i in range(count)])
        for i in range(7):
            tx.execute('''INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,created_at,updated_at)
                VALUES (%s,'shop','paid','alice','refund_only',%s,'test',%s,%s)''', (f'z-{i}', 'processing' if i == 6 else 'completed', now, now))
            tx.execute("INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity) VALUES (%s,'big-00000',1)", (f'z-{i}',))
    calls = monitor(monkeypatch)
    items = read(svc, 'get_order_items', order_ref=order_ref)
    assert items['data']['total'] == count + 1 and len(items['data']['items']) == 20
    assert len(calls) == 6
    first = items['data']['items'][0]
    assert first['recorded_shipped'] == first['recorded_delivered'] == count
    assert first['after_sale_occupied'] == 8 and first['after_sale_status'] == 'processing'
    assert len(first['after_sales']) == 5 and first['after_sales_total'] == 8 and first['after_sales_truncated']
    detail = read(svc, 'get_order', order_ref=order_ref)['data']
    assert detail['item_count'] == count + 1 and len(detail['items']) == 5
    assert detail['related']['payment_transactions']['count'] == count
    listing = read(svc, 'find_orders', query='paid')['data']['items']
    assert next(row for row in listing if row['order_id'] == 'paid')['item_count'] == count + 1
    payments = read(svc, 'get_payments', order_ref=order_ref)
    assert payments['data']['total'] == count and payments['data']['recorded_success_amounts']['CNY'] == Decimal(2 * count)
    assert payments['data']['recorded_difference'] == Decimal(2 * count - 30)
    refunds = read(svc, 'get_refunds', target_ref=order_ref)
    assert refunds['data']['total'] == count and refunds['data']['recorded_success_amount'] == Decimal(count)
    following = read(svc, 'get_refunds', target_ref=order_ref, cursor=refunds['next_cursor'])
    assert following['data']['amounts_by_currency_and_status'] == refunds['data']['amounts_by_currency_and_status']
    sale = read(svc, 'get_after_sale_status', after_sale_id='sale')['data']
    assert sale['item_count'] == count and len(sale['purchase_items']) == 20 and sale['items_truncated']
    assert sale['refund_summary']['CNY']['succeeded'] == Decimal(count)
    logistics = read(svc, 'get_logistics', target_ref=order_ref)['data']
    assert logistics['total'] == count and len(logistics['items']) == 5
    assert logistics['quantities_total'] == count + 1 and logistics['quantities_truncated']
    assert logistics['quantity_totals']['purchased'] == 3 * (count + 1)
    assert logistics['quantity_totals']['recorded_shipped'] == count


def test_coupon_expiry_unknown_status_and_missing_rules(svc):
    now = utcnow()
    with svc.store.transaction(write=True) as tx:
        template = tx.one("SELECT * FROM coupon_templates WHERE coupon_id='c'")
        for key, status, delta in [('expired', 'available', -1), ('used', 'used', -1), ('unknown', 'new', 1)]:
            tx.execute("INSERT INTO user_coupons(user_coupon_id,coupon_id,user_id,status,claimed_at,expires_at) VALUES (%s,'c','alice',%s,%s,%s)", (key, status, now, now + timedelta(days=delta)))
            tx.execute('INSERT INTO coupon_claim_rules(user_coupon_id,rules_snapshot,captured_at) VALUES (%s,%s,%s)',
                (key, dumps(dict(template=template, scopes=[dict(scope_type='product', scope_id='p')])), now))
    for status in ('expired', 'used', 'unknown'):
        rows = read(svc, 'find_my_coupons', status=status, subject_ref=ref('sku', 's1'))['data']['items']
        assert len(rows) == 1 and rows[0]['status'] == status
    with svc.store.transaction(write=True) as tx:
        tx.execute("DELETE FROM coupon_claim_rules WHERE user_coupon_id='used'")
    with pytest.raises(BusinessError) as error:
        read(svc, 'find_my_coupons', query='not matching')
    assert error.value.code == 'insufficient_data'


def test_mixed_key_ordering_and_filtered_status_changes(svc):
    now = utcnow()
    with svc.store.transaction(write=True) as tx:
        bulk(tx, '''INSERT INTO orders(platform_code,order_id,user_id,status,created_at,merchant_id,currency,updated_at)
            VALUES ('shop',%s,'alice','paid',%s,'m','CNY',%s)''',
            [(f'page-{i:02}', now - timedelta(days=i // 7), now) for i in range(25)])
    args = dict(query='page-', status='paid')
    first = read(svc, 'find_orders', **args)
    assert [r['order_id'] for r in first['data']['items']] == [f'page-{i:02}' for i in range(10)]
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE orders SET status='closed' WHERE order_id IN ('page-00','page-10')")
    second = read(svc, 'find_orders', **dict(args, cursor=first['next_cursor']))
    assert [r['order_id'] for r in second['data']['items']] == [f'page-{i:02}' for i in range(11, 21)]
    final = read(svc, 'find_orders', **dict(args, cursor=second['next_cursor']))
    assert [r['order_id'] for r in final['data']['items']] == [f'page-{i:02}' for i in range(21, 25)]
    assert final['data']['total'] == 23 and final['next_cursor'] is None
    with pytest.raises(BusinessError) as error:
        read(svc, 'find_orders', query='page-', status='all', cursor=first['next_cursor'])
    assert error.value.code == 'invalid_cursor'


@pytest.mark.parametrize('scopes', [[], [{'scope_type': 'new', 'scope_id': '*'}], [{'scope_id': '*'}]])
def test_coupon_invalid_frozen_scope_is_not_silently_filtered(svc, scopes):
    now = utcnow()
    with svc.store.transaction(write=True) as tx:
        template = tx.one("SELECT * FROM coupon_templates WHERE coupon_id='c'")
        tx.execute("INSERT INTO user_coupons(user_coupon_id,coupon_id,user_id,status,claimed_at,expires_at) VALUES ('c','c','alice','available',%s,%s)",
            (now, now + timedelta(days=1)))
        tx.execute("INSERT INTO coupon_claim_rules(user_coupon_id,rules_snapshot,captured_at) VALUES ('c',%s,%s)",
            (dumps(dict(template=template, scopes=scopes)), now))
    with pytest.raises(BusinessError) as error:
        read(svc, 'find_my_coupons', subject_ref=ref('sku', 's1'))
    assert error.value.code == 'insufficient_data'


def test_invoice_documents_are_batched_bounded_and_private(svc, monkeypatch):
    now = utcnow()
    with svc.store.transaction(write=True) as tx:
        for i in range(2):
            tx.execute('''INSERT INTO invoice_requests(invoice_id,platform_code,order_id,user_id,invoice_type,title_ciphertext,
                key_version,status,request_id,created_at) VALUES (%s,'shop','paid','alice','personal','PRIVATE',1,'issued',%s,%s)''', (f'v{i}', f'v{i}', now))
        bulk(tx, '''INSERT INTO issued_invoices(issued_invoice_id,invoice_id,invoice_number,status,document_key,provider,issued_at,updated_at)
            VALUES (%s,'v0',%s,'issued','PRIVATE-KEY','test',%s,%s)''', [(f'd{i}', f'n{i}', now, now) for i in range(9)])
    calls = monitor(monkeypatch)
    result = read(svc, 'get_invoices')
    assert len(calls) == 3
    row = result['data']['items'][0]
    assert row['issued_invoices_total'] == 9 and row['issued_invoices_truncated']
    assert len(row['issued_invoices']) == 5 and 'PRIVATE' not in dumps(result)
    other = CommerceService(svc.store, 'bob', scope=svc.scope, secret=svc.secret)
    with pytest.raises(BusinessError) as error:
        read(other, 'get_invoices', target_ref=ref('invoice', 'v0'))
    assert error.value.code == 'not_found'


def test_legacy_replacement_links_do_not_multiply_or_count_as_original_delivery(svc):
    now = utcnow(); order_ref = ref('order', 'shop', 'paid')
    with svc.store.transaction(write=True) as tx:
        for key, quantity in [('original', 1), ('replacement', 2)]:
            tx.execute("INSERT INTO shipments(shipment_id,platform_code,order_id,status,shipped_at,delivered_at,created_at) VALUES (%s,'shop','paid','delivered',%s,%s,%s)", (key, now, now, now))
            tx.execute("INSERT INTO shipment_items(shipment_id,order_item_id,quantity) VALUES (%s,'i-paid',%s)", (key, quantity))
        for i in range(6):
            tx.execute("""INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,created_at,updated_at)
                VALUES (%s,'shop','paid','alice','exchange','completed','test',%s,%s)""", (f'a{i}', now, now))
            tx.execute("""INSERT INTO service_work_orders(work_order_id,after_sale_id,kind,status,details,source,updated_at)
                VALUES (%s,%s,'exchange','completed',%s,'test',%s)""", (f'w{i}', f'a{i}', dumps(dict(replacement_shipment_id='replacement')), now))
    item = read(svc, 'get_order_items', order_ref=order_ref)['data']['items'][0]
    assert item['recorded_shipped'] == item['recorded_delivered'] == 1
    assert item['recorded_replacement_shipped'] == 2
    assert item['fulfillment_status'] == 'partially_received'
    data = read(svc, 'get_logistics', target_ref=order_ref)['data']
    assert data['quantity_totals']['recorded_shipped'] == 1
    assert data['quantity_totals']['recorded_replacement_shipped'] == 2
    assert data['items'][1]['replacement']['replacement_shipment_id'] == 'replacement'
