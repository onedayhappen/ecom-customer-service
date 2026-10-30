# 文件说明：在隔离 MySQL 中测试当前业务迁移的幂等性和订单归属。
"""Opt-in integration check for the current MySQL business schema."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.data.order_dao import parse_mysql_dsn
from scripts.migrate_mysql_business_schema import migrate


@pytest.mark.skipif(not os.getenv("BUSINESS_SCHEMA_TEST_DSN"),
                    reason="isolated MySQL database required")
def test_business_schema_idempotence_and_order_ownership():
    import pymysql

    dsn = os.environ["BUSINESS_SCHEMA_TEST_DSN"]
    assert migrate(dsn)
    assert migrate(dsn)
    assert migrate(dsn, check=True)

    conn = pymysql.connect(**parse_mysql_dsn(dsn), charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT table_name FROM information_schema.tables
                WHERE table_schema=DATABASE()""")
            names = {row[0] for row in cur.fetchall()}
            assert {"users", "orders", "order_items", "product_skus", "shipments",
                    "coupon_templates", "user_coupons", "coupon_redemptions",
                    "after_sale_requests", "refund_transactions"} <= names
            assert "logistics" not in names
            assert {name for name in names if name.startswith('commerce_')} == {'commerce_provider_requests'}
            cur.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='orders'""")
            columns = {row[0] for row in cur.fetchall()}
            assert {"merchant_id", "payable_amount", "paid_amount"} <= columns
            assert not {"items", "total", "tracking_number", "refund_status"} & columns

            owner, other, order_id = (uuid4().hex for _ in range(3))
            for user_id in (owner, other):
                cur.execute("""INSERT INTO users
                    (user_id, username, username_key, status, created_at, updated_at)
                    VALUES (%s,%s,%s,'active',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                    (user_id, user_id, user_id))
            cur.execute("""INSERT INTO orders(platform_code,order_id,user_id)
                VALUES ('pdd',%s,%s)""", (order_id, owner))

            with pytest.raises(pymysql.err.IntegrityError):
                cur.execute("""INSERT INTO payment_transactions
                    (payment_id,platform_code,order_id,user_id,provider,amount,currency,status,created_at)
                    VALUES (%s,'pdd',%s,%s,'test',1.00,'CNY','paid',UTC_TIMESTAMP(6))""",
                    (uuid4().hex, order_id, other))
    finally:
        conn.rollback()
        conn.close()
