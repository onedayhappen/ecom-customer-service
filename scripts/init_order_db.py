"""初始化业务库（本地 MySQL）：建库 → 建表 → seed 电商平台数据（幂等）。

覆盖三张业务表：orders（订单）、logistics（物流轨迹）、products（商品目录）。

用法:
    python scripts/init_order_db.py
    python scripts/init_order_db.py --dsn mysql://root:123456@127.0.0.1:3306/ecom_agent_orders

不传 --dsn 时读取 .env 的 ORDER_DB_DSN；两者皆空则直接退出。
- 建库与建表使用 CREATE IF NOT EXISTS，可重复执行；
- seed 用复合主键 upsert（platform_code+order_id / tracking_number），已存在只更新状态不重复插入。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.data.logistics_dao import MySqlLogisticsDAO  # noqa: E402
from app.data.order_dao import MySqlOrderDAO, parse_mysql_dsn  # noqa: E402
from app.data.product_dao import MySqlProductDAO  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化业务库（订单/物流/商品）")
    parser.add_argument("--dsn", default=settings.order_db_dsn or "", help="mysql://user:pass@host:port/db")
    parser.add_argument("--platform", default="pdd", help="seed 订单/物流的平台代码")
    args = parser.parse_args()

    if not args.dsn:
        print("未配置 ORDER_DB_DSN 且未传 --dsn，退出", file=sys.stderr)
        raise SystemExit(1)

    params = parse_mysql_dsn(args.dsn)
    db_name = params["database"]
    if not db_name:
        print("DSN 缺少数据库名", file=sys.stderr)
        raise SystemExit(1)

    server_params = {k: v for k, v in params.items() if k != "database"}
    conn = pymysql.connect(**server_params, charset="utf8mb4", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARSET utf8mb4"
            )
        print(f"[1/4] 数据库 {db_name} 就绪")
    finally:
        conn.close()

    conn = pymysql.connect(**params, charset="utf8mb4", autocommit=True)
    try:
        MySqlOrderDAO.ensure_schema(conn)
        MySqlLogisticsDAO.ensure_schema(conn)
        MySqlProductDAO.ensure_schema(conn)
        print("[2/4] 表 orders / logistics / products 就绪")

        from app.tools.mock_data import LOGISTICS, ORDERS, PRODUCTS

        # 订单 + 物流：物流的 order_id 从订单的 tracking_number 反查。
        tracking_to_order = {
            order["tracking_number"]: order["order_id"]
            for order in ORDERS.values()
            if order.get("tracking_number")
        }
        for order in ORDERS.values():
            MySqlOrderDAO.upsert(conn, order, args.platform)
        for tracking_number, logistics in LOGISTICS.items():
            order_id = tracking_to_order.get(tracking_number, "")
            MySqlLogisticsDAO.upsert(conn, logistics, args.platform, order_id)
        for product in PRODUCTS.values():
            MySqlProductDAO.upsert(conn, product)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM orders")
            order_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM logistics")
            logistics_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM products")
            product_count = cur.fetchone()[0]
        print(
            f"[3/4] seed 完成：orders={order_count} logistics={logistics_count} "
            f"products={product_count}（平台={args.platform}）"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
