"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

search_policies = bind('search_policies')
