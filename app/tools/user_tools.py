"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

get_my_account = bind('get_my_account')
list_addresses = bind('list_addresses')
get_membership = bind('get_membership')
list_points = bind('list_points')
