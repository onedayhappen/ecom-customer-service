# 文件说明：在隔离 MySQL 中测试账号、密码凭据和可撤销登录会话。
"""Opt-in MySQL integration test: AUTH_TEST_MYSQL_DSN points to an isolated database."""

from __future__ import annotations

import os
import secrets

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api.auth import _store, hash_password
from app.api.main import app
from app.config.settings import settings


@pytest.mark.skipif(not os.getenv("AUTH_TEST_MYSQL_DSN"), reason="isolated MySQL DSN required")
def test_identity_credential_and_revocable_session():
    previous = (settings.api_jwt_secret, settings.order_db_dsn,
                settings.api_auth_backend, settings.api_auth_users)
    legacy_user = "legacy_" + secrets.token_hex(4)
    username = "member_" + secrets.token_hex(4)
    try:
        settings.api_jwt_secret = "integration-test-secret-of-at-least-32-bytes"
        settings.order_db_dsn = os.environ["AUTH_TEST_MYSQL_DSN"]
        settings.api_auth_backend = "mysql"
        settings.api_auth_users = f"{legacy_user}:{hash_password('legacy-password-123')}"
        client = TestClient(app)

        created = client.post("/api/v1/auth/register", json={
            "username": username, "password": "a-strong-password-123",
        })
        assert created.status_code == 201, created.text
        user_id = created.json()["user_id"]
        assert user_id != username
        token = created.json()["access_token"]
        assert len(jwt.decode(token, settings.api_jwt_secret, algorithms=["HS256"])["sid"]) == 64
        assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

        duplicate = client.post("/api/v1/auth/register", json={
            "username": username.upper(), "password": "another-strong-password-123",
        })
        assert duplicate.status_code == 409
        assert client.post("/api/v1/auth/login", json={
            "username": username, "password": "wrong-password",
        }).status_code == 401

        login = client.post("/api/v1/auth/login", json={
            "username": username.upper(), "password": "a-strong-password-123",
        })
        assert login.status_code == 200, login.text
        assert login.json()["user_id"] == user_id
        token = login.json()["access_token"]
        csrf = client.cookies[settings.csrf_cookie_name]
        logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        assert logout.status_code == 200, logout.text
        assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

        imported = client.post("/api/v1/auth/login", json={
            "username": legacy_user, "password": "legacy-password-123",
        })
        assert imported.status_code == 200, imported.text
        assert imported.json()["user_id"] == legacy_user
    finally:
        (settings.api_jwt_secret, settings.order_db_dsn,
         settings.api_auth_backend, settings.api_auth_users) = previous
        _store.cache_clear()
