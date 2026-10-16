"""Add commerce tables and optional labelled demo evidence; never overwrite orders.

Usage: python scripts/init_commerce_db.py --seed-demo
Uses ORDER_DB_DSN. Existing rows are preserved; repeats insert only missing IDs.
"""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import settings
from app.data.commerce_dao import MySqlCommerceDAO


def demo_records(products, orders, at=None):
    at = at or datetime.now(timezone.utc)
    starts, ends = (at - timedelta(days=1)).isoformat(), (at + timedelta(days=90)).isoformat()
    evidence = {'source': 'mysql_demo', 'observed_at': at.isoformat()}
    records = []
    def add(kind, key, obj, payload, *, user_id='', channel='', region='', timestamp=None):
        records.append({'kind': kind, 'record_id': key, 'object_id': obj, 'user_id': user_id,
                        'platform_code': 'pdd', 'channel': channel, 'region': region,
                        'recorded_at': (timestamp or at).astimezone(timezone.utc).replace(tzinfo=None),
                        'payload': payload})
    skus = [p['product_id'] for p in products]
    for channel in ('pdd', 'demo_other_shop'):
        for p in products:
            price = int(Decimal(str(p['price'])) * 100)
            total = price + (500 if channel == 'demo_other_shop' else 0)
            add('offers', f"demo:{p['product_id']}:{channel}", p['product_id'], {
                **evidence, 'offer_id': f"demo:{p['product_id']}:{channel}",
                'sku_id': p['product_id'], 'specification': p['name'], 'condition': 'new',
                'included_items': '标准包装', 'channel': channel, 'region': '上海', 'quantity': 1,
                'currency': 'CNY', 'goods_minor': price, 'discount_minor': 0,
                'shipping_minor': total - price, 'fees_minor': 0, 'total_minor': total,
                'eligibility': 'eligible', 'eligibility_basis': '公开演示报价，不含个人优惠',
                'valid_until': ends,
            }, channel=channel, region='上海')
        rule = {**evidence, 'rule_id': f'demo-full-reduction:{channel}', 'version': 'demo-v1',
                'channel': channel, 'currency': 'CNY', 'starts_at': starts, 'ends_at': ends,
                'sku_ids': skus, 'minimum_minor': 30000, 'kind': 'fixed', 'amount_minor': 2000,
                'discount_bps': 0, 'cap_minor': None, 'exclusive_group': 'demo-store-event',
                'base': 'original', 'priority': 0}
        add('promotions', rule['rule_id'], '*', rule, channel=channel)
        for region in ('上海', '杭州', '北京'):
            add('delivery', f'demo:{channel}:{region}', '*', {
                **evidence, 'version': 'demo-v1', 'sku_ids': skus,
                'effective_from': starts, 'effective_until': ends, 'deliverable': True,
                'currency': 'CNY', 'fee_mode': 'fixed_plus_unit',
                'base_minor': 0 if channel == 'pdd' else 500, 'per_unit_minor': 0,
                'min_days': 2, 'max_days': 4,
            }, channel=channel, region=region)
    for p in products:
        price = int(Decimal(str(p['price'])) * 100)
        for days, delta in ((30, 1500), (14, 500), (7, 0)):
            timestamp = at - timedelta(days=days)
            add('history', f"demo:{p['product_id']}:d{days}", p['product_id'], {
                'specification': p['name'], 'currency': 'CNY', 'price_type': 'list',
                'point': {'source': 'mysql_demo', 'observed_at': timestamp.isoformat(),
                          'amount_minor': price + delta},
            }, channel='pdd', timestamp=timestamp)
    for identity in sorted({o['user_id'] for o in orders}):
        rule = {**evidence, 'rule_id': 'demo-coupon-rule', 'version': 'demo-v1',
                'channel': 'pdd', 'currency': 'CNY', 'starts_at': starts, 'ends_at': ends,
                'sku_ids': skus, 'minimum_minor': 10000, 'kind': 'fixed', 'amount_minor': 1000,
                'exclusive_group': 'demo-coupon', 'priority': 1}
        add('coupons', 'DEMO-COUPON-10', '*', {**evidence, 'coupon_id': 'DEMO-COUPON-10',
            'status': 'available', 'owned': True, 'rule': rule}, user_id=identity, channel='pdd')
    for order in orders:
        order_id, identity = order['order_id'], order['user_id']
        add('payments', f'demo-payment:{order_id}', order_id, {
            **evidence, 'transaction_id': f'demo-payment:{order_id}', 'order_id': order_id,
            'status': 'paid', 'amount_minor': int(Decimal(str(order['total'])) * 100),
            'currency': 'CNY', 'channel': 'demo_payment', 'is_demo': True,
        }, user_id=identity)
        if order.get('refund_status'):
            add('after_sales', f'demo-refund:{order_id}', order_id, {
                **evidence, 'after_sale_id': f'demo-refund:{order_id}', 'order_id': order_id,
                'kind': 'refund', 'status': order['refund_status'], 'is_demo': True,
                'note': '初始化时的退款状态快照，不表示已到账',
            }, user_id=identity)
    for action, statuses in [('address_change', ['pending']), ('cancel', ['pending']),
                             ('expedite', ['pending', 'shipped']), ('invoice', ['shipped', 'delivered'])]:
        add('policies', action, action, {
            **evidence, 'version': 'demo-v1', 'allowed_statuses': statuses,
            'effective_from': starts, 'effective_until': ends,
        })
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-demo', action='store_true', help='插入明确标注的演示数据，不覆盖已有行')
    args = parser.parse_args()
    if not settings.order_db_dsn:
        parser.error('请配置 ORDER_DB_DSN')
    db = MySqlCommerceDAO(settings.order_db_dsn)
    db.ensure_schema()
    print('10 张 commerce 表已就绪；未修改原订单、商品或物流表。')
    if args.seed_demo:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT product_id,name,price FROM products ORDER BY product_id')
            products = cur.fetchall()
            cur.execute("SELECT order_id,user_id,status,total,refund_status FROM orders WHERE platform_code='pdd'")
            orders = cur.fetchall()
        if not products:
            parser.error('商品表为空，请先准备商品数据')
        count = db.insert_seed(demo_records(products, orders))
        print(f'新增 {count} 条 mysql_demo 记录；已存在记录保持原值。')


if __name__ == '__main__':
    main()
