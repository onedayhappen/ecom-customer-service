"""商品 DAO：公开目录数据（无用户归属），支持全量加载与模糊搜索。

旧商品工具已移除；本层保留 MySQL 数据访问和离线测试数据源。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# pymysql 为可选依赖（仅 MySqlProductDAO 需要）：mock 模式（DSN 未配置）零外部依赖。
try:  # pragma: no cover - 环境依赖
    import pymysql
    import pymysql.cursors
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]

from app.data.dto import ProductDTO
from app.data.order_dao import parse_mysql_dsn

PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id   VARCHAR(64) NOT NULL,
    name          VARCHAR(128) NOT NULL,
    category      VARCHAR(32) NOT NULL DEFAULT '',
    description   VARCHAR(512) NOT NULL DEFAULT '',
    PRIMARY KEY (merchant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS products (
    product_id   VARCHAR(64)   NOT NULL,
    merchant_id  VARCHAR(64)   NOT NULL DEFAULT '',
    name         VARCHAR(128)  NOT NULL,
    category     VARCHAR(32)   NOT NULL DEFAULT '',
    price        DECIMAL(12,2) NOT NULL DEFAULT 0,
    stock        INT           NOT NULL DEFAULT 0,
    description  VARCHAR(512)  NOT NULL DEFAULT '',
    specs        JSON          NULL,
    PRIMARY KEY (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

PRODUCTS_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS merchants (
        merchant_id   VARCHAR(64) NOT NULL,
        name          VARCHAR(128) NOT NULL,
        category      VARCHAR(32) NOT NULL DEFAULT '',
        description   VARCHAR(512) NOT NULL DEFAULT '',
        PRIMARY KEY (merchant_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        product_id   VARCHAR(64)   NOT NULL,
        merchant_id  VARCHAR(64)   NOT NULL DEFAULT '',
        name         VARCHAR(128)  NOT NULL,
        category     VARCHAR(32)   NOT NULL DEFAULT '',
        price        DECIMAL(12,2) NOT NULL DEFAULT 0,
        stock        INT           NOT NULL DEFAULT 0,
        description  VARCHAR(512)  NOT NULL DEFAULT '',
        specs        JSON          NULL,
        PRIMARY KEY (product_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


@runtime_checkable
class ProductDAO(Protocol):
    def list_all(self) -> list[ProductDTO]: ...


class MySqlProductDAO:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def _connect(self) -> Any:
        if pymysql is None:
            raise RuntimeError("pymysql 未安装：请先 `pip install pymysql`（MySqlProductDAO 需要）")
        return pymysql.connect(
            **parse_mysql_dsn(self._dsn),
            charset="utf8mb4",
            connect_timeout=3,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def list_all(self) -> list[ProductDTO]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM products")
                rows = cur.fetchall()
        return [ProductDTO.from_row(row) for row in rows]

    @staticmethod
    def ensure_schema(conn: Any) -> None:
        with conn.cursor() as cur:
            # MySQL 默认不允许一次 execute 执行多条 DDL；逐条执行也便于
            # 定位具体失败的表，并兼容未开启 multi-statements 的连接池。
            for statement in PRODUCTS_DDL_STATEMENTS:
                cur.execute(statement)
            # MySQL 8.4 does not support ``ADD COLUMN IF NOT EXISTS``.  Keep
            # the migration idempotent by checking the existing table first.
            cur.execute("SHOW COLUMNS FROM products LIKE 'merchant_id'")
            if cur.fetchone() is None:
                cur.execute(
                    "ALTER TABLE products ADD COLUMN merchant_id VARCHAR(64) "
                    "NOT NULL DEFAULT '' AFTER product_id"
                )
        conn.commit()

    @staticmethod
    def upsert(conn: Any, product: dict[str, Any]) -> None:
        import json

        specs = json.dumps(product.get("specs") or {}, ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO products ("
                "  product_id, merchant_id, name, category, price, stock, description, specs"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name)",
                (
                    product["product_id"], product.get("merchant_id") or "",
                    product.get("name") or "",
                    product.get("category") or "", float(product.get("price") or 0),
                    int(product.get("stock") or 0),
                    product.get("description") or "", specs,
                ),
            )
        conn.commit()


class MockProductDAO:
    """内置 mock 商品目录。"""

    def list_all(self) -> list[ProductDTO]:
        from app.data.mock_data import PRODUCTS

        return [ProductDTO.from_mock(product) for product in PRODUCTS.values()]


__all__ = ["ProductDAO", "MySqlProductDAO", "MockProductDAO", "PRODUCTS_DDL", "PRODUCTS_DDL_STATEMENTS", "parse_mysql_dsn"]
