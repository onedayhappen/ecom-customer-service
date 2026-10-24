"""Create recent local demo orders against the real clock; no external purchases.

Default prints a plan. --apply updates 12 sample timelines and inserts seven
orders per demo user once. All business writes share one transaction.
--additional adds five more orders to each of the eight non-source owners,
without retiming historical orders.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pymysql
from dotenv import dotenv_values
from app.data.business_store import Transaction
from app.data.order_dao import parse_mysql_dsn
from app.services.commerce.common import Cipher, dumps, decoded
from scripts.seed_mall_orders import COLUMNS as BASE_COLUMNS, PLATFORM
from scripts.rename_demo_users import NAMES

PREFIX = 'DEMO-RECENT-'
CASES = [
    ('shoes', 'pending_receipt', '男鞋', 1),
    ('unpaid', 'pending_payment', '手机通讯', 0),
    ('paid', 'paid', 'T恤', 2),
    ('received', 'received', '床品件套', 7),
    ('service', 'after_sale', '锅具', 5),
    ('cancelled', 'cancelled', '糕点', 14),
    ('closed', 'closed', '功能箱包', 30),
]
ADDITIONAL_PREFIX = 'DEMO-EXTRA-'
ADDITIONAL_CASES = [
    ('phone', 'pending_payment', '手机通讯', 0),
    ('shirt', 'paid', 'T恤', 3),
    ('bag', 'pending_receipt', '功能箱包', 2),
    ('bedding', 'received', '床品件套', 9),
    ('cookware', 'received', '锅具', 12),
]
COLUMNS = dict(BASE_COLUMNS)
COLUMNS['orders'] += ',status_before_after_sale'
COLUMNS.update({
    'after_sale_requests': 'after_sale_id,platform_code,order_id,user_id,kind,status,reason,requested_amount,created_at,updated_at,closed_at',
    'after_sale_items': 'after_sale_id,order_item_id,quantity,requested_amount',
    'after_sale_events': 'event_id,after_sale_id,operation_id,kind,source,payload,created_at',
})


def build_plan(tx, cipher, now, *, additional=False):
    # orders.created_at is DATETIME(0): align all scenario timestamps to seconds
    # so MySQL rounding cannot place created_at after a microsecond updated_at.
    now = now.replace(microsecond=0)
    local = now.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    prefix = ADDITIONAL_PREFIX if additional else PREFIX
    cases = ADDITIONAL_CASES if additional else CASES
    numbers = [n for n in NAMES if not additional or n not in (1, 11)]
    users = tx.all('SELECT user_id,username FROM users ORDER BY user_id')
    users = sorted([u for u in users if u['user_id'] in {f'user_{n}' for n in numbers}], key=lambda u: int(u['user_id'].split('_')[-1]))
    if len(users) != len(numbers):
        raise ValueError('Expected all selected renamed demo users')
    if tx.one('SELECT order_id FROM orders WHERE LEFT(order_id,%s)=%s LIMIT 1', (len(prefix), prefix)):
        raise ValueError('Recent scenario orders already exist; refusing duplicate import or date refresh')
    catalog = tx.all('''SELECT p.product_id,p.merchant_id,p.name,p.category,s.sku_id,s.spec_values,s.list_price,s.currency
        FROM products p JOIN product_skus s ON s.product_id=p.product_id
        JOIN merchants m ON m.merchant_id=p.merchant_id JOIN sku_inventory i ON i.sku_id=s.sku_id
        WHERE m.status='active' AND s.status='active' AND i.available_qty>=2 AND s.list_price>0
        ORDER BY p.product_id,s.sku_id''')
    pools = {category: [s for s in catalog if s['category'] == category] for _, _, category, _ in cases}
    if any(not pool for pool in pools.values()):
        raise ValueError('A required scenario category has no sellable SKU')
    rows, changes, retimed = defaultdict(list), [], []
    # Shift a limited sample of historical orders. Rebuild event intervals to
    # avoid moving old multi-month delivery gaps into the future.
    for uid in (() if additional else ('user_1', 'user_11')):
        old_orders = tx.all('''SELECT o.* FROM orders o WHERE o.user_id=%s AND o.platform_code=%s
            AND o.status IN ('paid','pending_receipt','received','closed')
            AND NOT EXISTS(SELECT 1 FROM after_sale_requests a WHERE a.platform_code=o.platform_code AND a.order_id=o.order_id)
            AND NOT EXISTS(SELECT 1 FROM order_events e WHERE e.platform_code=o.platform_code AND e.order_id=o.order_id AND e.kind<>'imported')
            ORDER BY o.created_at DESC,o.order_id DESC LIMIT 6''', (uid, PLATFORM))
        if len(old_orders) != 6:
            raise ValueError('Expected six unchanged source orders for each original owner')
        for index, order in enumerate(old_orders):
            key = (order['platform_code'], order['order_id'])
            created = midnight - timedelta(days=10 + index * 2) + timedelta(hours=10)
            paid = created + timedelta(minutes=5) if order['paid_at'] else None
            if order['status'] != 'closed' and paid is None:
                raise ValueError('Historical order lacks expected payment evidence')
            payments = tx.all('SELECT * FROM payment_transactions WHERE platform_code=%s AND order_id=%s', key)
            shipments = tx.all('SELECT * FROM shipments WHERE platform_code=%s AND order_id=%s', key)
            if tx.one('SELECT invoice_id FROM invoice_requests WHERE platform_code=%s AND order_id=%s LIMIT 1', key):
                raise ValueError('Invoice-linked historical order needs a broader time migration')
            before = {'order': {k: order[k] for k in ('created_at','paid_at','updated_at')},
                      'payments': [{k: p[k] for k in ('payment_id','paid_at','created_at')} for p in payments],
                      'shipments': [{k: s[k] for k in ('shipment_id','shipped_at','delivered_at','created_at')} for s in shipments]}
            last = paid or created + timedelta(minutes=30)
            for payment in payments:
                if payment['status'] != 'succeeded' or paid is None:
                    raise ValueError('Unexpected payment state in date-refresh sample')
                changes.append(('UPDATE payment_transactions SET paid_at=%s,created_at=%s WHERE payment_id=%s', (paid, paid, payment['payment_id'])))
            for ship in shipments:
                if tx.one('SELECT event_id FROM shipment_events WHERE shipment_id=%s LIMIT 1', (ship['shipment_id'],)):
                    raise ValueError('Tracked historical shipment must be migrated with its events')
                sent = created + timedelta(days=1)
                delivered = created + timedelta(days=3) if ship['delivered_at'] else None
                changes.append(('UPDATE shipments SET shipped_at=%s,delivered_at=%s,created_at=%s WHERE shipment_id=%s', (sent, delivered, sent, ship['shipment_id'])))
                last = max(last, delivered or sent)
            if last > now:
                raise ValueError('Historical retiming would produce future events')
            changes.append(('UPDATE orders SET created_at=%s,paid_at=%s,updated_at=%s WHERE platform_code=%s AND order_id=%s', (created, paid, last, *key)))
            eid = 'recent_time_' + order['order_id']
            rows['order_events'].append((eid, *key, eid, 'demo_time_adjusted', dumps({'is_demo': True, 'old_times': before,
                'new_created_at': created, 'new_paid_at': paid, 'new_updated_at': last, 'clock': 'real',
                'note': '历史样例重排为近期测试时间，金额和状态保持不变'}), now))
            retimed.append(order['order_id'])
    for index, user in enumerate(users):
        uid, number = user['user_id'], int(user['user_id'].split('_')[-1])
        address = tx.one('SELECT * FROM user_addresses WHERE user_id=%s AND deleted_at IS NULL ORDER BY is_default DESC,address_id LIMIT 1', (uid,))
        if not address:
            aid = 710000000 + number
            if tx.one('SELECT address_id FROM user_addresses WHERE address_id=%s', (aid,)):
                raise ValueError('Demo address ID collision')
            address = {'province_code': '440000', 'city_code': '440300', 'district_code': '440305', 'key_version': cipher.version}
            for field, plain in [('recipient_name', user['username'] + '（测试）'), ('phone', '00000000000'),
                                 ('detail', '模拟收货地址，仅用于本地测试，不可用于真实配送')]:
                address[field + '_ciphertext'] = cipher.seal(plain, f'address:{uid}:{field}')
            rows['user_addresses'].append((aid, uid, address['recipient_name_ciphertext'], address['phone_ciphertext'],
                '440000', '440300', '440305', address['detail_ciphertext'], cipher.version, True, now, now, None))
            rows['address_labels'].append((aid, '本地模拟地址', '广东省', '深圳市', '南山区'))
        for slot, (case, status, category, days) in enumerate(cases):
            sku = pools[category][(index * 3 + slot + (11 if additional else 0)) % len(pools[category])]
            oid = f'{prefix}{number}-{case}'
            iid = 'item_' + oid
            created = max(midnight, now - timedelta(minutes=10 + index)) if status == 'pending_payment' else midnight - timedelta(days=days) + timedelta(hours=10, minutes=index)
            is_paid = status in {'paid','pending_receipt','received','after_sale'}
            paid = created + timedelta(minutes=5) if is_paid else None
            quantity = 2 if status == 'after_sale' else 1
            total = sku['list_price'] * quantity
            sent = created + (timedelta(hours=3) if status == 'pending_receipt' else timedelta(days=1)) if status in {'pending_receipt','received','after_sale'} else None
            received = created + timedelta(days=3) if status in {'received','after_sale'} else None
            requested = created + timedelta(days=4) if status == 'after_sale' else None
            last = requested or received or sent or paid or (created + timedelta(minutes=30) if status in {'cancelled','closed'} else created)
            if not created <= last <= now:
                raise ValueError('Invalid scenario timeline')
            rows['orders'].append((PLATFORM, oid, uid, status, created, sku['merchant_id'], sku['currency'], total,
                Decimal('0'), Decimal('0'), total, total if is_paid else Decimal('0'), paid, last,
                'received' if status == 'after_sale' else None))
            rows['order_items'].append((iid, PLATFORM, oid, sku['sku_id'], sku['product_id'], sku['name'] + '（' + category + '）',
                dumps(decoded(sku['spec_values'])), quantity, sku['list_price'], Decimal('0'), total))
            sealed = [cipher.seal(cipher.open(address[f + '_ciphertext'], f'address:{uid}:{f}', address['key_version']), f'address:{uid}:{f}')
                      for f in ('recipient_name','phone','detail')]
            rows['order_address_snapshots'].append((PLATFORM, oid, sealed[0], sealed[1], address['province_code'], address['city_code'],
                address['district_code'], sealed[2], cipher.version, now))
            eid = 'event_' + oid
            rows['order_events'].append((eid, PLATFORM, oid, eid, 'demo_seeded', dumps({'is_demo': True, 'case': case,
                'generated_against_real_local_date': local.date().isoformat(), 'timezone': 'Asia/Shanghai',
                'inventory_basis': 'historical_test_snapshot_no_live_stock_deduction', 'shipping_fee_basis': 'demo_zero',
                'note': '本地生成测试订单，无真实下单、支付、发货请求'}), now))
            if is_paid:
                rows['payment_transactions'].append(('pay_' + oid, PLATFORM, oid, uid, 'local_demo', None, total, sku['currency'], 'succeeded', paid, paid))
            if sent:
                sid = 'ship_' + oid
                rows['shipments'].append((sid, PLATFORM, oid, 'TEST-' + str(number) + '-' + case, '模拟物流',
                    'delivered' if received else 'shipped', sent, received, sent))
                rows['shipment_items'].append((sid, iid, quantity))
            if requested:
                aid = 'sale_' + oid
                rows['after_sale_requests'].append((aid, PLATFORM, oid, uid, 'refund_only', 'pending_review',
                    '本地测试：购买两件，申请其中一件退款', sku['list_price'], requested, requested, None))
                rows['after_sale_items'].append((aid, iid, 1, sku['list_price']))
                rows['after_sale_events'].append(('sale_event_' + oid, aid, 'sale_submit_' + oid, 'submitted', 'local_demo',
                    dumps({'is_demo': True, 'quantity': 1}), requested))
    report = {'real_local_date': str(local.date()), 'batch_prefix': prefix, 'user_ids': [u['user_id'] for u in users],
              'retimed_order_ids': retimed, 'insert_counts': {k: len(v) for k, v in rows.items()},
              'new_orders_by_status': dict(Counter(r[3] for r in rows['orders'])), 'new_orders_per_user': len(cases)}
    return rows, changes, report


def run(dsn, cipher, *, apply=False, additional=False):
    prefix = ADDITIONAL_PREFIX if additional else PREFIX
    conn = pymysql.connect(**parse_mysql_dsn(dsn), charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if cur.fetchone()['acquired'] != 1:
                raise TimeoutError('Seed lock busy')
        tx = Transaction(conn, write=apply)
        # Protect source ownership, stock snapshot, and timeline selection during planning.
        if apply:
            tx.all('SELECT user_id FROM users ORDER BY user_id FOR UPDATE')
            tx.all('SELECT platform_code,order_id FROM orders ORDER BY platform_code,order_id FOR UPDATE')
        rows, changes, report = build_plan(tx, cipher, datetime.now(timezone.utc).replace(tzinfo=None), additional=additional)
        if not apply:
            conn.rollback()
            return report
        # Existing designed tables required by after-sale query tools; no provenance tables.
        with conn.cursor() as cur:
            for table, filename in [('after_sale_events','003_core_tools.sql'), ('return_authorizations','004_extended_tools.sql')]:
                cur.execute('SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', (table,))
                if not cur.fetchone():
                    text = (ROOT / 'migrations/mysql' / filename).read_text(encoding='utf-8-sig')
                    statement = re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r'\s*\(.*?;', text, re.S)
                    if not statement:
                        raise ValueError('Missing business table DDL')
                    cur.execute(statement[0])
        conn.commit()
        conn.begin()
        # DDL commits release row locks: acquire again and rebuild against current data.
        tx.all('SELECT user_id FROM users ORDER BY user_id FOR UPDATE')
        tx.all('SELECT platform_code,order_id FROM orders ORDER BY platform_code,order_id FOR UPDATE')
        rows, changes, report = build_plan(tx, cipher, datetime.now(timezone.utc).replace(tzinfo=None), additional=additional)
        with conn.cursor() as cur:
            for sql, args in changes:
                cur.execute(sql, args)
            for table, columns in COLUMNS.items():
                if rows.get(table):
                    cur.executemany(f'INSERT INTO {table} ({columns}) VALUES (' + ','.join(['%s'] * len(columns.split(','))) + ')', rows[table])
            cur.execute('''SELECT COUNT(*) n FROM order_items i JOIN orders o ON o.platform_code=i.platform_code AND o.order_id=i.order_id
                JOIN products p ON p.product_id=i.product_id JOIN product_skus s ON s.sku_id=i.sku_id
                WHERE LEFT(o.order_id,%s)=%s AND (p.merchant_id<>o.merchant_id OR s.product_id<>i.product_id OR i.quantity*i.unit_price-i.discount_amount<>i.payable_amount)''', (len(prefix), prefix))
            if cur.fetchone()['n']:
                raise ValueError('Generated order association/amount check failed')
        conn.commit()
        return report
    except BaseException:
        conn.rollback()
        raise
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK('ecom_mysql_business_schema')")
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--additional', action='store_true', help='Add five orders each for the eight non-source owners')
    args = parser.parse_args()
    config = {**dotenv_values(ROOT / '.env'), **os.environ}
    cipher = Cipher(config.get('BUSINESS_DATA_KEYS', '{}'), int(config.get('BUSINESS_DATA_KEY_VERSION', '1')))
    print(dumps(run(config['ORDER_DB_DSN'], cipher, apply=args.apply, additional=args.additional)))


if __name__ == '__main__':
    main()
