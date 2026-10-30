# 文件说明：提供业务对象归属、可信范围、快照和通用读取能力。
"""Shared ownership, scope and business snapshot operations."""

import base64

from app.services.commerce.common import (
    BusinessError,
    Cipher,
    decoded,
    digest,
    dumps,
    ref,
    unref,
    utcnow,
)
from app.services.commerce.pagination import PaginationMixin


class BaseService(PaginationMixin):
    def __init__(self, store, user, *, scope=None, secret="", keys="{}", key_version=1):
        if not user:
            raise BusinessError("invalid_arguments", "需要登录身份")
        self.store, self.user, self.scope, self.secret = store, user, scope, secret
        self.cipher = Cipher(keys, key_version)

    def scope_check(self, row):
        for key, field in [
            ("platform_code", "platform_code"),
            ("store_id", "merchant_id"),
        ]:
            expected = getattr(self.scope, key, None)
            if expected and row.get(field) != expected:
                raise BusinessError("not_found", "对象不存在")

    def coupon_scope_check(self, row):
        # A platform coupon has no merchant restriction.
        self.scope_check(
            {
                **row,
                "merchant_id": row.get("merchant_id")
                or getattr(self.scope, "store_id", None),
            }
        )

    def default_platform(self, tx):
        # Resolve from trusted business records, not the legacy routing default.
        rows = tx.all(
            """SELECT platform_code FROM orders WHERE user_id=%s
            UNION SELECT platform_code FROM user_platform_accounts WHERE user_id=%s AND status='active' AND verified_at IS NOT NULL
            UNION SELECT platform_code FROM memberships WHERE user_id=%s AND status='active' LIMIT 2""",
            (self.user, self.user, self.user),
        )
        if rows:
            return rows[0]["platform_code"] if len(rows) == 1 else None
        rows = tx.all(
            "SELECT DISTINCT platform_code FROM business_policy_versions WHERE status='active' AND effective_from<=%s AND (effective_until IS NULL OR effective_until>%s) LIMIT 2",
            (utcnow(), utcnow()),
        )
        return rows[0]["platform_code"] if len(rows) == 1 else None

    def sales_platform(self, tx):
        platform = getattr(self.scope, "platform_code", None)
        if platform:
            return platform
        from app.tasks.runtime import current_task

        ctx = current_task()
        if not ctx:
            return self.default_platform(tx)
        if ctx.runs.user != self.user:
            return None
        candidate = ctx.record.details.get("platform_code")
        if not candidate:
            return self.default_platform(tx)
        owned = tx.one(
            "SELECT order_id FROM orders WHERE user_id=%s AND platform_code=%s LIMIT 1",
            (self.user, candidate),
        )
        bound = tx.one(
            "SELECT id FROM user_platform_accounts WHERE user_id=%s AND platform_code=%s AND status='active' AND verified_at IS NOT NULL LIMIT 1",
            (self.user, candidate),
        )
        return candidate if owned or bound else None

    def owned_order(self, tx, order_ref, *, lock=False):
        _, platform, oid = unref(order_ref, {"order"})
        row = tx.one(
            "SELECT * FROM orders WHERE platform_code=%s AND order_id=%s AND user_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (platform, oid, self.user),
        )
        if not row:
            raise BusinessError("not_found", "订单不存在")
        self.scope_check(row)
        return row

    def sale(self, tx, aid, *, lock=False):
        row = tx.one(
            "SELECT * FROM after_sale_requests WHERE after_sale_id=%s AND user_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (aid, self.user),
        )
        if not row:
            raise BusinessError("not_found", "售后申请不存在")
        order = self.owned_order(
            tx, ref("order", row["platform_code"], row["order_id"]), lock=lock
        )
        return row, order

    def order_base_status(self, order):
        return (
            order.get("status_before_after_sale")
            if order["status"] == "after_sale"
            else order["status"]
        )

    def sync_order_after_sale(self, tx, order):
        # Lock the parent first, as all after-sale writers do. Multiple active
        # requests must not restore the order when only one request terminates.
        current = self.owned_order(
            tx, ref("order", order["platform_code"], order["order_id"]), lock=True
        )
        active = tx.one(
            "SELECT after_sale_id FROM after_sale_requests WHERE platform_code=%s AND order_id=%s AND status IN ('pending_review','approved','processing') LIMIT 1",
            (current["platform_code"], current["order_id"]),
        )
        if active and current["status"] != "after_sale":
            tx.execute(
                "UPDATE orders SET status_before_after_sale=%s,status='after_sale',updated_at=%s WHERE platform_code=%s AND order_id=%s",
                (
                    current["status"],
                    utcnow(),
                    current["platform_code"],
                    current["order_id"],
                ),
            )
        elif not active and current["status"] == "after_sale":
            previous = current.get("status_before_after_sale")
            if not previous:
                raise BusinessError("insufficient_data", "售后前订单状态缺失")
            tx.execute(
                "UPDATE orders SET status=%s,status_before_after_sale=NULL,updated_at=%s WHERE platform_code=%s AND order_id=%s",
                (previous, utcnow(), current["platform_code"], current["order_id"]),
            )

    def order_rows(self, tx, table, order):
        allowed = {
            "order_items",
            "shipments",
            "payment_transactions",
            "after_sale_requests",
            "invoice_requests",
            "coupon_redemptions",
            "inventory_reservations",
            "order_events",
        }
        if table not in allowed:
            raise ValueError("invalid internal table")
        return tx.all(
            f"SELECT * FROM {table} WHERE platform_code=%s AND order_id=%s",
            (order["platform_code"], order["order_id"]),
        )

    def public_product(self, tx, pid):
        row = tx.one(
            "SELECT product_id,merchant_id,name,category,description,specs FROM products WHERE product_id=%s",
            (pid,),
        )
        if not row:
            raise BusinessError("not_found", "商品不存在")
        merchant = getattr(self.scope, "store_id", None)
        if merchant and row["merchant_id"] != merchant:
            raise BusinessError("not_found", "商品不存在")
        row["specs"] = decoded(row.get("specs"))
        return row

    def sku(self, tx, sid):
        lock = " FOR UPDATE" if tx.write else ""
        row = tx.one("SELECT * FROM product_skus WHERE sku_id=%s" + lock, (sid,))
        if not row:
            raise BusinessError("not_found", "规格不存在")
        product = self.public_product(tx, row["product_id"])
        inv = tx.one(
            "SELECT available_qty,updated_at FROM sku_inventory WHERE sku_id=%s" + lock,
            (sid,),
        )
        row["spec_values"] = decoded(row["spec_values"], {})
        allowed = row["spec_values"].get(
            "cancel_allowed", (product.get("specs") or {}).get("cancel_allowed")
        )
        if allowed is not None and not isinstance(allowed, bool):
            raise BusinessError("insufficient_data", "商品取消条件格式无法核实")
        row["cancel_allowed"] = (
            allowed
            if allowed is not None
            else product["category"] not in {"定制", "定制商品", "生鲜", "生鲜食品"}
        )
        row.update(
            merchant_id=product["merchant_id"],
            product_name=product["name"],
            inventory=inv,
            subject_ref=ref("sku", sid),
        )
        return row

    def subject(self, tx, value, *, allow_order=True):
        if not value:
            return {
                "platform_code": self.sales_platform(tx),
                "merchant_id": getattr(self.scope, "store_id", None),
            }
        data = unref(
            value,
            {"merchant", "product", "sku", "order"}
            if allow_order
            else {"merchant", "product", "sku"},
        )
        if data[0] == "order":
            row = self.owned_order(tx, value)
            return dict(
                platform_code=row["platform_code"],
                merchant_id=row["merchant_id"],
                order=row,
            )
        if data[0] == "sku":
            row = self.sku(tx, data[1])
        elif data[0] == "product":
            row = self.public_product(tx, data[1])
        else:
            row = tx.one(
                "SELECT merchant_id FROM merchants WHERE merchant_id=%s", (data[1],)
            )
            if not row:
                raise BusinessError("not_found", "商家不存在")
            if (
                getattr(self.scope, "store_id", None)
                and row["merchant_id"] != self.scope.store_id
            ):
                raise BusinessError("not_found", "商家不存在")
        return {**row, "platform_code": self.sales_platform(tx)}

    def snapshot(self, tx, order):
        tables = [
            "order_items",
            "shipments",
            "payment_transactions",
            "after_sale_requests",
            "invoice_requests",
            "coupon_redemptions",
            "inventory_reservations",
            "order_events",
        ]
        bundle = {"order": order}
        for table in tables:
            bundle[table] = self.order_rows(tx, table, order)
        bundle["address"] = tx.one(
            "SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s",
            (order["platform_code"], order["order_id"]),
        )

        # Ciphertext is hashed, never returned to the model.
        def binary(v):
            if isinstance(v, bytes):
                return base64.b64encode(v).decode()
            if isinstance(v, dict):
                return {k: binary(x) for k, x in v.items()}
            if isinstance(v, list):
                return [binary(x) for x in v]
            return v

        bundle = binary(bundle)
        for table in tables:
            bundle[table] = sorted(bundle[table], key=dumps)
        return digest(bundle)
