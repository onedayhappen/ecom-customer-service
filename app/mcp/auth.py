"""API 与 MCP Server 间的可信用户上下文 Token。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.config.settings import settings


def issue_mcp_user_context(user_id: str) -> str:
    """签发仅供 MCP Server 使用、短时有效的用户上下文 JWT。"""
    if not settings.mcp_context_jwt_secret:
        raise RuntimeError("MCP_CONTEXT_JWT_SECRET 未配置")
    now = datetime.now(timezone.utc)

    # jwt.encode():后端生成 JWT 字符串（签发令牌），传给前端
    return jwt.encode(
        {
            "sub": user_id,
            "aud": "ecom-mcp",
            "scope": ["mcp:tools"],
            "iat": now,
            "exp": now + timedelta(seconds=settings.mcp_context_token_ttl_seconds),
        },
        settings.mcp_context_jwt_secret,
        algorithm=settings.mcp_context_jwt_algorithm,
    )


def verify_mcp_user_context(token: str) -> str:
    """验证上下文 Token，返回 MCP 工具可安全使用的当前用户 ID。"""
    claims = jwt.decode(
        token,
        settings.mcp_context_jwt_secret,
        algorithms=[settings.mcp_context_jwt_algorithm],
        audience="ecom-mcp",
        options={"require": ["sub", "exp", "aud"]},
    )
    user_id = claims.get("sub")
    scopes = claims.get("scope", [])
    if not isinstance(user_id, str) or not user_id.strip() or "mcp:tools" not in scopes:
        raise jwt.PyJWTError("MCP 用户上下文缺少有效权限")
    return user_id
