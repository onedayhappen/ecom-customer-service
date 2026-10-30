# 文件说明：签发并校验 API 与 MCP 服务之间绑定用户及调用范围的可信令牌。
"""API 与 MCP Server 间的可信用户上下文 Token。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.config.settings import settings


def issue_mcp_user_context(user_id: str, *, business_scope=None, allowed_tools=None) -> str:
    """签发仅供 MCP Server 使用、短时有效的用户上下文 JWT。"""
    if not settings.mcp_context_jwt_secret:
        raise RuntimeError("MCP_CONTEXT_JWT_SECRET 未配置")
    now = datetime.now(timezone.utc)

    # Signed only by the trusted backend; never returned to the model/browser.
    return jwt.encode(
        {
            "sub": user_id,
            "aud": "ecom-mcp",
            "scope": ["mcp:tools"],
            "iat": now,
            "exp": now + timedelta(seconds=settings.mcp_context_token_ttl_seconds),
            "business_scope": business_scope or {},
            **({'allowed_tools': sorted(allowed_tools)} if allowed_tools is not None else {}),
        },
        settings.mcp_context_jwt_secret,
        algorithm=settings.mcp_context_jwt_algorithm,
    )


def verify_mcp_user_context(token: str) -> str:
    """验证上下文 Token，返回 MCP 工具可安全使用的当前用户 ID。"""
    return verify_mcp_claims(token)['sub']


def verify_mcp_claims(token: str) -> dict:
    claims = jwt.decode(
        token,
        settings.mcp_context_jwt_secret,
        algorithms=[settings.mcp_context_jwt_algorithm],
        audience="ecom-mcp",
        options={"require": ["sub", "exp", "aud"]},
    )
    user_id = claims.get("sub")
    scopes = claims.get("scope", [])
    if not isinstance(user_id, str) or not user_id.strip() or not isinstance(scopes, list) or "mcp:tools" not in scopes:
        raise jwt.PyJWTError("MCP 用户上下文缺少有效权限")
    business = claims.get('business_scope', {})
    if (not isinstance(business, dict) or set(business) - {'platform_code', 'store_id'}
            or any(v is not None and (not isinstance(v, str) or not v.strip()) for v in business.values())):
        raise jwt.PyJWTError('MCP 业务范围无效')
    allowed = claims.get('allowed_tools')
    if allowed is not None and (not isinstance(allowed, list) or any(not isinstance(v, str) for v in allowed)):
        raise jwt.PyJWTError('MCP 工具范围无效')
    return claims
