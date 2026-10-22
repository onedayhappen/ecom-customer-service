"""user tool entry points. Business logic lives in app.services.commerce."""
from app.tools.dispatch import bind

get_my_profile = bind('get_my_profile')
list_addresses = bind('list_addresses')
list_platform_accounts = bind('list_platform_accounts')
