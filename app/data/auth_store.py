"""MySQL-backed local identities and revocable login sessions."""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.data.order_dao import MySqlOrderDAO


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS users (
        user_id VARCHAR(64) NOT NULL PRIMARY KEY,
        username VARCHAR(64) NOT NULL,
        username_key VARCHAR(64) NOT NULL UNIQUE,
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        created_at DATETIME(6) NOT NULL,
        updated_at DATETIME(6) NOT NULL,
        CONSTRAINT users_status_check CHECK (status IN ('active','disabled'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS user_credentials (
        user_id VARCHAR(64) NOT NULL PRIMARY KEY,
        password_hash VARCHAR(255) NOT NULL,
        changed_at DATETIME(6) NOT NULL,
        CONSTRAINT user_credentials_user_fk FOREIGN KEY (user_id)
            REFERENCES users(user_id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS auth_sessions (
        session_hash CHAR(64) NOT NULL PRIMARY KEY,
        user_id VARCHAR(64) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        expires_at DATETIME(6) NOT NULL,
        revoked_at DATETIME(6) NULL,
        KEY auth_sessions_user_expiry (user_id,expires_at),
        CONSTRAINT auth_sessions_user_fk FOREIGN KEY (user_id)
            REFERENCES users(user_id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS auth_login_attempts (
        username_key VARCHAR(64) NOT NULL PRIMARY KEY,
        attempts INT NOT NULL,
        window_started_at DATETIME(6) NOT NULL,
        locked_until DATETIME(6) NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


class UsernameTaken(ValueError):
    pass


def utc_now() -> datetime:
    """All DATETIME columns in this module contain UTC values."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("ascii")).hexdigest()


def username_key(username: str) -> str:
    return username.strip().casefold()


class MySqlAuthStore(MySqlOrderDAO):
    def __init__(self, dsn: str, *, legacy_users: str = ""):
        super().__init__(dsn)
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._legacy_users = legacy_users

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connection() as conn, conn.cursor() as cur:
                for statement in SCHEMA:
                    cur.execute(statement)
                self._import_legacy(cur)
            self._schema_ready = True

    def _import_legacy(self, cur) -> None:
        """Import configured demo identities once without replacing database accounts."""
        at = utc_now()
        for entry in self._legacy_users.split(","):
            username, separator, encoded = entry.strip().partition(":")
            username, encoded = username.strip(), encoded.strip()
            if not separator or not username or not encoded:
                continue
            key = username_key(username)
            cur.execute("SELECT user_id FROM users WHERE username_key=%s", (key,))
            existing = cur.fetchone()
            if existing and existing["user_id"] != username:
                raise RuntimeError("旧账号与已注册账号重名，请先处理账号映射")
            if not existing:
                cur.execute("""INSERT INTO users
                    (user_id,username,username_key,status,created_at,updated_at)
                    VALUES (%s,%s,%s,'active',%s,%s)""",
                    (username, username, key, at, at))
            cur.execute("""INSERT INTO user_credentials(user_id,password_hash,changed_at)
                VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE user_id=user_id""",
                (username, encoded, at))

    def create_user(self, username: str, password_hash: str) -> str:
        self.ensure_schema()
        user_id, at = uuid4().hex, utc_now()
        try:
            with self._connection() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO users
                    (user_id,username,username_key,status,created_at,updated_at)
                    VALUES (%s,%s,%s,'active',%s,%s)""",
                    (user_id, username, username_key(username), at, at))
                cur.execute("""INSERT INTO user_credentials(user_id,password_hash,changed_at)
                    VALUES (%s,%s,%s)""", (user_id, password_hash, at))
        except Exception as exc:
            # PyMySQL is optional in mock-only environments.
            if getattr(exc, "args", [None])[0] == 1062:
                raise UsernameTaken("用户名已被使用") from exc
            raise
        return user_id

    def credential(self, username: str) -> dict | None:
        self.ensure_schema()
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT u.user_id,u.status,c.password_hash FROM users u
                JOIN user_credentials c ON c.user_id=u.user_id
                WHERE u.username_key=%s""", (username_key(username),))
            return cur.fetchone()

    def upgrade_hash(self, user_id: str, previous_hash: str, new_hash: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE user_credentials SET password_hash=%s,changed_at=%s
                WHERE user_id=%s AND password_hash=%s""",
                (new_hash, utc_now(), user_id, previous_hash))

    def login_locked(self, username: str) -> bool:
        self.ensure_schema()
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT locked_until FROM auth_login_attempts WHERE username_key=%s",
                        (username_key(username),))
            row = cur.fetchone()
            return bool(row and row["locked_until"] and row["locked_until"] > utc_now())

    def record_failure(self, username: str) -> None:
        at, key = utc_now(), username_key(username)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO auth_login_attempts
                (username_key,attempts,window_started_at,locked_until)
                VALUES (%s,0,%s,NULL)
                ON DUPLICATE KEY UPDATE username_key=username_key""", (key, at))
            cur.execute("SELECT attempts,window_started_at FROM auth_login_attempts "
                        "WHERE username_key=%s FOR UPDATE", (key,))
            row = cur.fetchone()
            fresh = at - row["window_started_at"] >= timedelta(minutes=15)
            attempts = 1 if fresh else row["attempts"] + 1
            cur.execute("""UPDATE auth_login_attempts SET attempts=%s,
                window_started_at=%s,locked_until=%s WHERE username_key=%s""",
                (attempts, at if fresh else row["window_started_at"],
                 at + timedelta(minutes=15) if attempts >= 5 else None, key))

    def clear_failures(self, username: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM auth_login_attempts WHERE username_key=%s",
                        (username_key(username),))

    def create_session(self, user_id: str, session_id: str, expires_at: datetime) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO auth_sessions
                (session_hash,user_id,created_at,expires_at)
                VALUES (%s,%s,%s,%s)""",
                (session_hash(session_id), user_id, utc_now(), expires_at))

    def session_active(self, user_id: str, session_id: str) -> bool:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM auth_sessions s JOIN users u ON u.user_id=s.user_id
                WHERE s.session_hash=%s AND s.user_id=%s AND s.expires_at>%s
                AND s.revoked_at IS NULL AND u.status='active'""",
                (session_hash(session_id), user_id, utc_now()))
            return cur.fetchone() is not None

    def revoke_session(self, user_id: str, session_id: str) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE auth_sessions SET revoked_at=%s
                WHERE session_hash=%s AND user_id=%s AND revoked_at IS NULL""",
                (utc_now(), session_hash(session_id), user_id))
