# 文件说明：封装旧物流数据访问接口及 MySQL、模拟数据实现，供兼容路径使用。
"""旧物流 DAO；数据库表已退役，待工具切换至 shipments 系列表。

- :class:`MySqlLogisticsDAO`（旧查询实现，不适用于新版数据库）
  - ``query_by_order``：先按 ``order_id+platform_code+user_id`` 查订单拿运单号，
    再查轨迹——归属在第一条 SQL 的 WHERE 里完成；
  - ``query_by_tracking``：**JOIN orders 反查归属**——``tracking_number`` 命中
    但订单不属于当前用户时返回 None（修复旧 mock 实现「运单号直查无鉴权」
    的漏洞，归属校验下沉到 SQL 层，调用方无绕过路径）。
- :class:`MockLogisticsDAO`：DSN 未配置的本地模式，同样执行反查归属。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# pymysql 为可选依赖（仅 MySqlLogisticsDAO 需要）：mock 模式（DSN 未配置）零外部依赖。
try:  # pragma: no cover - 环境依赖
    import pymysql
    import pymysql.cursors
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]

from app.data.dto import LogisticsDTO
from app.data.order_dao import parse_mysql_dsn

LOGISTICS_DDL = """
CREATE TABLE IF NOT EXISTS logistics (
    tracking_number   VARCHAR(64)  NOT NULL,
    platform_code     VARCHAR(16)  NOT NULL,
    order_id          VARCHAR(40)  NOT NULL,
    carrier           VARCHAR(32)  NOT NULL DEFAULT '',
    status            VARCHAR(32)  NOT NULL DEFAULT '',
    events            JSON         NULL,
    PRIMARY KEY (platform_code, tracking_number),
    KEY idx_order (platform_code, order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


@runtime_checkable
class LogisticsDAO(Protocol):
    def query_by_order(self, user_id: str, order_id: str, platform_code: str) -> LogisticsDTO | None: ...

    def query_by_tracking(self, user_id: str, tracking_number: str) -> LogisticsDTO | None: ...


class MySqlLogisticsDAO:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def _connect(self) -> Any:
        if pymysql is None:
            raise RuntimeError("pymysql 未安装：请先 `pip install pymysql`（MySqlLogisticsDAO 需要）")
        return pymysql.connect(
            **parse_mysql_dsn(self._dsn),
            charset="utf8mb4",
            connect_timeout=3,
            read_timeout=10,
            write_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def query_by_order(self, user_id: str, order_id: str, platform_code: str) -> LogisticsDTO | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                # 归属校验在 WHERE（user_id）完成；无运单号视为未发货 → None。
                cur.execute(
                    "SELECT tracking_number FROM orders "
                    "WHERE order_id=%s AND platform_code=%s AND user_id=%s",
                    (order_id, platform_code, user_id),
                )
                row = cur.fetchone()
                if row is None or not row.get("tracking_number"):
                    return None
                cur.execute(
                    "SELECT * FROM logistics "
                    "WHERE tracking_number=%s AND platform_code=%s",
                    (row["tracking_number"], platform_code),
                )
                logistics = cur.fetchone()
        if logistics is None:
            return None
        return LogisticsDTO.from_row(logistics, platform_code)

    def query_by_tracking(self, user_id: str, tracking_number: str) -> LogisticsDTO | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                # JOIN 反查订单归属：轨迹存在但订单不属于当前用户 → join 不到 → None
                cur.execute(
                    "SELECT l.* FROM logistics l "
                    "JOIN orders o ON l.order_id=o.order_id AND l.platform_code=o.platform_code "
                    "WHERE l.tracking_number=%s AND o.user_id=%s",
                    (tracking_number, user_id),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return LogisticsDTO.from_row(row, row.get("platform_code") or "")

    @staticmethod
    def ensure_schema(conn: Any) -> None:
        raise RuntimeError("logistics 旧表已退役；请使用 shipments / shipment_events")

    @staticmethod
    def upsert(conn: Any, logistics: dict[str, Any], platform_code: str, order_id: str) -> None:
        import json

        events = json.dumps(logistics.get("events") or [], ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO logistics ("
                "  tracking_number, platform_code, order_id, carrier, status, events"
                ") VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE status=VALUES(status)",
                (
                    logistics["tracking_number"], platform_code, order_id,
                    logistics.get("carrier") or "", logistics.get("status") or "", events,
                ),
            )
        conn.commit()


class MockLogisticsDAO:
    """内置 mock 物流；与 MySQL 路径一样执行反查归属（修复旧漏洞）。"""

    DEFAULT_PLATFORM = "pdd"

    def query_by_order(self, user_id: str, order_id: str, platform_code: str) -> LogisticsDTO | None:
        if platform_code != self.DEFAULT_PLATFORM:
            return None
        from app.data.mock_data import LOGISTICS, ORDERS

        order = ORDERS.get(order_id)
        if order is None or order.get("user_id") != user_id:
            return None
        tracking_number = order.get("tracking_number")
        if not tracking_number:
            return None
        logistics = LOGISTICS.get(tracking_number)
        if logistics is None:
            return None
        return LogisticsDTO.from_mock(logistics, platform_code, order_id)

    def query_by_tracking(self, user_id: str, tracking_number: str) -> LogisticsDTO | None:
        from app.data.mock_data import LOGISTICS, ORDERS

        logistics = LOGISTICS.get(tracking_number)
        if logistics is None:
            return None
        # 反查订单归属：mock 订单的 tracking_number 与运单号匹配才算有权访问。
        for order in ORDERS.values():
            if order.get("tracking_number") == tracking_number:
                if order.get("user_id") != user_id:
                    return None
                return LogisticsDTO.from_mock(logistics, self.DEFAULT_PLATFORM, order["order_id"])
        return None


__all__ = ["LogisticsDAO", "MySqlLogisticsDAO", "MockLogisticsDAO", "LOGISTICS_DDL", "parse_mysql_dsn"]
