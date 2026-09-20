"""登录认证：PBKDF2 密码哈希 + 用户名密码换 JWT。

设计要点：
- 用户表来自环境变量 ``API_AUTH_USERS``（``username:hash`` 逗号分隔），不引入
  数据库表，本地 / 演示 / 面试场景零迁移；生产环境可替换为 LDAP / 用户服务。
- 密码哈希用标准库 ``hashlib.pbkdf2_hmac``（120k 迭代 + 随机盐），零新依赖；
  校验用 ``hmac.compare_digest`` 恒定时间比较，防时序攻击。
- 登录签发的 JWT 与外部生成的 JWT 同构（sub + exp），由同一个 ``api_jwt_secret``
  校验，因此现有 ``get_current_user`` 依赖与全局鉴权中间件无需区分 token 来源。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import jwt
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.config.settings import settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_HASH_ALGORITHM = "pbkdf2_sha256"
_HASH_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    """生成 ``pbkdf2_sha256$iterations$salt$hex`` 格式的密码哈希。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _HASH_ITERATIONS,
    )
    return f"{_HASH_ALGORITHM}${_HASH_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """校验明文密码与存储哈希是否匹配（恒定时间比较）。"""
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
    except ValueError:
        return False
    if algorithm != _HASH_ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations),
    )
    return hmac.compare_digest(digest.hex(), expected)


def _user_entries() -> list[tuple[str, str]]:
    """解析 ``API_AUTH_USERS`` -> [(username, hash), ...]，跳过空条目。"""
    entries: list[tuple[str, str]] = []
    for item in settings.api_auth_users.split(","):
        item = item.strip()
        if not item:
            continue
        username, _, encoded = item.partition(":")
        username = username.strip()
        encoded = encoded.strip()
        if username and encoded:
            entries.append((username, encoded))
    return entries


def authenticate_user(username: str, password: str) -> str | None:
    """校验用户名密码；成功返回用户名（作为 JWT sub），失败返回 None。

    对每条用户记录都执行密码校验，避免仅凭响应时间区分「用户不存在」与
    「密码错误」；用户名比较同样使用恒定时间比较。
    """
    matched: str | None = None
    for candidate, encoded in _user_entries():
        password_ok = verify_password(password, encoded)
        if hmac.compare_digest(candidate, username):
            matched = candidate if password_ok else None
    return matched


def create_access_token(user_id: str) -> str:
    """签发与现有 JWT 同构的访问令牌（sub + exp）。"""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "exp": now + settings.api_auth_token_ttl_seconds,
        },
        settings.api_jwt_secret,
        algorithm=settings.api_jwt_algorithm,
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, response: Response) -> LoginResponse:
    """用户名密码登录，写入 HttpOnly Cookie 并保留 Bearer 响应兼容性。"""
    if not settings.api_jwt_secret:
        raise HTTPException(status_code=503, detail="服务端未配置 API_JWT_SECRET")
    if not _user_entries():
        raise HTTPException(status_code=503, detail="服务端未配置登录用户(API_AUTH_USERS)")
    user_id = authenticate_user(request.username, request.password)
    if user_id is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    access_token = create_access_token(user_id)
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=access_token,
        max_age=settings.api_auth_token_ttl_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        max_age=settings.api_auth_token_ttl_seconds,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.api_auth_token_ttl_seconds,
        user_id=user_id,
    )


@router.post("/logout")
def logout(response: Response) -> dict[str, str]:
    """清理浏览器登录 Cookie。"""
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return {"status": "logged_out"}
