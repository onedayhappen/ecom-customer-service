"""product tool entry points. Business logic lives in app.services.commerce."""
from app.tools.dispatch import bind

search_products = bind('search_products')
get_product = bind('get_product')
get_sku = bind('get_sku')
compare_skus = bind('compare_skus')
get_price_history = bind('get_price_history')
get_merchant = bind('get_merchant')
