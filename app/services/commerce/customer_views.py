# 文件说明：将业务事实投影为客户页面所需的资料、订单与优惠券视图。
"""Customer-facing read models, independent of HTTP and tool result envelopes."""

from app.data.membership_store import membership_facts
from app.services.commerce.common import BusinessError, decoded, ref


class CustomerViews:
    def __init__(self, commerce):
        self.commerce = commerce

    def profile(self):
        svc = self.commerce
        with svc.store.transaction() as tx:
            account = tx.one(
                "SELECT username,created_at FROM users WHERE user_id=%s", (svc.user,)
            )
            if not account:
                raise BusinessError("account_not_found", "账号资料不存在")
            platforms = tx.all(
                "SELECT DISTINCT platform_code FROM memberships WHERE user_id=%s ORDER BY platform_code",
                (svc.user,),
            )
            return {
                **account,
                "memberships": [
                    membership_facts(tx, svc.user, row["platform_code"])
                    for row in platforms
                ],
            }

    def orders(self, *, status="all", cursor=None):
        svc = self.commerce
        with svc.store.transaction() as tx:
            result = svc.find_orders(tx, status=status, cursor=cursor)
            for order in result["data"]["items"]:
                order["items"] = [
                    {
                        "order_item_id": item["order_item_id"],
                        "title_snapshot": item["title_snapshot"],
                        "spec_snapshot": decoded(item["spec_snapshot"], {}),
                        "quantity": item["quantity"],
                    }
                    for item in svc.order_rows(tx, "order_items", order)
                ]
            return {**result["data"], "next_cursor": result["next_cursor"]}

    def coupons(self, *, status="all", cursor=None, coupon_type="all"):
        svc = self.commerce
        with svc.store.transaction() as tx:
            result = svc.list_my_coupons(
                tx,
                status=status,
                cursor=cursor,
                exclude_expired=True,
                coupon_type=coupon_type,
            )
            for coupon in result["data"]["items"]:
                template, instance, scopes = svc.coupon(
                    tx, ref("coupon_instance", coupon["user_coupon_id"])
                )
                basis = "claim_snapshot"
                try:
                    template, scopes = svc.frozen_coupon(tx, template, instance, scopes)
                except BusinessError:
                    basis = "current_template"
                coupon["template"] = {
                    key: template.get(key)
                    for key in (
                        "name",
                        "kind",
                        "currency",
                        "platform_code",
                        "threshold_amount",
                        "discount_amount",
                        "discount_bps",
                        "cap_amount",
                    )
                }
                coupon["rules_basis"] = basis
                coupon["scope_description"] = (
                    "全场适用"
                    if any(scope["scope_type"] == "all" for scope in scopes)
                    else "限指定商家或商品使用"
                )
            return {**result["data"], "next_cursor": result["next_cursor"]}
