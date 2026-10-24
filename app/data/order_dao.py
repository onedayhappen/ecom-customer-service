"""订单 DAO：唯一执行 SQL 的代码层。

- :class:`MySqlOrderDAO`：pymysql 参数化查询本地业务库
  （``ORDER_DB_DSN`` 形如 ``mysql://user:pass@host:port/db``）。
  查询强制 ``where order_id=? and platform_code=? and user_id=?``，
  user_id 由上层（order_service）从请求级 ContextVar 传入，绝无参数注入路径。
- :class:`MockOrderDAO`：内置 mock 数据源，DSN 未配置 / MySQL 不可用时
  由 service 降级使用，保证离线与测试零外部依赖。

注意：连接按查询即建即关，本地演示够用；生产替换为连接池（可复用
``app.db`` 的池化思路）。
"""

from __future__ import annotations

import json
import queue
import threading
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import unquote, urlparse
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# pymysql 为可选依赖（仅 MySqlOrderDAO 需要）：mock 模式（DSN 未配置）零外部依赖，
# 缺 pymysql 时导入模块不受影响，与 intent/classifier.py 的 torch 可选依赖模式一致。
try:  # pragma: no cover - 环境依赖
    import pymysql
    import pymysql.cursors
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]

from app.data.dto import OrderDTO

# 复合主键 (platform_code, order_id)：多平台下订单号不全局唯一。
ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS orders (
    platform_code        VARCHAR(16)  NOT NULL,
    order_id             VARCHAR(40)  NOT NULL,
    user_id              VARCHAR(64)  NOT NULL,
    status               VARCHAR(32)  NOT NULL DEFAULT 'pending_payment',
    status_before_after_sale VARCHAR(32) NULL,
    created_at           DATETIME     NULL,
    merchant_id          VARCHAR(64)  NULL,
    currency             CHAR(3)      NULL,
    goods_amount         DECIMAL(12,2) NULL,
    discount_amount      DECIMAL(12,2) NULL,
    shipping_amount      DECIMAL(12,2) NULL,
    payable_amount       DECIMAL(12,2) NULL,
    paid_amount          DECIMAL(12,2) NULL,
    paid_at              DATETIME(6) NULL,
    updated_at           DATETIME(6) NULL,
    PRIMARY KEY (platform_code, order_id),
    KEY idx_user (user_id, platform_code),
    UNIQUE KEY uq_orders_owner (platform_code, order_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


@runtime_checkable
class OrderDAO(Protocol):
    """DAO 接口：查询归属当前用户的订单，未命中返回 None（不抛异常）。"""

    def query(self, user_id: str, order_id: str, platform_code: str) -> OrderDTO | None: ...

    def list_by_user(self, user_id: str, platform_code: str) -> list[OrderDTO]: ...

    def query(self, user_id: str, order_id: str, platform_code: str) -> OrderDTO | None: ...


def parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    """解析 MySQL URL，正确处理 URL 编码密码、IPv6 和查询参数。"""
    if not dsn:
        return {}
    parsed = urlparse(dsn if "://" in dsn else f"mysql://{dsn}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3306
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    db = parsed.path.lstrip("/")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": db,
    }


class MySqlOrderDAO:
    """MySQL 订单数据访问实现。"""

    def __init__(self, dsn: str, *, pool_size: int = 10, pool_timeout: float = 5.0):
        self._dsn = dsn
        self._pool_size = max(1, int(pool_size))
        self._pool_timeout = max(0.1, float(pool_timeout))
        self._pool: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=self._pool_size)
        self._created = 0
        self._lock = threading.Lock()

    def _connect(self) -> Any:
        if pymysql is None:
            raise RuntimeError("pymysql 未安装：请先 `pip install pymysql`（MySqlOrderDAO 需要）")
        return pymysql.connect(
            **parse_mysql_dsn(self._dsn),
            charset="utf8mb4",
            connect_timeout=3,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _acquire(self) -> Any:
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._pool_size:
                    conn = self._connect()
                    self._created += 1
                else:
                    conn = None
            if conn is None:
                try:
                    conn = self._pool.get(timeout=self._pool_timeout)
                except queue.Empty as exc:
                    raise TimeoutError("MySQL 连接池获取连接超时") from exc
        try:
            conn.ping(reconnect=True)
        except Exception:
            self._discard(conn)
            return self._acquire()
        return conn

    def _release(self, conn: Any) -> None:
        try:
            conn.rollback()
            self._pool.put_nowait(conn)
        except Exception:
            self._discard(conn)

    def _discard(self, conn: Any) -> None:
        try:
            conn.close()
        finally:
            with self._lock:
                self._created = max(0, self._created - 1)

    @contextmanager
    def _connection(self):
        conn = self._acquire()
        try:
            yield conn
            conn.commit()
        finally:
            self._release(conn)

    def query(self, user_id: str, order_id: str, platform_code: str) -> OrderDTO | None:
        """参数化查询；归属校验在 SQL 层完成（user_id 参与 WHERE）。"""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM orders "
                    "WHERE order_id=%s AND platform_code=%s AND user_id=%s",
                    (order_id, platform_code, user_id),
                )
                row = cur.fetchone()
        if row is None:
            return None
        if "items" not in row:
            raise RuntimeError("订单工具尚未适配新版 order_items，不能返回不完整的订单金额和商品")
        if isinstance(row.get("items"), str):
            try:
                row["items"] = json.loads(row["items"])
            except (TypeError, json.JSONDecodeError):
                row["items"] = []
        return OrderDTO.from_row(row, platform_code)

    def list_by_user(self, user_id: str, platform_code: str) -> list[OrderDTO]:
        """按用户列出订单（idx_user 索引）；列表不做分页（本地客服规模）。"""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM orders "
                    "WHERE user_id=%s AND platform_code=%s "
                    "ORDER BY created_at DESC",
                    (user_id, platform_code),
                )
                rows = cur.fetchall()
        result: list[OrderDTO] = []
        for row in rows:
            if "items" not in row:
                raise RuntimeError("订单工具尚未适配新版 order_items，不能返回不完整的订单金额和商品")
            if isinstance(row.get("items"), str):
                try:
                    row["items"] = json.loads(row["items"])
                except (TypeError, json.JSONDecodeError):
                    row["items"] = []
            result.append(OrderDTO.from_row(row, platform_code))
        return result

    def request_refund(
        self, user_id: str, order_id: str, platform_code: str, reason: str,
    ) -> str:
        """旧退款写入已退役；后续改为售后申请与退款流水。"""
        raise RuntimeError("旧退款工具尚未适配 after_sale_requests / refund_transactions")

    # -- 迁移脚本建表入口 --------------------------------------------------

    @staticmethod
    def ensure_schema(conn: Any) -> None:
        """建立新版订单头；完整业务表由版本化迁移负责。"""
        with conn.cursor() as cur:
            cur.execute(ORDERS_DDL)
        conn.commit()

    @staticmethod
    def upsert(conn: Any, order: dict[str, Any], platform_code: str) -> None:
        raise RuntimeError("旧订单演示数据写入已退役；请按新版订单表写入")


class MockOrderDAO:
    """内置 mock 数据源（app/data/mock_data.ORDERS），离线/降级路径。"""

    DEFAULT_PLATFORM = "pdd"

    def query(self, user_id: str, order_id: str, platform_code: str) -> OrderDTO | None:
        # mock 订单全部属于 pdd 平台；其他平台一律视为未命中。
        if platform_code != self.DEFAULT_PLATFORM:
            return None
        from app.data.mock_data import ORDERS

        order = ORDERS.get(order_id)
        if order is None or order.get("user_id") != user_id:
            return None
        return OrderDTO.from_mock(order, platform_code)

    def list_by_user(self, user_id: str, platform_code: str) -> list[OrderDTO]:
        if platform_code != self.DEFAULT_PLATFORM:
            return []
        from app.data.mock_data import ORDERS

        return [
            OrderDTO.from_mock(order, platform_code)
            for order in ORDERS.values()
            if order.get("user_id") == user_id
        ]

    def request_refund(
        self, user_id: str, order_id: str, platform_code: str, reason: str,
    ) -> str:
        if platform_code != self.DEFAULT_PLATFORM:
            return "not_found"
        from app.data.mock_data import ORDERS

        order = ORDERS.get(order_id)
        if order is None or order.get("user_id") != user_id:
            return "not_found"
        if order.get("refund_status") or order.get("status") == "refund_processing":
            return "already_processing"
        order["refund_status"] = "submitted"
        order["refund_reason"] = reason
        order["refund_requested_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if order.get("status") == "pending":
            order["status"] = "refund_processing"
        return "success"


@dataclass(frozen=True)
class OrderQueryResult:
    """适配层查询结果：携带数据源标记，便于观测与排障。"""

    dto: OrderDTO | None
    source: str  # "mysql" | "mock" | "cache"


def build_dao(dsn: str, *, pool_size: int = 10, pool_timeout: float = 5.0) -> OrderDAO:
    """按配置构建 DAO；DSN 为空返回 mock 实现（fail-safe）。"""
    if not dsn:
        return MockOrderDAO()
    return MySqlOrderDAO(dsn, pool_size=pool_size, pool_timeout=pool_timeout)
