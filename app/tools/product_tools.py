# 文件说明：绑定商品、规格与库存等查询工具，复用统一参数校验和分发。
"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

search_products = bind('search_products')
get_product_details = bind('get_product_details')
get_price_history = bind('get_price_history')
get_merchant = bind('get_merchant')
get_restock_plan = bind('get_restock_plan')
