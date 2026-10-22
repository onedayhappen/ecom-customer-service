"""Business service factory; tool adapters own schemas, this layer owns transactions."""
from contextlib import contextmanager
from contextvars import ContextVar
from app.services.commerce.common import BaseService
from app.services.commerce.reads import ReadService
from app.services.commerce.rules import RuleService
from app.services.commerce.writes import WriteService

class CommerceService(BaseService, ReadService, RuleService, WriteService):
    def read(self,name,args):
        with self.store.transaction() as tx:
            result=getattr(self,name)(tx,**args)
            result['sources']=[{'kind':'mysql','tables':sorted(tx.tables)}]
            # No source synchronization watermark exists in this schema.
            result['data_as_of']=None
            result['limitations'].append('读取时刻不代表上游同步时刻；数据同步时间未记录')
            result['completeness']='partial'
            return result

_factory=ContextVar('commerce_factory',default=None)

@contextmanager
def service_factory(factory):
    token=_factory.set(factory)
    try: yield
    finally: _factory.reset(token)

def service(user=None,scope=None):
    from app.tools.request_context import get_current_user_id
    from app.config.settings import settings
    from app.data.business_store import BusinessStore
    from app.tasks.runtime import current_task
    user=user or get_current_user_id()
    ctx=current_task()
    if scope is None and ctx: scope=ctx.runs.candidates.scope
    if _factory.get(): return _factory.get()(user,scope)
    return CommerceService(BusinessStore(settings.order_db_dsn),user,scope=scope,secret=settings.api_jwt_secret,
        keys=settings.business_data_keys,key_version=settings.business_data_key_version)
