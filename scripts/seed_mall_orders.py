# 文件说明：转换 mall 示例地址和历史订单，默认预览并按参数执行导入。
"""Import mall example addresses and non-deleted historical orders; dry run by default."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pymysql
from dotenv import dotenv_values
from app.data.order_dao import parse_mysql_dsn
from app.services.commerce.common import Cipher
from scripts.seed_mall_catalog import URL, SHA256, parse_rows

PLATFORM = 'mall_demo'
ADDRESS_BASE = 700000000
REGIONS = {
    ('北京市', '北京城区', '东城区'): ('110000', '110100', '110101'),
    ('广东省', '深圳市', '南山区'): ('440000', '440300', '440305'),
    ('广东省', '深圳市', '福田区'): ('440000', '440300', '440304'),
    ('江苏省', '常州市', '天宁区'): ('320000', '320400', '320402'),
}
STATES = {'0': 'pending_payment', '1': 'paid', '2': 'pending_receipt', '3': 'received', '4': 'closed', '5': 'closed'}


def money(value):
    return Decimal(value or '0').quantize(Decimal('.01'))


def utc(value):
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).replace(tzinfo=None)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def load_source():
    with urlopen(URL, timeout=30) as r:
        sql = r.read().decode('utf-8-sig')
    if hashlib.sha256(sql.encode()).hexdigest() != SHA256:
        raise ValueError('Source changed; review before importing')
    return {t: parse_rows(sql, t) for t in ['ums_member_receive_address', 'oms_order', 'oms_order_item',
                                           'pms_product', 'pms_sku_stock']}


def build_rows(source, cipher):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = defaultdict(list)
    products = {p['id']: p for p in source['pms_product']}
    skus = {s['id']: s for s in source['pms_sku_stock']}
    items = defaultdict(list)
    for i in source['oms_order_item']:
        items[i['order_id']].append(i)
    orders = [o for o in source['oms_order'] if o['delete_status'] == '0']
    if (len(source['ums_member_receive_address']), len(orders)) != (5, 48):
        raise ValueError('Unexpected source coverage')
    defaults = Counter()
    for a in source['ums_member_receive_address']:
        aid, uid = ADDRESS_BASE + int(a['id']), 'user_' + a['member_id']
        codes = REGIONS[(a['province'], a['city'], a['region'])]
        encrypted = [cipher.seal(a[k], f'address:{uid}:{field}') for k, field in
                     [('name', 'recipient_name'), ('phone_number', 'phone'), ('detail_address', 'detail')]]
        default = a['default_status'] == '1'
        defaults[uid] += int(default)
        rows['user_addresses'].append((aid, uid, encrypted[0], encrypted[1], *codes, encrypted[2],
                                      cipher.version, default, now, now, None))
        rows['address_labels'].append((aid, f'mall示例地址{a["id"]}', a['province'], a['city'], a['region']))
    if any(n > 1 for n in defaults.values()):
        raise ValueError('Multiple default source addresses')
    issues, unresolved = [], []
    for o in orders:
        oid, uid = o['order_sn'], 'user_' + o['member_id']
        lines = items[o['id']]
        if not lines or not oid or len(oid) > 40:
            raise ValueError('Invalid source order')
        groups = {'fashion' if products[i['product_id']]['product_category_name'] in {'T恤', '男鞋'} else 'digital' for i in lines}
        if len(groups) != 1:
            raise ValueError('Cross-category order requires an explicit merchant mapping')
        # mall is single-store; map a whole order to one compatible simulated retailer.
        merchant = f'mall_demo_{next(iter(groups))}_a'
        goods, payable, freight = map(money, (o['total_amount'], o['pay_amount'], o['freight_amount']))
        discount = sum(money(o[k]) for k in ['promotion_amount', 'integration_amount', 'coupon_amount', 'discount_amount'])
        paid_at = utc(o['payment_time'])
        status = STATES[o['status']]
        if (o['status'] in {'1', '2', '3'}) != bool(paid_at):
            raise ValueError('Payment state conflicts with source timestamp')
        dates = [utc(o[k]) for k in ['create_time', 'payment_time', 'delivery_time', 'receive_time', 'modify_time'] if o[k]]
        rows['orders'].append((PLATFORM, oid, uid, status, utc(o['create_time']), merchant, 'CNY', goods,
                               discount, freight, payable, payable if paid_at else Decimal('0'), paid_at, max(dates)))
        codes = REGIONS[(o['receiver_province'], o['receiver_city'], o['receiver_region'])]
        sealed = [cipher.seal(o[k], f'address:{uid}:{field}') for k, field in
                  [('receiver_name', 'recipient_name'), ('receiver_phone', 'phone'), ('receiver_detail_address', 'detail')]]
        rows['order_address_snapshots'].append((PLATFORM, oid, sealed[0], sealed[1], *codes, sealed[2], cipher.version, now))
        line_gross = line_net = Decimal('0')
        source_line_discounts = []
        missing_ids = []
        for i in lines:
            iid = 'mall_order_item_' + i['id']
            pid = f'{merchant}_p{i["product_id"]}'
            sid = f'{merchant}_s{i["product_sku_id"]}' if i['product_sku_id'] in skus else None
            if sid and skus[i['product_sku_id']]['product_id'] != i['product_id']:
                raise ValueError('Source SKU belongs to a different product')
            if not sid:
                missing_ids.append(i['product_sku_id'])
                unresolved.append(iid)
            spec_list = json.loads(i['product_attr']) if i['product_attr'] else None
            spec = {x['key']: x['value'] for x in spec_list} if spec_list else None
            quantity = int(i['product_quantity'])
            price, net = money(i['product_price']), money(i['real_amount'])
            if quantity <= 0 or min(price, net) < 0 or net > price:
                raise ValueError('Invalid source line amounts')
            line_gross += price * quantity
            line_net += net * quantity
            rows['order_items'].append((iid, PLATFORM, oid, sid, pid, i['product_name'],
                                       dump(spec) if spec is not None else None, quantity, price,
                                       (price - net) * quantity, net * quantity))
            source_line_discounts.append({'item_id': iid, 'source_sku_id': i['product_sku_id'],
                'promotion_per_unit': i['promotion_amount'], 'coupon_per_unit': i['coupon_amount'],
                'points_per_unit': i['integration_amount'], 'real_amount_per_unit': i['real_amount']})
        differences = {k: str(v) for k, v in {
            'items_gross_minus_header_goods': line_gross - goods,
            'items_net_plus_freight_minus_payable': line_net + freight - payable,
            'header_goods_plus_freight_minus_discount_minus_payable': goods + freight - discount - payable,
        }.items() if v != 0}
        if differences:
            issues.append({'source_order_id': o['id'], 'order_id': oid, 'differences': differences})
        payload = {'dataset': 'mall_example', 'source_order_id': o['id'], 'source_status': o['status'],
            'merchant_mapping': 'simulated_category_retailer', 'missing_source_sku_ids': missing_ids,
            'amount_differences': differences, 'source_discount_breakdown': {k: o[k] for k in
                ['promotion_amount', 'integration_amount', 'coupon_amount', 'discount_amount']},
            'source_item_discounts': source_line_discounts, 'source_timezone': 'Asia/Shanghai',
            'payment_record': 'derived_from_source_order_not_provider_receipt',
            'shipment_record': 'order_level_dispatch_not_independent_parcel_manifest'}
        rows['order_events'].append(('mall_import_' + o['id'], PLATFORM, oid, 'mall_import_' + o['id'], 'imported', dump(payload), now))
        if paid_at:
            provider = {'1': 'mall_demo_alipay', '2': 'mall_demo_wechat'}.get(o['pay_type'], 'mall_demo_unknown')
            rows['payment_transactions'].append(('mall_payment_' + o['id'], PLATFORM, oid, uid, provider,
                None, payable, 'CNY', 'succeeded', paid_at, paid_at))
        if o['delivery_time']:
            shipment = 'mall_shipment_' + o['id']
            delivered = utc(o['receive_time'])
            rows['shipments'].append((shipment, PLATFORM, oid, o['delivery_sn'] or None, o['delivery_company'] or None,
                'delivered' if delivered else 'shipped', utc(o['delivery_time']), delivered, utc(o['delivery_time'])))
            for i in lines:
                rows['shipment_items'].append((shipment, 'mall_order_item_' + i['id'], int(i['product_quantity'])))
    report = {'source_orders': len(source['oms_order']), 'excluded_deleted_orders': len(source['oms_order']) - len(orders),
        'orders_by_user': dict(Counter('user_' + o['member_id'] for o in orders)),
        'orders_by_status': dict(Counter(STATES[o['status']] for o in orders)),
        'insert_counts': {k: len(v) for k, v in rows.items()}, 'unresolved_sku_items': unresolved,
        'amount_differences': issues}
    return dict(rows), report


COLUMNS = {
    'user_addresses': 'address_id,user_id,recipient_name_ciphertext,phone_ciphertext,province_code,city_code,district_code,detail_ciphertext,key_version,is_default,created_at,updated_at,deleted_at',
    'address_labels': 'address_id,label,province_name,city_name,district_name',
    'orders': 'platform_code,order_id,user_id,status,created_at,merchant_id,currency,goods_amount,discount_amount,shipping_amount,payable_amount,paid_amount,paid_at,updated_at',
    'order_address_snapshots': 'platform_code,order_id,recipient_name_ciphertext,phone_ciphertext,province_code,city_code,district_code,detail_ciphertext,key_version,captured_at',
    'order_items': 'order_item_id,platform_code,order_id,sku_id,product_id,title_snapshot,spec_snapshot,quantity,unit_price,discount_amount,payable_amount',
    'order_events': 'event_id,platform_code,order_id,operation_id,kind,payload,created_at',
    'payment_transactions': 'payment_id,platform_code,order_id,user_id,provider,provider_txn_id,amount,currency,status,paid_at,created_at',
    'shipments': 'shipment_id,platform_code,order_id,tracking_number,carrier,status,shipped_at,delivered_at,created_at',
    'shipment_items': 'shipment_id,order_item_id,quantity',
}


def apply(dsn, rows):
    conn = pymysql.connect(**parse_mysql_dsn(dsn), charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if cur.fetchone()['acquired'] != 1:
                raise TimeoutError('Schema/seed lock busy')
            cur.execute('SELECT 1 FROM orders WHERE platform_code=%s LIMIT 1', (PLATFORM,))
            if cur.fetchone():
                raise ValueError('mall order seed already exists; nothing overwritten')
            for a in rows['user_addresses']:
                cur.execute('SELECT 1 FROM user_addresses WHERE address_id=%s OR (user_id=%s AND is_default=1 AND deleted_at IS NULL)', (a[0], a[1]))
                if cur.fetchone():
                    raise ValueError('Existing address/default conflicts with seed; nothing overwritten')
            cur.execute('SELECT user_id FROM users')
            users = {r['user_id'] for r in cur.fetchall()}
            if not {r[2] for r in rows['orders']} <= users or not {r[1] for r in rows['user_addresses']} <= users:
                raise ValueError('Import mall users first')
            cur.execute('SELECT product_id,merchant_id FROM products')
            product_merchants = {r['product_id']: r['merchant_id'] for r in cur.fetchall()}
            cur.execute('SELECT sku_id,product_id FROM product_skus')
            sku_products = {r['sku_id']: r['product_id'] for r in cur.fetchall()}
            owners = {r[1]: r[5] for r in rows['orders']}
            for i in rows['order_items']:
                if product_merchants.get(i[4]) != owners[i[2]] or i[3] and sku_products.get(i[3]) != i[4]:
                    raise ValueError('Missing or inconsistent merchant/product/SKU mapping')
            # Only these existing designed business tables are needed. Do not mark
            # whole migrations 003/004 applied while their other tables remain pending.
            for table, filename in [('address_labels', '004_extended_tools.sql'), ('order_events', '003_core_tools.sql'),
                                    ('service_work_orders', '004_extended_tools.sql')]:
                sql = (ROOT / 'migrations/mysql' / filename).read_text(encoding='utf-8-sig')
                ddl = re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r'\s*\(.*?;', sql, re.S)
                if not ddl:
                    raise ValueError('Business DDL not found')
                cur.execute(ddl[0])
        conn.commit()  # MySQL DDL boundary; all following business inserts are atomic.
        conn.begin()
        with conn.cursor() as cur:
            for table, columns in COLUMNS.items():
                values = rows.get(table, [])
                if values:
                    cur.executemany(f'INSERT INTO {table} ({columns}) VALUES (' + ','.join(['%s'] * len(columns.split(','))) + ')', values)
        conn.commit()
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
    args = parser.parse_args()
    config = {**dotenv_values(ROOT / '.env'), **os.environ}
    keys = config.get('BUSINESS_DATA_KEYS', '{}')
    version = int(config.get('BUSINESS_DATA_KEY_VERSION', '1'))
    if args.apply:
        cipher = Cipher(keys, version)
        cipher.key(version)  # Fail before any database mutation when key is missing.
    else:
        # Ephemeral dry-run key, never persisted or used for database writes.
        import base64
        cipher = Cipher({str(version): base64.urlsafe_b64encode(os.urandom(32)).decode()}, version)
    rows, report = build_rows(load_source(), cipher)
    print(dump(report))
    if args.apply:
        dsn = config.get('ORDER_DB_DSN')
        if not dsn:
            parser.error('Configure ORDER_DB_DSN')
        apply(dsn, rows)
        print('mall addresses and orders committed.')


if __name__ == '__main__':
    main()
