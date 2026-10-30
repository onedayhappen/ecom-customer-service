# 文件说明：绑定优惠券领域工具入口，将已校验参数交给统一业务分发。
"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

list_promotions = bind('list_promotions')
list_claimable_coupons = bind('list_claimable_coupons')
find_my_coupons = bind('find_my_coupons')
get_coupon = bind('get_coupon')
check_coupon = bind('check_coupon')
estimate_purchase = bind('estimate_purchase')
claim_coupon = bind('claim_coupon')
