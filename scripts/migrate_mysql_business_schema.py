"""Apply versioned MySQL business schema and remove retired legacy structures."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.data.auth_store import MySqlAuthStore  # noqa: E402
from app.data.order_dao import MySqlOrderDAO, parse_mysql_dsn  # noqa: E402
from app.data.product_dao import MySqlProductDAO  # noqa: E402


VERSIONS = ("001_business_schema", "002_remove_legacy_commerce", "003_core_tools")
SQL_FILE = ROOT / "migrations" / "mysql" / f"{VERSIONS[0]}.sql"
LEGACY_SQL_FILE = ROOT / "migrations" / "mysql" / f"{VERSIONS[1]}.sql"
LEGACY_ORDER_COLUMNS = (
    "user_name", "items", "total", "shipped_at", "delivered_at",
    "tracking_number", "carrier", "estimated_delivery", "refund_status",
    "refund_reason", "refund_requested_at",
)

# Nullable columns deliberately leave unknown historical values unknown.
ADDITIONS = {
    "merchants": {
        "status": "VARCHAR(16) NULL",
        "created_at": "DATETIME(6) NULL",
        "updated_at": "DATETIME(6) NULL",
    },
    "orders": {
        "merchant_id": "VARCHAR(64) NULL",
        "currency": "CHAR(3) NULL",
        "goods_amount": "DECIMAL(12,2) NULL",
        "discount_amount": "DECIMAL(12,2) NULL",
        "shipping_amount": "DECIMAL(12,2) NULL",
        "payable_amount": "DECIMAL(12,2) NULL",
        "paid_amount": "DECIMAL(12,2) NULL",
        "paid_at": "DATETIME(6) NULL",
        "updated_at": "DATETIME(6) NULL",
    },
}


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute("""SELECT 1 FROM information_schema.columns
        WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s""",
        (table, column))
    return cur.fetchone() is not None


def _index_exists(cur, table: str, index: str) -> bool:
    cur.execute("""SELECT 1 FROM information_schema.statistics
        WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s LIMIT 1""",
        (table, index))
    return cur.fetchone() is not None


def _ensure_baseline(conn, dsn: str) -> None:
    # Fresh databases start directly on the target order schema.
    MySqlOrderDAO.ensure_schema(conn)
    MySqlProductDAO.ensure_schema(conn)
    MySqlAuthStore(dsn, legacy_users="").ensure_schema()


def _apply_001(conn) -> None:
    with conn.cursor() as cur:
        for table, columns in ADDITIONS.items():
            for column, definition in columns.items():
                if not _column_exists(cur, table, column):
                    cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")

        # Referenced by the composite FKs on payment, after-sale and invoices.
        if not _index_exists(cur, "orders", "uq_orders_owner"):
            cur.execute("""ALTER TABLE orders ADD UNIQUE KEY uq_orders_owner
                (platform_code, order_id, user_id)""")

        statements = [part.strip() for part in SQL_FILE.read_text(encoding="utf-8").split(";")]
        for statement in statements:
            if statement:
                cur.execute(statement)
    conn.commit()


def _apply_002(conn) -> None:
    with conn.cursor() as cur:
        for statement in LEGACY_SQL_FILE.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                cur.execute(statement.strip())
        for column in LEGACY_ORDER_COLUMNS:
            if _column_exists(cur, "orders", column):
                cur.execute(f"ALTER TABLE orders DROP COLUMN `{column}`")
    conn.commit()


def _apply_003(conn) -> None:
    path = ROOT / "migrations" / "mysql" / "003_core_tools.sql"
    with conn.cursor() as cur:
        for statement in path.read_text(encoding="utf-8-sig").split(";"):
            if statement.strip():
                cur.execute(statement.strip())
    conn.commit()


def migrate(dsn: str, *, check: bool = False) -> bool:
    params = parse_mysql_dsn(dsn)
    if not params.get("database"):
        raise ValueError("DSN must name an existing MySQL database")
    conn = pymysql.connect(**params, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=5, read_timeout=30, write_timeout=30)
    try:
        if check:
            with conn.cursor() as cur:
                cur.execute("""SELECT 1 FROM information_schema.tables
                    WHERE table_schema=DATABASE() AND table_name='schema_migrations'""")
                if cur.fetchone() is None:
                    return False
                cur.execute("SELECT version FROM schema_migrations WHERE version IN (%s,%s,%s)", VERSIONS)
                return {row["version"] for row in cur.fetchall()} == set(VERSIONS)
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 30) AS acquired", ("ecom_mysql_business_schema",))
            if cur.fetchone()["acquired"] != 1:
                raise TimeoutError("MySQL schema migration lock is busy")
        try:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(64) NOT NULL PRIMARY KEY,
                    applied_at DATETIME(6) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
                cur.execute("SELECT version FROM schema_migrations WHERE version IN (%s,%s,%s)", VERSIONS)
                applied = {row["version"] for row in cur.fetchall()}
            for version, operation in ((VERSIONS[0], _apply_001), (VERSIONS[1], _apply_002), (VERSIONS[2], _apply_003)):
                if version in applied:
                    continue
                if version == VERSIONS[0]:
                    _ensure_baseline(conn, dsn)
                operation(conn)
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO schema_migrations(version, applied_at)
                        VALUES (%s, UTC_TIMESTAMP(6))""", (version,))
                conn.commit()
            return True
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", ("ecom_mysql_business_schema",))
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=settings.order_db_dsn,
                        help="mysql://user:password@host:port/database")
    parser.add_argument("--check", action="store_true", help="read migration status only")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("Provide --dsn or ORDER_DB_DSN")
    applied = migrate(args.dsn, check=args.check)
    print(f"MySQL business schema: {'applied' if applied else 'pending'}")


if __name__ == "__main__":
    main()
