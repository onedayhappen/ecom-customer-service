# 文件说明：将 litemall 示例商品转换为模拟商家目录并按参数导入。
"""Append litemall example goods to three simulated shops per top-level category.

Default: inspect only. --apply: insert once, without overwriting existing data.
No source SQL is executed. No source-tracking tables are created.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
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

BASE = 'https://raw.githubusercontent.com/linlinjava/litemall/master/litemall-db/sql/'
HASHES = {
    'litemall_table.sql': '86b5756057e17ae5e234271deb6cb58b15e36e0c9e04c5d83cb5a500385da170',
    'litemall_data.sql': '2eff474b469a7457cf009b44daaebfd0dc4e4a3130dae2bdc559ac796a8c48df',
}
PREFIX = 'litemall_demo_'
NOTE = '模拟店铺；商品资料来自 litemall 示例，规格未经品牌核实，价格与库存为测试数据。'


def parse_rows(ddl, data, table):
    """Parse multi-row INSERT literal tuples, including escaped strings and JSON."""
    match = re.search(r'CREATE TABLE `' + table + r'`\s*\((.*?)\) ENGINE', ddl, re.S)
    if not match:
        raise ValueError(f'Missing DDL: {table}')
    columns = re.findall(r'^\s*`(\w+)`', match[1], re.M)
    rows = []
    for insert in re.finditer(r'INSERT INTO `' + table + r'` VALUES\s*', data):
        i = insert.end()
        while True:
            while data[i].isspace():
                i += 1
            if data[i] != '(':
                raise ValueError(f'Expected tuple in {table}')
            i += 1
            values, buf = [], []
            quoted = was_string = False
            while i < len(data):
                c = data[i]
                if quoted:
                    if c == '\\':
                        i += 1
                        buf.append({'n': '\n', 'r': '\r', 't': '\t', '0': '\0', 'b': '\b', 'Z': '\x1a'}.get(data[i], data[i]))
                    elif c == "'":
                        if data[i + 1] == "'":
                            buf.append(c)
                            i += 1
                        else:
                            quoted = False
                    else:
                        buf.append(c)
                elif c == "'":
                    quoted = was_string = True
                elif c in ',)':
                    token = ''.join(buf)
                    values.append(token if was_string else None if token.strip() == 'NULL' else token.strip())
                    buf, was_string = [], False
                    if c == ')':
                        i += 1
                        break
                elif not c.isspace() or buf:
                    buf.append(c)
                i += 1
            if len(values) != len(columns):
                raise ValueError(f'Column mismatch in {table}')
            rows.append(dict(zip(columns, values)))
            while data[i].isspace():
                i += 1
            if data[i] == ';':
                break
            if data[i] != ',':
                raise ValueError(f'Expected tuple separator in {table}')
            i += 1
    if not rows:
        raise ValueError(f'No rows parsed: {table}')
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError(f'Duplicate source IDs: {table}')
    return rows


def load_source():
    files = {}
    for filename, expected in HASHES.items():
        with urlopen(BASE + filename, timeout=30) as response:
            text = response.read().decode('utf-8-sig')
        if hashlib.sha256(text.encode()).hexdigest() != expected:
            raise ValueError(f'Upstream {filename} changed; inspect before importing')
        files[filename] = text
    return {table: parse_rows(files['litemall_table.sql'], files['litemall_data.sql'], table)
            for table in ['litemall_goods', 'litemall_goods_product', 'litemall_goods_specification',
                          'litemall_goods_attribute', 'litemall_category', 'litemall_brand']}


def build_catalog(source):
    categories = {r['id']: r for r in source['litemall_category'] if r['deleted'] == '0'}
    brands = {r['id']: r['name'] for r in source['litemall_brand'] if r['deleted'] == '0'}
    sku_map, spec_map, attrs = defaultdict(list), defaultdict(list), defaultdict(list)
    for table, dest in [('litemall_goods_product', sku_map), ('litemall_goods_specification', spec_map),
                        ('litemall_goods_attribute', attrs)]:
        for row in source[table]:
            if row['deleted'] == '0':
                dest[row['goods_id']].append(row)
    shops, products, skus, inventories = {}, [], [], []
    skipped, accepted, category_counts = [], [], Counter()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for g in source['litemall_goods']:
        gid = g['id']
        if g['deleted'] != '0' or g['is_on_sale'] != '1':
            skipped.append({'id': gid, 'reason': 'deleted_or_off_sale'})
            continue
        try:
            leaf = categories[g['category_id']]
            path, category = [], leaf
            while True:
                if category['id'] in {r['id'] for r in path}:
                    raise ValueError('category_cycle')
                path.append(category)
                if category['pid'] == '0':
                    break
                category = categories[category['pid']]
            top = path[-1]
            if not sku_map[gid]:
                raise ValueError('missing_skus')
            decoded_skus, seen = [], set()
            for s in sku_map[gid]:
                values = json.loads(s['specifications'])
                if not isinstance(values, list) or not values:
                    raise ValueError('invalid_specifications')
                specs = {}
                for value in values:
                    names = {r['specification'] for r in spec_map[gid] if r['value'] == value}
                    if len(names) != 1:
                        raise ValueError('ambiguous_specification_name')
                    name = next(iter(names))
                    if name in specs:
                        raise ValueError('duplicate_specification_name')
                    specs[name] = value
                spec_json = json.dumps(specs, ensure_ascii=False, sort_keys=True)
                if spec_json in seen or Decimal(s['price']) <= 0:
                    raise ValueError('duplicate_specs_or_invalid_price')
                seen.add(spec_json)
                decoded_skus.append((s, spec_json))
        except (KeyError, ValueError) as exc:
            skipped.append({'id': gid, 'reason': str(exc)})
            continue
        parameters = defaultdict(list)
        for a in attrs[gid]:
            if a['value'] not in parameters[a['attribute']]:
                parameters[a['attribute']].append(a['value'])
        facts = {'catalog_model_key': f'litemall-example-{gid}', 'is_demo': True,
                 'specification_verification': 'source_example_unverified',
                 '款式名称': g['name'], '类目路径': [r['name'] for r in reversed(path)],
                 '参数': {k: v[0] if len(v) == 1 else v for k, v in parameters.items()},
                 '单位': g['unit'], 'image_url': g['pic_url'], 'gallery': json.loads(g['gallery'] or '[]')}
        # Manufacturer labels are not verified product brands or merchant authorization.
        if g['brand_id'] in brands:
            facts['源资料制造商标签'] = brands[g['brand_id']]
        accepted.append(gid)
        category_counts[top['name']] += 1
        for tier, shop_name in enumerate(['拾光', '宜选', '悦享']):
            mid = f'{PREFIX}{top["id"]}_{tier}'
            shops[mid] = (mid, f'{shop_name}{top["name"]}专营店（模拟）', top['name'],
                          '模拟类目专营店；不代表资料原商家，报价和库存均为测试数据。', 'active', now, now, 'specialty')
            pid = f'{mid}_g{gid}'
            prices, stock = [], 0
            for s, spec_json in decoded_skus:
                sid = f'{mid}_s{s["id"]}'
                price = (Decimal(s['price']) * [Decimal('1'), Decimal('.97'), Decimal('1.02')][tier]).quantize(Decimal('.01'))
                qty = (int(s['id']) * 7 + tier * 31) % 121
                skus.append((sid, pid, f'LT-{top["id"]}-{tier}-{s["id"]}', spec_json, price, 'CNY', 'active', now, now))
                inventories.append((sid, qty, 0, 0, now))
                prices.append(price)
                stock += qty
            description = NOTE + ' ' + ' / '.join(facts['类目路径']) + '。' + (g['brief'] or '')
            products.append((pid, mid, g['name'], leaf['name'], min(prices), stock, description[:512],
                             json.dumps(facts, ensure_ascii=False)))
    catalog = (list(shops.values()), products, skus, inventories)
    # Validate relationships and complete three-shop groups before opening a write transaction.
    pmap = {p[0]: p for p in products}
    groups = defaultdict(list)
    for s in skus:
        p = pmap[s[1]]
        groups[(json.loads(p[7])['catalog_model_key'], s[3])].append((p[1], s[4]))
    if not groups or any(len(v) != 3 or len({r[0] for r in v}) != 3 for v in groups.values()):
        raise ValueError('Incomplete comparison groups')
    if any(len(p[2]) > 128 or len(p[3]) > 32 for p in products):
        raise ValueError('Source text exceeds destination field length')
    report = {'source_counts': {k: len(v) for k, v in source.items()}, 'accepted_goods': len(accepted),
              'comparison_groups': len(groups), 'category_goods': dict(category_counts), 'skipped': skipped,
              'insert_counts': dict(zip(['merchants', 'products', 'product_skus', 'sku_inventory'], map(len, catalog)))}
    return catalog, report


def apply(dsn, catalog):
    conn = pymysql.connect(**parse_mysql_dsn(dsn), charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if cur.fetchone()['acquired'] != 1:
                raise TimeoutError('Schema/seed lock busy')
            cur.execute("SELECT store_type FROM merchants LIMIT 0")
            for table in ['merchants', 'products', 'product_skus', 'sku_inventory']:
                key = {'merchants': 'merchant_id', 'products': 'product_id'}.get(table, 'sku_id')
                cur.execute(f'SELECT COUNT(*) n FROM {table} WHERE LEFT({key},%s)=%s', (len(PREFIX), PREFIX))
                if cur.fetchone()['n']:
                    raise ValueError('litemall seed IDs already exist; nothing overwritten')
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
    catalog, report = build_catalog(load_source())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.apply:
        dsn = os.environ.get('ORDER_DB_DSN') or dotenv_values(ROOT / '.env').get('ORDER_DB_DSN')
        if not dsn:
            parser.error('Set ORDER_DB_DSN or configure .env')
        apply(dsn, catalog)
        print('litemall catalog committed.')


if __name__ == '__main__':
    main()
