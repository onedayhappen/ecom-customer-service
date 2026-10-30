# 文件说明：编排旧物流查询、用户归属检查与可选缓存访问。
"""物流查询适配层：order / tracking 双路径，归属校验下沉 SQL 层。

与订单适配层同约定：
- user_id 只来自请求级 ContextVar，不接收工具参数；
- 物流轨迹属订单敏感数据，**tracking_number 直查也做归属反查**
  （JOIN orders），不区分「无此运单」与「无权查看」——防止运单号枚举；
- DSN 未配置 → mock（source 标注）；配置后查询异常 → TRANSIENT_FAILURE
  fail-closed，绝不静默回退 mock。
"""

from __future__ import annotations

from typing import Any

from app.config.settings import settings
from app.reliability.failure_policy import FailureCategory
from app.tools.request_context import get_current_user_id

from app.data.logistics_dao import MockLogisticsDAO, MySqlLogisticsDAO
from app.data.redis_cache import JsonCache

_cache: JsonCache | None = None
_dao: Any | None = None


def _get_dao() -> Any:
    global _dao
    if _dao is None:
        if settings.order_db_dsn:
            _dao = MySqlLogisticsDAO(settings.order_db_dsn)
        else:
            _dao = MockLogisticsDAO()
    return _dao


def _get_cache() -> JsonCache | None:
    global _cache
    if _cache is None and settings.order_cache_redis_url:
        _cache = JsonCache(
            settings.order_cache_redis_url,
            prefix="ecom:logi",
            ttl=settings.order_cache_ttl_seconds,
        )
    return _cache


def query_logistics(
    order_id: str | None = None,
    tracking_number: str | None = None,
    platform_code: str = "pdd",
) -> dict[str, Any]:
    """按订单号或物流单号查询轨迹；返回与本地工具同构的 dict，永不抛异常。

    - tracking_number 与 order_id 同时传入时以 tracking_number 为准（与旧工具语义一致）。
    """
    user_id = get_current_user_id()
    if not user_id:
        return {
            "success": False,
            "error": "缺少用户身份上下文",
            "error_code": FailureCategory.BUSINESS_REJECTION.value,
        }

    if not order_id and not tracking_number:
        return {
            "success": False,
            "error": "请提供订单号或物流单号",
            "error_code": FailureCategory.VALIDATION_ERROR.value,
        }

    cache = _get_cache()
    source = "mock" if not settings.order_db_dsn else "mysql"
    dao = _get_dao()

    # -- 运单号直查（归属反查） --------------------------------------------
    if tracking_number:
        cache_key = f"tracking:{tracking_number}:{user_id}"
        if cache is not None:
            cached = cache.get(cache_key)
            if cached is not None:
                return {"success": True, "logistics": cached, "source": "cache"}
        try:
            dto = dao.query_by_tracking(user_id, tracking_number)
        except Exception as e:  # noqa: BLE001
            return {
                "success": False,
                "error": f"物流数据源暂不可用: {type(e).__name__}",
                "error_code": FailureCategory.TRANSIENT_FAILURE.value,
            }
        if dto is None:
            # 运单不存在或无权访问：统一「暂无轨迹信息」，防运单号枚举。
            return {
                "success": False,
                "error": f"物流单号 {tracking_number} 暂无轨迹信息",
                "error_code": FailureCategory.EVIDENCE_UNAVAILABLE.value,
            }
        data = dto.to_dict()
        if cache is not None:
            cache.set(cache_key, data)
        return {"success": True, "logistics": data, "source": source}

    # -- 订单路径（先查订单归属 + 运单号，再查轨迹） --------------------------
    cache_key = f"order:{platform_code}:{order_id}:{user_id}"
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return {"success": True, "logistics": cached, "source": "cache"}
    try:
        dto = dao.query_by_order(user_id, order_id, platform_code)
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "error": f"物流数据源暂不可用: {type(e).__name__}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
        }
    if dto is None:
        # 订单不存在 / 归属不符 / 未发货 / 无轨迹：统一按无证据处理。
        return {
            "success": False,
            "error": f"未找到订单 {order_id} 的物流信息",
            "error_code": FailureCategory.EVIDENCE_UNAVAILABLE.value,
        }
    data = dto.to_dict()
    if cache is not None:
        cache.set(cache_key, data)
    return {"success": True, "logistics": data, "source": source}
