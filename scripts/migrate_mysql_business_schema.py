# 文件说明：按登记版本升级 MySQL 业务结构，并检查迁移状态和旧表清理。
"""Apply versioned MySQL business schema and remove retired legacy structures."""

from __future__ import annotations

import argparse
import json
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


VERSIONS = ("001_business_schema", "002_remove_legacy_commerce", "003_core_tools", "004_extended_tools", "005_merchant_store_type", "006_order_statuses", "007_pending_receipt", "008_after_sale_fulfillment", "009_coupon_eligibility", "010_membership_programs", "011_order_changes", "012_local_provider")
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


def _apply_004(conn) -> None:
    path = ROOT / "migrations" / "mysql" / "004_extended_tools.sql"
    with conn.cursor() as cur:
        for statement in path.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                cur.execute(statement.strip())
    conn.commit()


def _apply_005(conn) -> None:
    path = ROOT / "migrations" / "mysql" / "005_merchant_store_type.sql"
    with conn.cursor() as cur:
        if not _column_exists(cur, "merchants", "store_type"):
            cur.execute(path.read_text(encoding="utf-8").strip().rstrip(";"))
    conn.commit()


def _apply_006(conn) -> None:
    path = ROOT / 'migrations' / 'mysql' / '006_order_statuses.sql'
    with conn.cursor() as cur:
        for statement in path.read_text(encoding='utf-8').split(';'):
            statement = statement.strip()
            if not statement:
                continue
            if 'ADD COLUMN status_before_after_sale' in statement and _column_exists(cur, 'orders', 'status_before_after_sale'):
                continue
            constraint = next((name for name in ('chk_orders_status','chk_orders_after_sale_base') if 'ADD CONSTRAINT '+name+' ' in statement), None)
            if constraint:
                cur.execute('SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s',('orders',constraint))
                if cur.fetchone():
                    continue
            cur.execute(statement)
        cur.execute("ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'pending_payment'")
        # Policy order-state names follow the same vocabulary after migration.
        cur.execute('SELECT policy_id,version,rules FROM business_policy_versions')
        import json
        for policy in cur.fetchall():
            rules = json.loads(policy['rules']) if isinstance(policy['rules'],str) else policy['rules']
            if isinstance(rules,dict) and 'completed' in rules.get('allowed_statuses',[]):
                rules['allowed_statuses'] = ['received' if value=='completed' else value for value in rules['allowed_statuses']]
                cur.execute('UPDATE business_policy_versions SET rules=%s WHERE policy_id=%s AND version=%s',(json.dumps(rules,ensure_ascii=False),policy['policy_id'],policy['version']))
    conn.commit()


def _apply_007(conn) -> None:
    import json
    path = ROOT / 'migrations' / 'mysql' / '007_pending_receipt.sql'
    with conn.cursor() as cur:
        for statement in path.read_text(encoding='utf-8').split(';'):
            statement = statement.strip()
            if not statement:
                continue
            if 'DROP CHECK' in statement:
                name = statement.split()[-1]
                cur.execute('SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s',('orders',name))
                if not cur.fetchone():
                    continue
            cur.execute(statement)
        cur.execute('SELECT policy_id,version,rules FROM business_policy_versions')
        for policy in cur.fetchall():
            rules = json.loads(policy['rules']) if isinstance(policy['rules'],str) else policy['rules']
            allowed = rules.get('allowed_statuses',[]) if isinstance(rules,dict) else []
            if any(value in {'fulfilling','shipped'} for value in allowed):
                rules['allowed_statuses'] = list(dict.fromkeys('pending_receipt' if value in {'fulfilling','shipped'} else value for value in allowed))
                cur.execute('UPDATE business_policy_versions SET rules=%s WHERE policy_id=%s AND version=%s',(json.dumps(rules,ensure_ascii=False),policy['policy_id'],policy['version']))
    conn.commit()


def _apply_008(conn) -> None:
    import re
    path = ROOT / 'migrations/mysql/008_after_sale_fulfillment.sql'
    with conn.cursor() as cur:
        for statement in path.read_text(encoding='utf-8').split(';'):
            statement = statement.strip()
            if not statement:
                continue
            col = re.match(r'ALTER TABLE (\w+) ADD COLUMN (\w+)', statement)
            if col and _column_exists(cur, *col.groups()):
                continue
            constraint = re.match(r'ALTER TABLE (\w+) ADD CONSTRAINT (\w+)', statement)
            if constraint:
                cur.execute('SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s',constraint.groups())
                if cur.fetchone():
                    continue
            # Backfill original parcel SKU only after identifying legacy exchange parcels.
            if statement.startswith('UPDATE shipment_items'):
                cur.execute("""UPDATE shipments s JOIN service_work_orders w
                    ON s.shipment_id=JSON_UNQUOTE(JSON_EXTRACT(w.details,'$.replacement_shipment_id'))
                    SET s.shipment_kind='exchange',s.after_sale_id=w.after_sale_id WHERE w.kind='exchange'""")
            cur.execute(statement)
        if not _index_exists(cur,'return_shipments','idx_return_authorization'):
            cur.execute('ALTER TABLE return_shipments ADD KEY idx_return_authorization(authorization_id)')
        cur.execute("""SELECT INDEX_NAME FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='return_shipments' AND NON_UNIQUE=0
            GROUP BY INDEX_NAME HAVING COUNT(*)=1 AND MAX(COLUMN_NAME)='authorization_id'""")
        for row in cur.fetchall():
            cur.execute('ALTER TABLE return_shipments DROP INDEX `'+row['INDEX_NAME'].replace('`','``')+'`')
    conn.commit()


def _apply_009(conn) -> None:
    import re
    path = ROOT / 'migrations/mysql/009_coupon_eligibility.sql'
    with conn.cursor() as cur:
        for statement in path.read_text(encoding='utf-8').split(';'):
            statement = statement.strip()
            if not statement:
                continue
            column = re.match(r'ALTER TABLE (\w+) ADD COLUMN (\w+)', statement)
            if column and _column_exists(cur, *column.groups()):
                continue
            constraint = re.match(r'ALTER TABLE (\w+) ADD CONSTRAINT (\w+)', statement)
            if constraint:
                cur.execute('SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s', constraint.groups())
                if cur.fetchone():
                    continue
            cur.execute(statement)
    conn.commit()


def _apply_010(conn) -> None:
    """Upgrade existing membership records without inventing paid entitlements."""
    import re
    from app.data.membership_store import USER_MEMBERSHIP_COLUMNS
    statements = [s.strip() for s in (ROOT/'migrations/mysql/010_membership_programs.sql').read_text(encoding='utf-8').split(';') if s.strip()]
    with conn.cursor() as cur:
        old = (ROOT/'migrations/mysql/004_extended_tools.sql').read_text(encoding='utf-8')
        for table in ('membership_levels','memberships','points_ledger'):
            cur.execute(re.search(r'CREATE TABLE IF NOT EXISTS '+table+r'\s*\(.*?;',old,re.S)[0])
        cur.execute(statements[0])
        for table, columns in {
            'users': USER_MEMBERSHIP_COLUMNS,
            'membership_levels': {'program_id':'VARCHAR(64) NULL','rank_value':'INT NOT NULL DEFAULT 0','growth_threshold':'BIGINT NOT NULL DEFAULT 0'},
            'memberships': {'membership_id':'VARCHAR(64) NULL','program_id':'VARCHAR(64) NULL',
                'growth_value':'BIGINT NOT NULL DEFAULT 0','evaluated_at':'DATETIME(6) NULL',
                'next_evaluate_at':'DATETIME(6) NULL','assignment_source':"VARCHAR(32) NOT NULL DEFAULT 'legacy_record'",'version':'BIGINT NOT NULL DEFAULT 0'},
        }.items():
            for column, definition in columns.items():
                if not _column_exists(cur,table,column):
                    cur.execute(f'ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}')
        cur.execute("""INSERT IGNORE INTO membership_programs(program_id,platform_code,program_type,name,status,rule_version,evaluation_rules)
            SELECT CONCAT('legacy_',platform_code),platform_code,'growth','原有平台会员','active',1,'{}'
            FROM membership_levels WHERE program_id IS NULL GROUP BY platform_code""")
        cur.execute("UPDATE membership_levels SET program_id=CONCAT('legacy_',platform_code) WHERE program_id IS NULL")
        cur.execute("UPDATE memberships SET program_id=CONCAT('legacy_',platform_code) WHERE program_id IS NULL")
        cur.execute("UPDATE memberships SET membership_id=SHA2(CONCAT(user_id,':',platform_code,':legacy'),256) WHERE membership_id IS NULL")
        # Replace the old level FK before replacing its referenced primary key.
        cur.execute("""SELECT CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS
            WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='memberships' AND REFERENCED_TABLE_NAME='membership_levels'""")
        for row in cur.fetchall():
            if row['CONSTRAINT_NAME'] != 'fk_membership_program_level':
                cur.execute('ALTER TABLE memberships DROP FOREIGN KEY `'+row['CONSTRAINT_NAME'].replace('`','``')+'`')
        if not _index_exists(cur,'memberships','uq_user_member_program'):
            cur.execute('ALTER TABLE memberships ADD UNIQUE KEY uq_user_member_program(user_id,program_id)')
        for table, target in [('membership_levels',['program_id','level_code','version']),('memberships',['membership_id'])]:
            cur.execute("SELECT COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX",(table,))
            if [r['COLUMN_NAME'] for r in cur.fetchall()] != target:
                cur.execute('ALTER TABLE '+table+' DROP PRIMARY KEY, ADD PRIMARY KEY ('+','.join(target)+')')
        cur.execute('ALTER TABLE membership_levels MODIFY program_id VARCHAR(64) NOT NULL')
        cur.execute('ALTER TABLE memberships MODIFY program_id VARCHAR(64) NOT NULL, MODIFY membership_id VARCHAR(64) NOT NULL')
        for table, name, clause in [
            ('membership_levels','fk_level_program','FOREIGN KEY(program_id) REFERENCES membership_programs(program_id)'),
            ('memberships','fk_membership_program_level','FOREIGN KEY(program_id,level_code,level_version) REFERENCES membership_levels(program_id,level_code,version)'),
        ]:
            cur.execute('SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s',(table,name))
            if not cur.fetchone(): cur.execute(f'ALTER TABLE {table} ADD CONSTRAINT {name} {clause}')
        for statement in statements[1:]: cur.execute(statement)
    conn.commit()


def _apply_012(conn):
    # MySQL DDL commits individually: resume safely after any interrupted ALTER.
    with conn.cursor() as cur:
        for statement in (ROOT / 'migrations/mysql/012_local_provider.sql').read_text(encoding='utf-8').split(';'):
            statement=statement.strip()
            if not statement: continue
            if statement.startswith('ALTER TABLE'):
                parts=statement.split()
                if _column_exists(cur,parts[2],parts[5]): continue
            cur.execute(statement)
    conn.commit()


def _apply_011(conn) -> None:
    with conn.cursor() as cur:
        for statement in (ROOT / 'migrations/mysql/011_order_changes.sql').read_text(encoding='utf-8').split(';'):
            if statement.strip(): cur.execute(statement)
        # Version existing local policies; do not invent authority for a new platform.
        cur.execute("SELECT * FROM business_policy_versions WHERE action='cancel_order' AND status='active' AND channel IS NULL")
        for row in cur.fetchall():
            rules = json.loads(row['rules']) if isinstance(row['rules'], str) else row['rules']
            if not rules.get('local_authority'): continue
            states = ['pending_payment', 'paid', 'pending_receipt']
            if rules.get('allowed_statuses') == states: continue
            rules['allowed_statuses'] = states
            cur.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM business_policy_versions WHERE policy_id=%s', (row['policy_id'],))
            version = cur.fetchone()['v']
            cur.execute("UPDATE business_policy_versions SET status='superseded',effective_until=UTC_TIMESTAMP(6) WHERE policy_id=%s AND version=%s", (row['policy_id'], row['version']))
            cur.execute('''INSERT INTO business_policy_versions(policy_id,version,action,platform_code,channel,rules,effective_from,effective_until,source,status)
                VALUES (%s,%s,%s,%s,NULL,%s,UTC_TIMESTAMP(6),%s,%s,'active')''',
                (row['policy_id'], version, row['action'], row['platform_code'], json.dumps(rules), row['effective_until'], 'knowledge/cancel_order.md'))
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
                cur.execute("SELECT version FROM schema_migrations WHERE version IN (" + ",".join(["%s"] * len(VERSIONS)) + ")", VERSIONS)
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
                cur.execute("SELECT version FROM schema_migrations WHERE version IN (" + ",".join(["%s"] * len(VERSIONS)) + ")", VERSIONS)
                applied = {row["version"] for row in cur.fetchall()}
            for version, operation in ((VERSIONS[0], _apply_001), (VERSIONS[1], _apply_002), (VERSIONS[2], _apply_003), (VERSIONS[3], _apply_004), (VERSIONS[4], _apply_005), (VERSIONS[5], _apply_006), (VERSIONS[6], _apply_007), (VERSIONS[7], _apply_008), (VERSIONS[8], _apply_009), (VERSIONS[9], _apply_010), (VERSIONS[10], _apply_011), (VERSIONS[11], _apply_012)):
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
    if not applied:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
