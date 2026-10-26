"""Business service assembly and request-local factory overrides."""

from contextlib import contextmanager
from contextvars import ContextVar

from app.services.commerce.base import BaseService
from app.services.commerce.extended_writes import ExtendedWriteService
from app.services.commerce.extensions import ExtendedService
from app.services.commerce.fulfillment import FulfillmentService
from app.services.commerce.order_changes import OrderChangeService
from app.services.commerce.queries import QueryService


def _missing_fields(data, limit=100):
    missing = []

    def visit(value, path=""):
        if value is None:
            missing.append(path)
        elif isinstance(value, dict):
            for key, child in value.items():
                if len(missing) >= limit:
                    break
                visit(child, path + "." + key if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if len(missing) >= limit:
                    break
                visit(child, f"{path}[{index}]")

    visit(data)
    return missing


def _no_match(data):
    if data.get("total") == 0 and not data.get("after_sale"):
        return True
    return (
        "membership" in data
        and data["membership"] is None
        and not data.get("vip", {}).get("records")
        and not data.get("store_memberships")
    )


class CommerceService(
    OrderChangeService,
    FulfillmentService,
    ExtendedWriteService,
    ExtendedService,
    QueryService,
    BaseService,
):
    """Compose domain operations with explicit extension relationships.

    QueryService extends ReadService/RuleService, and ExtendedWriteService
    extends WriteService. Overrides live in those declared hierarchies.
    """

    def read(self, name, args):
        with self.store.transaction() as tx:
            result = getattr(self, name)(tx, **args)
            result["sources"] = [{"kind": "mysql", "tables": sorted(tx.tables)}]
            # The schema has no upstream synchronization watermark.
            result["data_as_of"] = None
            result["limitations"].append(
                "读取时刻不代表上游同步时刻；数据同步时间未记录"
            )
            result["completeness"] = "partial"
            data = result.get("data", {})
            result["status"] = "no_match" if _no_match(data) else "ok"
            result["missing_fields"] = _missing_fields(data)
            return result


_factory = ContextVar("commerce_factory", default=None)


@contextmanager
def service_factory(factory):
    token = _factory.set(factory)
    try:
        yield
    finally:
        _factory.reset(token)


def service(user=None, scope=None):
    from app.config.settings import settings
    from app.data.business_store import BusinessStore
    from app.tasks.runtime import current_task
    from app.tools.request_context import get_current_user_id

    user = user or get_current_user_id()
    ctx = current_task()
    if scope is None and ctx:
        scope = ctx.runs.candidates.scope
    factory = _factory.get()
    if factory:
        return factory(user, scope)
    return CommerceService(
        BusinessStore(settings.order_db_dsn),
        user,
        scope=scope,
        secret=settings.api_jwt_secret,
        keys=settings.business_data_keys,
        key_version=settings.business_data_key_version,
    )
