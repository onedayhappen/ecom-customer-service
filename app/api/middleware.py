"""全局登录鉴权中间件。

在路由分发前统一校验 Bearer JWT：白名单路径（健康检查、指标、登录）与
CORS 预检直接放行，其余请求必须携带有效令牌，否则返回 401 / 503。

认证身份写入 request.state，端点与存储层继续校验归属：
- 中间件拦在路由之前，未登录请求根本进不了业务端点；
- 端点通过 ``identity(request)`` 读取认证身份，存储层核验会话和事项归属。

实现为纯 ASGI 中间件（不依赖 BaseHTTPMiddleware），避免为每个请求引入额外
协程与 body 缓冲开销；JWT 解码是微秒级 CPU 操作，直接同步执行。
"""

from __future__ import annotations

import hmac
import re
import asyncio
from http.cookies import SimpleCookie

import jwt
from starlette.responses import JSONResponse

from app.config.settings import settings
from app.api.auth import get_auth_store, use_mysql_auth

# 无需登录即可访问的路径（保持与 README 的「除健康检查外均需鉴权」一致，
# 登录端点自身必须公开，否则用户永远无法获得 token）。
PUBLIC_PATHS = {"/health", "/ready", "/metrics", "/api/v1/auth/login", "/api/v1/auth/register"}


class AuthMiddleware:
    """纯 ASGI 中间件：白名单之外的所有 HTTP 请求必须携带有效 Bearer JWT。"""

    def __init__(self, app, public_paths: set[str] | None = None):
        self.app = app
        self.public_paths = public_paths if public_paths is not None else PUBLIC_PATHS

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        method = scope.get("method", "GET")
        # CORS 预检（OPTIONS）与公开路径直接放行。
        if method == "OPTIONS" or path in self.public_paths:
            await self.app(scope, receive, send)
            return

        # 运行时读取 settings：测试可动态改密钥后复用同一个 app 实例。
        if not settings.api_jwt_secret:
            await _respond(scope, receive, send, 503, "服务端未配置 API_JWT_SECRET")
            return

        token = _extract_bearer(scope)
        auth_via_cookie = False
        if token is None:
            cookies = _extract_cookies(scope)
            token = cookies.get(settings.auth_cookie_name)
            auth_via_cookie = bool(token)
        if token is None:
            await _respond(scope, receive, send, 401, "缺少 Bearer Token 或登录 Cookie")
            return
        if auth_via_cookie and method in {"POST", "PUT", "PATCH", "DELETE"}:
            cookies = _extract_cookies(scope)
            headers = dict(scope.get("headers") or [])
            csrf_header = headers.get(b"x-csrf-token", b"").decode("latin-1").strip()
            csrf_cookie = cookies.get(settings.csrf_cookie_name, "")
            if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
                await _respond(scope, receive, send, 403, "CSRF 校验失败")
                return
        try:
            claims = jwt.decode(
                token,
                settings.api_jwt_secret,
                algorithms=[settings.api_jwt_algorithm],
                options={"require": ["sub", "exp"]},
            )
        except jwt.PyJWTError:
            await _respond(scope, receive, send, 401, "Token 无效或已过期")
            return
        user_id = claims.get("sub")
        if not isinstance(user_id, str) or not user_id.strip():
            await _respond(scope, receive, send, 401, "Token 缺少有效用户身份")
            return

        session_id = claims.get("sid")
        if use_mysql_auth():
            if session_id is None and settings.api_allow_legacy_jwt:
                pass
            elif not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{64}", session_id):
                await _respond(scope, receive, send, 401, "Token 缺少有效会话")
                return
            else:
                try:
                    active = await asyncio.to_thread(get_auth_store().session_active, user_id, session_id)
                except Exception:
                    await _respond(scope, receive, send, 503, "会话服务不可用")
                    return
                if not active:
                    await _respond(scope, receive, send, 401, "会话已失效")
                    return

        # 校验通过：把用户身份挂到请求 state，端点可通过 request.state.user_id 读取。
        state = dict(scope.get("state") or {})
        state["user_id"] = user_id
        state["auth_via_cookie"] = auth_via_cookie
        state["token_expires_at"] = claims["exp"]
        state["session_id"] = session_id
        scope["state"] = state
        await self.app(scope, receive, send)


def _extract_bearer(scope) -> str | None:
    """从请求头解析 Bearer token；缺失或格式错误返回 None。"""
    headers = dict(scope.get("headers") or [])
    auth = headers.get(b"authorization", b"")
    if not auth.startswith(b"Bearer "):
        return None
    token = auth[len(b"Bearer "):].decode("latin-1").strip()
    return token or None


def _extract_cookies(scope) -> dict[str, str]:
    """解析 ASGI cookie 请求头，忽略格式错误的条目。"""
    headers = dict(scope.get("headers") or [])
    raw = headers.get(b"cookie", b"").decode("latin-1")
    parsed = SimpleCookie()
    try:
        parsed.load(raw)
    except ValueError:
        return {}
    return {key: morsel.value for key, morsel in parsed.items()}


async def _respond(scope, receive, send, status_code: int, detail: str) -> None:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    await response(scope, receive, send)
