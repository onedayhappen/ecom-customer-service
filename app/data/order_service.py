"""订单查询适配层（service）：MCP 工具与 DAO 之间的唯一编排入口。

职责与安全边界（本设计的关键约定）：

1. **user_id 只来自请求级 ContextVar，绝不接受工具参数**。调用方（MCP 工具 /
   Workflow / ReAct）只传 ``order_id`` 与 ``platform_code``；归属身份由
   ``app.tools.request_context.get_current_user_id()`` 提供——该值由 FastAPI
   验 JWT 后签发的 MCP 上下文令牌（``X-MCP-User-Context``）在 MCP 中间件
   验签后注入，模型无法伪造。

2. **MySQL 是权威数据源，mock 只是「未配置 DSN」的显式本地模式**。
   一旦配置了 ``ORDER_DB_DSN``，查询异常一律返回 ``TRANSIENT_FAILURE``
   （fail-closed，由 ReActPolicy 决定重试/转人工），**绝不静默回退 mock**——
   否则生产库故障时会拿演示数据冒充真实订单，违背「无证据不编造」原则。

3. **缓存键包含 user_id**：``ecom:order:{platform}:{order_id}:{user_id}``，
   命中即归属安全，无需在缓存层重复校验；订单写操作成功后由调用方
   （服务端操作确认 成功路径）按同构键主动失效。

4. 返回结构与现有本地工具同构（``success/order|error/error_code``），
   下游 FailureClassifier / ReActPolicy 无需任何改动即可分类。
"""

from __future__ import annotations

from typing import Any

from app.config.settings import settings
from app.reliability.failure_policy import FailureCategory
from app.tools.request_context import get_current_user_id

from app.data.order_dao import OrderDAO, build_dao
from app.data.redis_cache import JsonCache

_cache: JsonCache | None = None
_dao: OrderDAO | None = None


def _get_dao() -> OrderDAO:
    global _dao
    if _dao is None:
        _dao = build_dao(settings.order_db_dsn, pool_size=settings.order_db_pool_size, pool_timeout=settings.order_db_pool_timeout)
    return _dao


def _get_cache() -> JsonCache | None:
    global _cache
    if _cache is None:
        if settings.order_cache_redis_url:
            _cache = JsonCache(
                settings.order_cache_redis_url,
                ttl=settings.order_cache_ttl_seconds,
            )
    return _cache


def cache_key(order_id: str, platform_code: str, user_id: str) -> str:
    """缓存键（供查询与失效共用同一构造）。"""
    return f"{platform_code}:{order_id}:{user_id}"


def invalidate_order_cache(order_id: str, platform_code: str, user_id: str) -> None:
    """订单状态写操作成功后主动失效缓存（由 服务端操作确认 成功路径调用）。"""
    cache = _get_cache()
    if cache is not None:
        cache.delete(cache_key(order_id, platform_code, user_id))


def query_order_detail(order_id: str, platform_code: str) -> dict[str, Any]:
    """按订单号 + 平台查询订单详情（归属身份取自上下文）。

    返回与本地工具同构的 dict，永不抛异常——错误以
    ``error_code``（FailureCategory）显式表达。
    """
    # 1. 可信身份：上下文，而非参数。
    user_id = get_current_user_id()
    if not user_id:
        return {
            "success": False,
            "error": "缺少用户身份上下文",
            "error_code": FailureCategory.BUSINESS_REJECTION.value,
        }

    # 2. 参数白名单与格式校验（platform_code 防任意字符串进入路由/分库逻辑）。
    allowed_platforms = {
        item.strip() for item in settings.order_platform_codes.split(",") if item.strip()
    }
    if platform_code not in allowed_platforms:
        return {
            "success": False,
            "error": f"不支持的平台: {platform_code}",
            "error_code": FailureCategory.VALIDATION_ERROR.value,
        }
    if not order_id or len(order_id) > 40:
        return {
            "success": False,
            "error": "订单号格式不正确",
            "error_code": FailureCategory.VALIDATION_ERROR.value,
        }

    # 3. cache-aside：先读缓存（键含 user_id，命中即归属安全）。
    cache = _get_cache()
    if cache is not None:
        cached = cache.get(cache_key(order_id, platform_code, user_id))
        if cached is not None:
            return {"success": True, "order": cached, "source": "cache"}

    # 4. 查权威数据源；DSN 未配置 = 显式本地模式，用 mock（source 标注）。
    dao = _get_dao()
    if not settings.order_db_dsn:
        dto = dao.query(user_id, order_id, platform_code)
        if dto is None:
            return {
                "success": False,
                "error": f"未找到订单 {order_id}",
                "error_code": FailureCategory.EVIDENCE_UNAVAILABLE.value,
            }
        return {"success": True, "order": dto.to_dict(), "source": "mock"}

    # 5. MySQL 路径：查询异常 = 瞬时故障（fail-closed），绝不回退 mock。
    try:
        dto = dao.query(user_id, order_id, platform_code)
    except Exception as e:  # noqa: BLE001 —— 适配层兜底，映射为可分类错误
        return {
            "success": False,
            "error": f"订单数据源暂不可用: {type(e).__name__}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
        }
    if dto is None:
        return {
            "success": False,
            "error": f"未找到订单 {order_id}",
            "error_code": FailureCategory.EVIDENCE_UNAVAILABLE.value,
        }

    # 6. 回写缓存（加速层失败静默，不阻塞读）。
    if cache is not None:
        cache.set(cache_key(order_id, platform_code, user_id), dto.to_dict())
    return {"success": True, "order": dto.to_dict(), "source": "mysql"}


def list_user_orders(platform_code: str = "pdd") -> dict[str, Any]:
    """列出当前用户的订单（概要由展示层 tools/user_orders.py 映射）。

    不做 Redis 缓存：订单列表与写操作强相关（退款/取消会改变列表），
    避免缓存脏读；MySQL 本地查询量级可忽略。
    """
    user_id = get_current_user_id()
    if not user_id:
        return {
            "success": False,
            "error": "缺少用户身份上下文",
            "error_code": FailureCategory.BUSINESS_REJECTION.value,
        }

    source = "mock" if not settings.order_db_dsn else "mysql"
    try:
        orders = [dto.to_dict() for dto in _get_dao().list_by_user(user_id, platform_code)]
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "error": f"订单数据源暂不可用: {type(e).__name__}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
        }
    return {"success": True, "orders": orders, "source": source}


def request_refund(order_id: str, reason: str, platform_code: str = "pdd") -> dict:
    """Retired legacy write path; never fall back to a demo refund."""
    return {"success": False, "error_code": "tool_unavailable", "error": "旧退款工具已移除"}
