# 文件说明：生成环境认证用户配置所需的 PBKDF2 密码哈希。
"""生成 API_AUTH_USERS 登录用户的 PBKDF2 密码哈希。

用法:
    python scripts/hash_password.py [用户名] [明文密码]

输出形如:
    admin:pbkdf2_sha256$120000$<salt>$<hex>

把整行追加到 .env 的 API_AUTH_USERS（多用户之间用逗号分隔）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.auth import hash_password  # noqa: E402


def main() -> None:
    if len(sys.argv) >= 3:
        username, password = sys.argv[1], sys.argv[2]
    else:
        username = input("用户名: ").strip()
        password = input("密码: ")
    if not username or not password:
        print("用户名和密码不能为空", file=sys.stderr)
        raise SystemExit(1)
    print(f"{username}:{hash_password(password)}")


if __name__ == "__main__":
    main()
