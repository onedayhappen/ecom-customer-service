# 文件说明：测试旧订单数据适配器的身份、平台、参数校验和模拟查询。
"""订单数据访问层（第15期）单元测试。

约定：不依赖外部 MySQL / Redis——DAO 用 fake 注入、缓存走不可达地址验证降级；
mock 数据源路径（ORDER_DB_DSN 为空）覆盖默认本地模式。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from app.config.settings import settings
from app.data import order_service
from app.data.dto import OrderDTO
from app.data.order_dao import MockOrderDAO, OrderDAO, parse_mysql_dsn
from app.data.redis_cache import JsonCache
from app.tools.request_context import set_current_user_id


class _FakeDAO:
    """可编程 DAO：注入固定结果或抛错，验证适配层的分支逻辑。"""

    def __init__(self, result: OrderDTO | None = None, error: Exception | None = None):
        self.result = result
        self.error = error

    def query(self, user_id: str, order_id: str, platform_code: str) -> OrderDTO | None:
        if self.error is not None:
            raise self.error
        return self.result

    def list_by_user(self, user_id: str, platform_code: str) -> list[OrderDTO]:
        if self.error is not None:
            raise self.error
        return [self.result] if self.result is not None else []


@pytest.fixture
def user_context():
    token = set_current_user_id("demo_user")
    yield
    set_current_user_id(None)
    # 恢复原 token（demo 用例里可能被覆盖过，这里保守复位）
    try:
        from app.tools.request_context import _current_user_id

        _current_user_id.set(token)
    except Exception:  # noqa: BLE001
        pass
    from app.tools.request_context import reset_current_user_id

    reset_current_user_id(token)


@pytest.fixture(autouse=True)
def restore_service_state():
    """用例结束恢复 settings 与 service 单例，避免污染后续测试。

    测试默认不碰真实 Redis：置空 order_cache_redis_url 并重置 _dao/_cache
    单例（防止前序测试在默认 URL 下创建的真实缓存实例残留命中）；缓存行为
    由 JsonCache 单测独立覆盖。
    """
    original_dsn = settings.order_db_dsn
    original_redis = settings.order_cache_redis_url
    original_dao = order_service._dao
    original_cache = order_service._cache
    settings.order_cache_redis_url = ""
    order_service._dao = None
    order_service._cache = None
    yield
    settings.order_db_dsn = original_dsn
    settings.order_cache_redis_url = original_redis
    order_service._dao = original_dao
    order_service._cache = original_cache
    # 无用户上下文兜底
    set_current_user_id(None)


# ---------------------------------------------------------------------------
# 身份与参数防线
# ---------------------------------------------------------------------------

def test_query_requires_user_context():
    """无用户上下文：拒绝（business_rejection），而不是放行。"""
    result = order_service.query_order_detail("ORD-20240115-001", "pdd")
    assert result["success"] is False
    assert result["error_code"] == "business_rejection"


def test_query_platform_whitelist(user_context):
    """platform_code 白名单校验：白名单外直接 validation_error。"""
    result = order_service.query_order_detail("ORD-20240115-001", "taobao")
    assert result["success"] is False
    assert result["error_code"] == "validation_error"


def test_query_order_id_format(user_context):
    """订单号格式校验：空/超长 → validation_error。"""
    result = order_service.query_order_detail("", "pdd")
    assert result["error_code"] == "validation_error"
    result = order_service.query_order_detail("O" * 41, "pdd")
    assert result["error_code"] == "validation_error"


# ---------------------------------------------------------------------------
# mock 数据源（DSN 未配置 = 显式本地模式）
# ---------------------------------------------------------------------------

def test_mock_dao_returns_own_order():
    dao = MockOrderDAO()
    dto = dao.query("demo_user", "ORD-20240115-001", "pdd")
    assert dto is not None and dto.order_id == "ORD-20240115-001"
    # 他人订单/不存在/其他平台 → None
    assert dao.query("demo_user", "ORD-20240110-003", "pdd") is None
    assert dao.query("demo_user", "ORD-NOPE", "pdd") is None
    assert dao.query("demo_user", "ORD-20240115-001", "jd") is None


def test_query_mock_fallback(user_context):
    """默认配置（DSN 空）→ mock 数据源，source 显式标注 mock。"""
    result = order_service.query_order_detail("ORD-20240115-001", "pdd")
    assert result["success"] is True
    assert result["source"] == "mock"
    assert result["order"]["order_id"] == "ORD-20240115-001"
    # mock 输出不含归属字段
    assert "user_id" not in result["order"]


def test_query_mock_other_user_denied(user_context):
    """mock 数据源同样执行归属校验：查他人订单 = 无证据。"""
    result = order_service.query_order_detail("ORD-20240110-003", "pdd")
    assert result["success"] is False
    assert result["error_code"] == "evidence_unavailable"


# ---------------------------------------------------------------------------
# MySQL 路径：成功 / fail-closed / 未命中
# ---------------------------------------------------------------------------

def _sample_dto() -> OrderDTO:
    return OrderDTO(
        order_id="ORD-20240115-001", platform_code="pdd", user_id="demo_user",
        user="小明", status="shipped", total=899.0,
        items=[{"name": "Nike Air Max 270 运动鞋", "quantity": 1, "price": 899.0}],
    )


def test_query_mysql_success(user_context):
    settings.order_db_dsn = "mysql://u:p@127.0.0.1:3306/db"
    settings.order_cache_redis_url = ""
    order_service._dao = _FakeDAO(result=_sample_dto())
    result = order_service.query_order_detail("ORD-20240115-001", "pdd")
    assert result["success"] is True
    assert result["source"] == "mysql"
    assert result["order"]["status"] == "shipped"


def test_query_mysql_failure_is_fail_closed(user_context):
    """MySQL 配置后查询异常 = TRANSIENT_FAILURE，绝不静默回退 mock。"""
    settings.order_db_dsn = "mysql://u:p@127.0.0.1:3306/db"
    settings.order_cache_redis_url = ""
    order_service._dao = _FakeDAO(error=RuntimeError("connection refused"))
    result = order_service.query_order_detail("ORD-20240115-001", "pdd")
    assert result["success"] is False
    assert result["error_code"] == "transient_failure"
    assert "暂不可用" in result["error"]


def test_query_mysql_not_found(user_context):
    settings.order_db_dsn = "mysql://u:p@127.0.0.1:3306/db"
    settings.order_cache_redis_url = ""
    order_service._dao = _FakeDAO(result=None)
    result = order_service.query_order_detail("ORD-999", "pdd")
    assert result["success"] is False
    assert result["error_code"] == "evidence_unavailable"


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def test_cache_key_includes_user_and_platform():
    k1 = order_service.cache_key("O1", "pdd", "u1")
    assert k1 == "pdd:O1:u1"
    assert k1 != order_service.cache_key("O1", "pdd", "u2")
    assert k1 != order_service.cache_key("O1", "jd", "u1")


def test_cache_degraded_when_redis_unreachable():
    """Redis 不可达：get 返回 None、set 静默丢弃，不抛异常（fail-safe）。"""
    cache = JsonCache("redis://127.0.0.1:1/0", ttl=60)
    assert cache.get("x") is None
    cache.set("x", {"v": 1})  # 不应抛异常
    assert cache.get("x") is None
    cache.delete("x")  # 不应抛异常


# ---------------------------------------------------------------------------
# 映射与 DSN 解析
# ---------------------------------------------------------------------------

def test_dto_to_dict_excludes_user_id():
    data = _sample_dto().to_dict()
    assert "user_id" not in data
    assert data["order_id"] == "ORD-20240115-001"


def test_parse_mysql_dsn():
    params = parse_mysql_dsn("mysql://root:secret@10.0.0.1:3307/ecom")
    assert params == {
        "host": "10.0.0.1", "port": 3307, "user": "root",
        "password": "secret", "database": "ecom",
    }
    assert parse_mysql_dsn("") == {}


def test_dao_interface_duck():
    """OrderDAO 是协议类型：fake 实现可直接注入（鸭子类型）。"""
    assert isinstance(_FakeDAO(result=None), OrderDAO)


# ---------------------------------------------------------------------------
# 工具层切换（query_order / list_user_orders 统一走 app/data）
# ---------------------------------------------------------------------------

def test_list_user_orders_mock(user_context):
    settings.order_cache_redis_url = ""
    result = order_service.list_user_orders()
    assert result["success"] is True
    assert result["source"] == "mock"
    assert {o["order_id"] for o in result["orders"]} == {
        "ORD-20240115-001", "ORD-20240120-002",
        "ORD-20240125-006", "ORD-20240126-007",
        "ORD-20240128-008", "ORD-20240201-010",
    }


def test_list_user_orders_requires_context():
    result = order_service.list_user_orders()
    assert result["success"] is False
    assert result["error_code"] == "business_rejection"


def test_list_user_orders_mysql_fail_closed(user_context):
    settings.order_db_dsn = "mysql://u:p@127.0.0.1:3306/db"
    order_service._dao = _FakeDAO(error=RuntimeError("db down"))
    result = order_service.list_user_orders()
    assert result["error_code"] == "transient_failure"
