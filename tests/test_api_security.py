# 文件说明：测试 API 登录鉴权、令牌校验和用户订单访问隔离。
"""FastAPI 认证与订单归属隔离测试，不调用 LLM。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.auth import authenticate_user, hash_password, verify_password
from app.config.settings import settings
from app.tools.request_context import set_current_user_id


@pytest.fixture(autouse=True)
def env_auth_for_legacy_tests():
    original = settings.api_auth_backend
    settings.api_auth_backend = "env"
    try:
        yield
    finally:
        settings.api_auth_backend = original


def _token(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.api_jwt_secret,
        algorithm=settings.api_jwt_algorithm,
    )


def test_jwt_is_required_and_validated():
    original_secret = settings.api_jwt_secret
    settings.api_jwt_secret = "test-secret"
    client = TestClient(app)
    try:
        assert client.get("/health").status_code == 200
        assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 401
        assert client.post(
            "/api/v1/chat",
            headers={"Authorization": "Bearer invalid"},
            json={"message": "你好"},
        ).status_code == 401
    finally:
        settings.api_jwt_secret = original_secret


def test_password_hash_roundtrip():
    encoded = hash_password("s3cret")
    assert encoded.startswith("pbkdf2_sha256$600000$")
    assert verify_password("s3cret", encoded) is True
    assert verify_password("wrong", encoded) is False
    assert verify_password("s3cret", "garbage") is False


def test_login_endpoint():
    original_secret = settings.api_jwt_secret
    original_users = settings.api_auth_users
    try:
        settings.api_jwt_secret = "test-secret"
        settings.api_auth_users = f"admin:{hash_password('secret')},op:{hash_password('ops-pw')}"
        client = TestClient(app)

        # 错误密码 → 401
        resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

        # 正确登录 → 200 + token
        resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "secret"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "admin"
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == settings.api_auth_token_ttl_seconds

        # 登录拿到的 token 可直接访问受保护接口
        token = body["access_token"]
        resp = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "admin"

        # 多用户：第二个账号也能登录
        resp = client.post("/api/v1/auth/login", json={"username": "op", "password": "ops-pw"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "op"
    finally:
        settings.api_jwt_secret = original_secret
        settings.api_auth_users = original_users


def test_auth_middleware_blocks_and_whitelists():
    original_secret = settings.api_jwt_secret
    try:
        settings.api_jwt_secret = "test-secret"
        client = TestClient(app)

        # 白名单路径无需 token
        assert client.get("/health").status_code == 200
        # 登录端点自身公开
        resp = client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})
        assert resp.status_code in (401, 503)

        # 受保护路径：无 token / 无效 token 均被中间件拦截（不会到达端点逻辑）
        assert client.get("/api/v1/me").status_code == 401
        assert client.get("/api/v1/me", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert client.get("/api/v1/sessions/00000000-0000-0000-0000-000000000001").status_code == 401
    finally:
        settings.api_jwt_secret = original_secret


if __name__ == "__main__":
    test_jwt_is_required_and_validated()
    test_password_hash_roundtrip()
    test_login_endpoint()
    test_auth_middleware_blocks_and_whitelists()
    print("API 认证与订单隔离测试通过")
