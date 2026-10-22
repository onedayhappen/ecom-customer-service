"""policy tool entry points. Business logic lives in app.services.commerce."""
from app.tools.dispatch import bind

search_policies = bind('search_policies')
