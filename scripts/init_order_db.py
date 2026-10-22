"""Compatibility entry point: apply the current MySQL business schema.

Historical demo orders and logistics are no longer seeded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from scripts.migrate_mysql_business_schema import migrate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=settings.order_db_dsn or "")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("请配置 ORDER_DB_DSN 或传入 --dsn")
    migrate(args.dsn)
    print("MySQL 业务表结构已就绪；未写入演示数据。")


if __name__ == "__main__":
    main()
