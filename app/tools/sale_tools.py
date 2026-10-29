"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

find_after_sales = bind('find_after_sales')
get_after_sale_status = bind('get_after_sale_status')
get_after_sale_items = bind('get_after_sale_items')
get_refunds = bind('get_refunds')
assess_after_sale = bind('assess_after_sale')
prepare_after_sale = bind('prepare_after_sale')
prepare_after_sale_cancel = bind('prepare_after_sale_cancel')
assess_price_protection = bind('assess_price_protection')
prepare_price_protection = bind('prepare_price_protection')
prepare_exchange = bind('prepare_exchange')
get_return_instructions = bind('get_return_instructions')
prepare_return_shipment = bind('prepare_return_shipment')
prepare_repair = bind('prepare_repair')
