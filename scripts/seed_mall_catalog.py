"""Import a pinned mall example catalog into simulated competing shops.

Dry run by default. --apply inserts once, never overwrites existing seed rows.
Reads ORDER_DB_DSN from the environment or .env; SQL from upstream is NEVER executed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
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
from scripts.migrate_mysql_business_schema import _apply_005

URL = 'https://raw.githubusercontent.com/macrozheng/mall/master/document/sql/mall.sql'
SHA256 = 'fd68990fc1de8ebcb8e968f45a0a7cb4072a3dd2a54aaa4b278b72c4546e4488'
NOTE = '模拟店铺测试商品；规格沿用 mall 示例，未经品牌核实，报价和库存为模拟值。'
NAMES = {
    '26': '华为 HUAWEI P20', '27': '小米8', '28': '小米 红米5A',
    '29': 'Apple iPhone 8 Plus', '30': 'HLA海澜之家简约动物印花短袖T恤',
    '31': 'HLA海澜之家蓝灰花纹圆领针织布短袖T恤', '32': 'HLA海澜之家短袖T恤男基础款',
    '33': '小米电视4A', '34': '小米电视4A 大屏系列',
    '35': '耐克 NIKE ROSHE RUN 511881', '36': '耐克 NIKE AIR MAX 90 ESSENTIAL AJ1285',
    '37': 'Apple iPhone 14 (A2884)', '38': 'Apple iPad 10.9英寸 2022年款',
    '39': '小米 / Redmi Pro 锐龙版笔记本（多型号可选）', '40': '小米12 Pro 天玑版',
    '41': 'Redmi K50', '42': 'HUAWEI Mate 50', '43': '万和 Vanward 燃气热水器',
    '44': '三星 SAMSUNG M.2 NVMe SSD固态硬盘', '45': 'OPPO Reno8',
}


def parse_rows(sql, table):
    """Decode this pinned dump's single-row INSERT literals without evaluating SQL."""
    ddl = re.search(r'CREATE TABLE `' + table + r'`\s*\((.*?)\) ENGINE', sql, re.S)
    columns = re.findall(r'^\s*`(\w+)`', ddl[1], re.M)
    result = []
    for match in re.finditer(r'INSERT INTO `' + table + r'` VALUES \(', sql):
        values, buf = [], []
        quoted = was_string = False
        i = match.end()
        while i < len(sql):
            c = sql[i]
            if quoted:
                if c == '\\':
                    i += 1
                    buf.append({'n': '\n', 'r': '\r', 't': '\t', '0': '\0'}.get(sql[i], sql[i]))
                elif c == "'":
                    if sql[i + 1] == "'":
                        buf.append(c)
                        i += 1
                    else:
                        quoted = False
                else:
                    buf.append(c)
            elif c == "'":
                quoted = was_string = True
            elif c in ',)':
                value = ''.join(buf)
                values.append(value if was_string else None if value.strip() == 'NULL' else value.strip())
                buf, was_string = [], False
                if c == ')':
                    break
            elif not c.isspace() or buf:
                buf.append(c)
            i += 1
        if len(values) != len(columns):
            raise ValueError(f'Unexpected column count in {table}')
        result.append(dict(zip(columns, values)))
    return result


def build_catalog(sql):
    if hashlib.sha256(sql.encode()).hexdigest() != SHA256:
        raise ValueError('Upstream dump changed; inspect the new source before importing')
    products = [p for p in parse_rows(sql, 'pms_product')
                if p['delete_status'] == '0' and p['publish_status'] == '1']
    skus = parse_rows(sql, 'pms_sku_stock')
    brands = {r['id']: r['name'] for r in parse_rows(sql, 'pms_brand')}
    categories = {r['id']: r['name'] for r in parse_rows(sql, 'pms_product_category')}
    if len(products) != 20 or len(skus) != 96 or set(NAMES) != {p['id'] for p in products}:
        raise ValueError('Unexpected source catalog coverage')
    shops, offers, variants, inventories = {}, [], [], []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    groups = {'digital': ('数码', '数码产品'), 'fashion': ('服饰', '服饰鞋靴'), 'home': ('家电', '家用电器')}
    for p in products:
        pid, brand = p['id'], brands[p['brand_id']]
        category = categories[p['product_category_id']]
        group = 'fashion' if category in {'T恤', '男鞋'} else 'home' if category in {'电视', '厨卫大电'} else 'digital'
        label, shop_category = groups[group]
        shop_ids = [f"mall_demo_brand_{p['brand_id']}", f'mall_demo_{group}_a', f'mall_demo_{group}_b']
        flagship_category = '数码家电' if brand == '小米' else shop_category
        shops[shop_ids[0]] = (shop_ids[0], f'{brand}旗舰店（模拟）', flagship_category,
            '模拟品牌旗舰店，仅供开发测试；不代表品牌官方开设或已获得品牌授权。', 'active', now, now, 'flagship')
        for sid, name in zip(shop_ids[1:], [f'优选{label}专营店（模拟）', f'惠购{label}专营店（模拟）']):
            shops[sid] = (sid, name, shop_category, '模拟多品牌专营店，报价和库存均为测试数据。', 'active', now, now, 'specialty')
        source_skus = [s for s in skus if s['product_id'] == pid]
        if not source_skus:
            raise ValueError(f'Product {pid} has no SKU')
        for tier, sid in enumerate(shop_ids):
            offer_id = f'{sid}_p{pid}'
            offer_variants = []
            offer_stock = 0
            for s in source_skus:
                attrs = json.loads(s['sp_data'])
                specs = {a['key']: a['value'] for a in attrs}
                if len(specs) != len(attrs) or not specs:
                    raise ValueError(f"Invalid SKU attributes: {s['id']}")
                # These source fields contain model names, not colors.
                if pid in {'39', '44'}:
                    specs['型号'] = specs.pop('颜色')
                if pid == '44':
                    specs['容量'] = specs.pop('版本')
                price = (Decimal(s['price']) * [Decimal('1'), Decimal('.97'), Decimal('.95')][tier]).quantize(Decimal('.01'))
                if price <= 0:
                    raise ValueError('Invalid price')
                # Deterministic test stock; do not inherit stale/negative lock_stock.
                qty = 20 + ((int(s['id']) * 7 + tier * 13) % 81)
                sku_id = f"{sid}_s{s['id']}"
                row = (sku_id, offer_id, f"DEMO-{sid.removeprefix('mall_demo_')}-{s['sku_code']}",
                    json.dumps(specs, ensure_ascii=False), price, 'CNY', 'active', now, now)
                variants.append(row)
                offer_variants.append(row)
                inventories.append((sku_id, qty, 0, 0, now))
                offer_stock += qty
            facts = {'品牌': brand, '款式名称': NAMES[pid], 'catalog_model_key': f'mall-example-{pid}',
                     'is_demo': True, 'specification_verification': 'source_example_unverified',
                     'image_url': p['pic']}
            offers.append((offer_id, sid, NAMES[pid], category, min(r[4] for r in offer_variants),
                           offer_stock, NOTE, json.dumps(facts, ensure_ascii=False)))
    if (len(shops), len(offers), len(variants)) != (14, 60, 288):
        raise ValueError('Unexpected generated catalog size')
    return list(shops.values()), offers, variants, inventories


def apply(dsn, catalog):
    conn = pymysql.connect(**parse_mysql_dsn(dsn), charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if cur.fetchone()['acquired'] != 1:
                raise TimeoutError('Schema/seed lock busy')
            cur.execute("SELECT version FROM schema_migrations WHERE version='001_business_schema'")
            if not cur.fetchone():
                raise ValueError('Apply the baseline business schema first')
            # Refuse partial or previous seeds rather than resetting live inventory/prices.
            for table in ['merchants', 'products', 'product_skus', 'sku_inventory']:
                key = {'merchants': 'merchant_id', 'products': 'product_id'}.get(table, 'sku_id')
                cur.execute(f"SELECT COUNT(*) n FROM {table} WHERE LEFT({key},10)='mall_demo_'")
                if cur.fetchone()['n']:
                    raise ValueError('Seed IDs already exist; nothing overwritten')
        _apply_005(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO schema_migrations VALUES ('005_merchant_store_type',UTC_TIMESTAMP(6))")
        conn.commit()
        conn.begin()
        with conn.cursor() as cur:
            cur.executemany('INSERT INTO merchants (merchant_id,name,category,description,status,created_at,updated_at,store_type) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', catalog[0])
            cur.executemany('INSERT INTO products (product_id,merchant_id,name,category,price,stock,description,specs) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', catalog[1])
            cur.executemany('INSERT INTO product_skus (sku_id,product_id,sku_code,spec_values,list_price,currency,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)', catalog[2])
            cur.executemany('INSERT INTO sku_inventory (sku_id,available_qty,reserved_qty,version,updated_at) VALUES (%s,%s,%s,%s,%s)', catalog[3])
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
    with urlopen(URL, timeout=30) as response:
        sql = response.read().decode('utf-8-sig')
    catalog = build_catalog(sql)
    print(json.dumps({'mode': 'apply' if args.apply else 'dry_run',
        'counts': dict(zip(['merchants', 'products', 'product_skus', 'sku_inventory'], map(len, catalog))),
        'store_types': dict(Counter(r[-1] for r in catalog[0])),
        'product_categories': dict(Counter(r[3] for r in catalog[1]))}, ensure_ascii=False))
    if args.apply:
        dsn = os.environ.get('ORDER_DB_DSN') or dotenv_values(ROOT / '.env').get('ORDER_DB_DSN')
        if not dsn:
            parser.error('Set ORDER_DB_DSN or configure .env')
        apply(dsn, catalog)
        print('Catalog committed. Users unchanged.')


if __name__ == '__main__':
    main()
