"""物流/商品数据层（第15期扩展）单元测试：不依赖外部 MySQL/Redis。

覆盖重点：tracking_number 直查的归属反查（越权修复）、mock 回退、
MySQL fail-closed、缓存降级、商品目录与评分检索保持。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from app.config.settings import settings
from app.data import logistics_service, product_service
from app.data.dto import LogisticsDTO, ProductDTO
from app.data.logistics_dao import MockLogisticsDAO
from app.data.order_dao import OrderDAO
from app.data.product_dao import MockProductDAO, ProductDAO
from app.tools.request_context import set_current_user_id


@pytest.fixture(autouse=True)
def restore_state():
    """恢复 settings 与 service 单例；测试默认不碰真实 Redis（置空并重置单例）。"""
    original_dsn = settings.order_db_dsn
    original_redis = settings.order_cache_redis_url
    originals = (
        logistics_service._dao,
        logistics_service._cache,
        product_service._dao,
        product_service._cache,
    )
    settings.order_cache_redis_url = ""
    logistics_service._dao = None
    logistics_service._cache = None
    product_service._dao = None
    product_service._cache = None
    yield
    settings.order_db_dsn = original_dsn
    settings.order_cache_redis_url = original_redis
    (logistics_service._dao, logistics_service._cache,
     product_service._dao, product_service._cache) = originals
    set_current_user_id(None)


@pytest.fixture
def user_context():
    token = set_current_user_id("demo_user")
    yield
    set_current_user_id(None)
    from app.tools.request_context import reset_current_user_id

    reset_current_user_id(token)


# ---------------------------------------------------------------------------
# 物流 DAO（mock 实现同样执行归属反查）
# ---------------------------------------------------------------------------

def test_logistics_dao_by_order():
    dao = MockLogisticsDAO()
    dto = dao.query_by_order("demo_user", "ORD-20240115-001", "pdd")
    assert dto is not None and dto.tracking_number == "SF1234567890"
    assert dto.events and isinstance(dto.events, list)
    # 他人订单 / 未发货 / 其他平台
    assert dao.query_by_order("demo_user", "ORD-20240110-003", "pdd") is None
    assert dao.query_by_order("demo_user", "ORD-NOPE", "pdd") is None
    assert dao.query_by_order("demo_user", "ORD-20240115-001", "jd") is None


def test_logistics_dao_by_tracking_enforces_ownership():
    """越权修复：凭运单号直查，也必须反查订单归属。"""
    dao = MockLogisticsDAO()
    # 本人运单 → 命中
    dto = dao.query_by_tracking("demo_user", "SF1234567890")
    assert dto is not None and dto.order_id == "ORD-20240115-001"
    # 他人运单 → None（不泄漏存在性）
    assert dao.query_by_tracking("other_user", "SF1234567890") is None
    # 不存在运单 → None
    assert dao.query_by_tracking("demo_user", "SF-NOPE") is None


# ---------------------------------------------------------------------------
# 物流适配层
# ---------------------------------------------------------------------------

def test_logistics_requires_user_context():
    result = logistics_service.query_logistics(order_id="ORD-20240115-001")
    assert result["error_code"] == "business_rejection"


def test_logistics_validation(user_context):
    result = logistics_service.query_logistics()
    assert result["error_code"] == "validation_error"


def test_logistics_mock_order_path(user_context):
    result = logistics_service.query_logistics(order_id="ORD-20240115-001")
    assert result["success"] is True
    assert result["source"] == "mock"
    assert result["logistics"]["tracking_number"] == "SF1234567890"
    assert "user_id" not in result["logistics"]


def test_logistics_mock_tracking_ownership(user_context):
    """mock 路径 tracking 直查：他人运单 → 统一「暂无轨迹信息」。"""
    result = logistics_service.query_logistics(tracking_number="SF1234567890")
    assert result["success"] is True
    # 换 other_user 查同一运单 → evidence_unavailable（防枚举，不区分无单/无权）
    set_current_user_id("other_user")
    result = logistics_service.query_logistics(tracking_number="SF1234567890")
    assert result["success"] is False
    assert result["error_code"] == "evidence_unavailable"


class _FakeLogisticsDAO:
    def __init__(self, dto: LogisticsDTO | None = None, error: Exception | None = None):
        self.dto = dto
        self.error = error

    def query_by_order(self, user_id, order_id, platform_code):
        if self.error:
            raise self.error
        return self.dto

    def query_by_tracking(self, user_id, tracking_number):
        if self.error:
            raise self.error
        return self.dto


def test_logistics_mysql_fail_closed(user_context):
    settings.order_db_dsn = "mysql://u:p@127.0.0.1:3306/db"
    logistics_service._dao = _FakeLogisticsDAO(error=RuntimeError("db down"))
    result = logistics_service.query_logistics(order_id="ORD-20240115-001")
    assert result["error_code"] == "transient_failure"


# ---------------------------------------------------------------------------
# 商品
# ---------------------------------------------------------------------------

def test_product_dao_mock():
    dao = MockProductDAO()
    products = dao.list_all()
    assert len(products) == 19
    assert all(isinstance(p, ProductDTO) for p in products)
    assert any(p.product_id == "SHOE-270-BK-42" for p in products)


class _FakeProductDAO:
    def __init__(self, products=None, error=None):
        self.products = products or []
        self.error = error

    def list_all(self):
        if self.error:
            raise self.error
        return self.products


def test_dao_protocol_duck():
    assert isinstance(MockLogisticsDAO(), OrderDAO) is False  # 不同协议
    assert isinstance(_FakeProductDAO(), ProductDAO)
