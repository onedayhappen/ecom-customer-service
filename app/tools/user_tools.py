# 文件说明：绑定账号、地址、会员和积分等本人数据查询工具。
"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

get_my_account = bind('get_my_account')
list_addresses = bind('list_addresses')
get_membership = bind('get_membership')
list_points = bind('list_points')
