# 文件说明：为旧商品查询路径组织目录加载与可选缓存。
"""商品目录适配层：公开数据（无用户归属），全量加载供评分检索。

旧商品工具已移除；本层保留数据源与缓存供后续改造：
- MySQL 全量加载（目录规模小，加载成本可忽略）；
- 目录静态，缓存 TTL 按订单缓存配置；失败 fail-closed（TRANSIENT_FAILURE）。
"""

from __future__ import annotations

from typing import Any

from app.config.settings import settings
from app.reliability.failure_policy import FailureCategory

from app.data.product_dao import MockProductDAO, MySqlProductDAO
from app.data.redis_cache import JsonCache

_cache: JsonCache | None = None
_dao: Any | None = None


def _get_dao() -> Any:
    global _dao
    if _dao is None:
        if settings.order_db_dsn:
            _dao = MySqlProductDAO(settings.order_db_dsn)
        else:
            _dao = MockProductDAO()
    return _dao


def _get_cache() -> JsonCache | None:
    global _cache
    if _cache is None and settings.order_cache_redis_url:
        _cache = JsonCache(
            settings.order_cache_redis_url,
            prefix="ecom:product",
            ttl=settings.order_cache_ttl_seconds,
        )
    return _cache


def list_products() -> dict[str, Any]:
    """返回全部商品（dict 列表）；失败返回 fail-closed 错误 dict，不抛异常。"""
    cache = _get_cache()
    if cache is not None:
        cached = cache.get("all")
        if cached is not None:
            return {"success": True, "products": cached, "source": "cache"}

    source = "mock" if not settings.order_db_dsn else "mysql"
    try:
        products = [dto.to_dict() for dto in _get_dao().list_all()]
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "error": f"商品数据源暂不可用: {type(e).__name__}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
        }

    if cache is not None:
        cache.set("all", products)
    return {"success": True, "products": products, "source": source}
