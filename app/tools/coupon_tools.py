"""coupon tool entry points. Business logic lives in app.services.commerce."""
from app.tools.dispatch import bind

list_promotions = bind('list_promotions')
list_claimable_coupons = bind('list_claimable_coupons')
list_my_coupons = bind('list_my_coupons')
get_coupon = bind('get_coupon')
check_coupon = bind('check_coupon')
estimate_purchase = bind('estimate_purchase')
claim_coupon = bind('claim_coupon')
