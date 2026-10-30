# 文件说明：提供账号注册、密码登录、退出及可撤销登录会话的接口。
"""Account registration, password login and revocable sessions."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.data.auth_store import MySqlAuthStore, UsernameTaken

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_HASH_ALGORITHM = "pbkdf2_sha256"
_HASH_ITERATIONS = 600_000
_USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _HASH_ITERATIONS)
    return f"{_HASH_ALGORITHM}${_HASH_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = encoded.split("$")
        iterations = int(iterations_text)
        if (algorithm != _HASH_ALGORITHM or not 1 <= iterations <= 1_000_000
                or len(salt) > 128 or len(expected) != 64):
            return False
        bytes.fromhex(expected)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, UnicodeError):
        return False


def _user_entries() -> list[tuple[str, str]]:
    entries = []
    for item in settings.api_auth_users.split(","):
        username, sep, encoded = item.strip().partition(":")
        if sep and username.strip() and encoded.strip():
            entries.append((username.strip(), encoded.strip()))
    return entries


def authenticate_user(username: str, password: str) -> str | None:
    """Compatibility path for local mode without a MySQL DSN."""
    matched = None
    for candidate, encoded in _user_entries():
        password_ok = verify_password(password, encoded)
        if hmac.compare_digest(candidate, username):
            matched = candidate if password_ok else None
    return matched


def use_mysql_auth() -> bool:
    backend = settings.api_auth_backend.lower()
    if backend not in {"auto", "mysql", "env"}:
        raise RuntimeError("API_AUTH_BACKEND must be auto, mysql or env")
    return backend == "mysql" or (backend == "auto" and bool(settings.order_db_dsn))


@lru_cache(maxsize=4)
def _store(dsn: str, legacy_users: str) -> MySqlAuthStore:
    return MySqlAuthStore(dsn, legacy_users=legacy_users)


def get_auth_store() -> MySqlAuthStore:
    if not settings.order_db_dsn:
        raise RuntimeError("MySQL authentication requires ORDER_DB_DSN")
    return _store(settings.order_db_dsn, settings.api_auth_users)


def create_access_token(user_id: str, session_id: str | None = None) -> str:
    now = int(time.time())
    claims = {"sub": user_id, "iat": now, "exp": now + settings.api_auth_token_ttl_seconds}
    if session_id is not None:
        claims["sid"] = session_id
    return jwt.encode(claims, settings.api_jwt_secret, algorithm=settings.api_jwt_algorithm)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str


def _issue_login(response: Response, user_id: str, store: MySqlAuthStore | None) -> LoginResponse:
    session_id = secrets.token_hex(32) if store else None
    if store:
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
            seconds=settings.api_auth_token_ttl_seconds)
        store.create_session(user_id, session_id, expires_at)
    access_token = create_access_token(user_id, session_id)
    response.set_cookie(
        settings.auth_cookie_name, access_token,
        max_age=settings.api_auth_token_ttl_seconds, httponly=True,
        secure=settings.auth_cookie_secure, samesite=settings.auth_cookie_samesite, path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name, secrets.token_urlsafe(32),
        max_age=settings.api_auth_token_ttl_seconds, httponly=False,
        secure=settings.auth_cookie_secure, samesite=settings.auth_cookie_samesite, path="/",
    )
    return LoginResponse(access_token=access_token,
                         expires_in=settings.api_auth_token_ttl_seconds, user_id=user_id)


@router.post("/register", response_model=LoginResponse, status_code=201)
def register(request: RegisterRequest, response: Response) -> LoginResponse:
    if not settings.api_registration_enabled:
        raise HTTPException(403, "当前未开放注册")
    if not settings.api_jwt_secret:
        raise HTTPException(503, "服务端未配置 API_JWT_SECRET")
    if not use_mysql_auth():
        raise HTTPException(503, "注册需要 MySQL 账号库")
    if not _USERNAME_PATTERN.fullmatch(request.username):
        raise HTTPException(422, "用户名须以字母开头，只能包含字母、数字和下划线")
    store = get_auth_store()
    try:
        user_id = store.create_user(request.username, hash_password(request.password))
        return _issue_login(response, user_id, store)
    except UsernameTaken as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, response: Response) -> LoginResponse:
    if not settings.api_jwt_secret:
        raise HTTPException(503, "服务端未配置 API_JWT_SECRET")
    if not use_mysql_auth():
        if not _user_entries():
            raise HTTPException(503, "服务端未配置登录用户(API_AUTH_USERS)")
        user_id = authenticate_user(request.username, request.password)
        if user_id is None:
            raise HTTPException(401, "用户名或密码错误")
        return _issue_login(response, user_id, None)

    store = get_auth_store()
    if store.login_locked(request.username):
        raise HTTPException(429, "尝试次数过多，请 15 分钟后重试")
    credential = store.credential(request.username)
    encoded = credential["password_hash"] if credential else _DUMMY_HASH
    password_ok = verify_password(request.password, encoded)
    if not credential or not password_ok or credential["status"] != "active":
        store.record_failure(request.username)
        raise HTTPException(401, "用户名或密码错误")
    store.clear_failures(request.username)
    if encoded.split("$")[1] != str(_HASH_ITERATIONS):
        store.upgrade_hash(credential["user_id"], encoded, hash_password(request.password))
    return _issue_login(response, credential["user_id"], store)


# A fixed, valid hash makes unknown-user timing similar to a wrong password.
_DUMMY_HASH = f"{_HASH_ALGORITHM}${_HASH_ITERATIONS}$0123456789abcdef0123456789abcdef$" + "0" * 64


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    session_id = getattr(request.state, "session_id", None)
    if session_id and use_mysql_auth():
        get_auth_store().revoke_session(request.state.user_id, session_id)
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return {"status": "logged_out"}
