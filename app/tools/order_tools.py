"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

find_orders = bind('find_orders')
get_order = bind('get_order')
get_order_items = bind('get_order_items')
get_payments = bind('get_payments')
get_logistics = bind('get_logistics')
assess_order_action = bind('assess_order_action')
prepare_order_address_change = bind('prepare_order_address_change')
prepare_order_cancel = bind('prepare_order_cancel')
quote_delivery = bind('quote_delivery')
request_dispatch = bind('request_dispatch')
quote_checkout = bind('quote_checkout')
prepare_purchase = bind('prepare_purchase')
