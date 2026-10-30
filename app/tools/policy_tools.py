# 文件说明：绑定政策检索工具入口，使用统一契约构造可追溯政策回执。
"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

search_policies = bind('search_policies')
