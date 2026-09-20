"""订单数据传输对象：数据库行 ↔ 标准化订单结构的字段映射。

DTO 存在的意义：SQL 行结构（表字段名、NULL 语义）不泄漏到 MCP 输出与
LLM 上下文，数据源演进（MySQL → PG → HTTP 服务）只改本文件的映射。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OrderDTO:
    """标准化订单结构（与 mock 订单输出保持同构，避免扰动下游提示词/模板）。"""

    order_id: str
    platform_code: str
    user_id: str
    user: str
    status: str
    items: list[dict[str, Any]] = field(default_factory=list)
    total: float = 0.0
    created_at: str | None = None
    shipped_at: str | None = None
    delivered_at: str | None = None
    tracking_number: str | None = None
    carrier: str | None = None
    estimated_delivery: str | None = None
    refund_status: str | None = None
    refund_reason: str | None = None
    refund_requested_at: str | None = None

    # -- 行 → DTO ---------------------------------------------------------

    @classmethod
    def from_row(cls, row: dict[str, Any], platform_code: str) -> "OrderDTO":
        """MySQL 行（列名小写）→ OrderDTO。缺列按 None 处理，容忍表结构演进。"""
        return cls(
            order_id=row["order_id"],
            platform_code=platform_code,
            user_id=row.get("user_id") or "",
            user=row.get("user_name") or "",
            status=row.get("status") or "",
            items=row.get("items") or [],
            total=float(row.get("total") or 0),
            created_at=_fmt_datetime(row.get("created_at")),
            shipped_at=_fmt_datetime(row.get("shipped_at")),
            delivered_at=_fmt_datetime(row.get("delivered_at")),
            tracking_number=row.get("tracking_number"),
            carrier=row.get("carrier"),
            estimated_delivery=_fmt_date(row.get("estimated_delivery")),
            refund_status=row.get("refund_status"),
            refund_reason=row.get("refund_reason"),
            refund_requested_at=_fmt_datetime(row.get("refund_requested_at")),
        )

    @classmethod
    def from_mock(cls, order: dict[str, Any], platform_code: str) -> "OrderDTO":
        """内置 mock 订单（app/tools/mock_data）→ OrderDTO，离线/降级用。"""
        return cls(
            order_id=order["order_id"],
            platform_code=platform_code,
            user_id=order.get("user_id") or "",
            user=order.get("user") or "",
            status=order.get("status") or "",
            items=order.get("items") or [],
            total=float(order.get("total") or 0),
            created_at=order.get("created_at"),
            shipped_at=order.get("shipped_at"),
            delivered_at=order.get("delivered_at"),
            tracking_number=order.get("tracking_number"),
            carrier=order.get("carrier"),
            estimated_delivery=order.get("estimated_delivery"),
            refund_status=order.get("refund_status"),
            refund_reason=order.get("refund_reason"),
            refund_requested_at=order.get("refund_requested_at"),
        )

    # -- DTO → 标准化输出 ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """转 MCP 返回用的标准字典（不含 user_id，避免把归属信息暴露给模型）。"""
        data: dict[str, Any] = {
            "order_id": self.order_id,
            "platform_code": self.platform_code,
            "status": self.status,
            "items": self.items,
            "total": self.total,
        }
        for key in (
            "user", "created_at", "shipped_at", "delivered_at",
            "tracking_number", "carrier", "estimated_delivery",
            "refund_status", "refund_reason", "refund_requested_at",
        ):
            value = getattr(self, key)
            if value not in (None, "", []):
                data[key] = value
        return data


def _fmt_datetime(value: Any) -> str | None:
    """datetime/date → 'YYYY-MM-DD HH:MM:SS' 字符串；None/空原样返回。"""
    if value is None or value == "":
        return None
    return str(value)


def _fmt_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


# ---------------------------------------------------------------------------
# 物流轨迹
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogisticsDTO:
    """标准化物流轨迹结构。"""

    tracking_number: str
    platform_code: str
    order_id: str
    carrier: str
    status: str
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any], platform_code: str) -> "LogisticsDTO":
        events = row.get("events") or []
        if isinstance(events, str):
            try:
                import json as _json

                events = _json.loads(events)
            except (TypeError, ValueError):
                events = []
        return cls(
            tracking_number=row["tracking_number"],
            platform_code=platform_code,
            order_id=row.get("order_id") or "",
            carrier=row.get("carrier") or "",
            status=row.get("status") or "",
            events=events if isinstance(events, list) else [],
        )

    @classmethod
    def from_mock(cls, logistics: dict[str, Any], platform_code: str, order_id: str) -> "LogisticsDTO":
        return cls(
            tracking_number=logistics["tracking_number"],
            platform_code=platform_code,
            order_id=order_id,
            carrier=logistics.get("carrier") or "",
            status=logistics.get("status") or "",
            events=logistics.get("events") or [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracking_number": self.tracking_number,
            "carrier": self.carrier,
            "status": self.status,
            "events": self.events,
        }


# ---------------------------------------------------------------------------
# 商品（公开目录数据，无用户归属）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductDTO:
    """标准化商品结构（与 mock 商品同构，保留评分所需全部字段）。"""

    product_id: str
    merchant_id: str
    name: str
    category: str = ""
    price: float = 0.0
    stock: int = 0
    description: str = ""
    specs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ProductDTO":
        specs = row.get("specs") or {}
        if isinstance(specs, str):
            try:
                import json as _json

                specs = _json.loads(specs)
            except (TypeError, ValueError):
                specs = {}
        return cls(
            product_id=row["product_id"],
            merchant_id=row.get("merchant_id") or "",
            name=row.get("name") or "",
            category=row.get("category") or "",
            price=float(row.get("price") or 0),
            stock=int(row.get("stock") or 0),
            description=row.get("description") or "",
            specs=specs if isinstance(specs, dict) else {},
        )

    @classmethod
    def from_mock(cls, product: dict[str, Any]) -> "ProductDTO":
        return cls(
            product_id=product["product_id"],
            merchant_id=product.get("merchant_id") or "",
            name=product.get("name") or "",
            category=product.get("category") or "",
            price=float(product.get("price") or 0),
            stock=int(product.get("stock") or 0),
            description=product.get("description") or "",
            specs=product.get("specs") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """给评分检索用的完整结构（保持与 mock 商品 dict 同构）。"""
        return {
            "product_id": self.product_id,
            "merchant_id": self.merchant_id,
            "name": self.name,
            "category": self.category,
            "price": self.price,
            "stock": self.stock,
            "description": self.description,
            "specs": self.specs,
        }
