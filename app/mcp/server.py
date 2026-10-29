"""MCP 服务发起 HTTP 请求
→ 进入本中间件
→ 判断是 HTTP 请求
→ 校验 Authorization（MCP 服务令牌）
→ 校验 X-MCP-User-Context，解析 user_id
→ ContextVar 设置当前用户
→ 进入接口业务代码（内部可以无参读取当前 user_id）
→ 接口处理完毕 / 接口抛出异常
→ finally 强制恢复 ContextVar 到原始状态
→ 返回响应
"""

import hmac
import sys
from pathlib import Path

# 允许直接以脚本方式启动时仍能导入仓库内的 app 包。
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config.settings import settings
from app.tools.request_context import (reset_current_user_id, set_current_user_id,
    get_remote_tool_context, set_remote_tool_context, reset_remote_tool_context)
from app.mcp.auth import verify_mcp_claims
# Each request must inherit its own authenticated identity, not the context of
# the HTTP request that happened to create a persistent MCP server session.
mcp = FastMCP("ecom-tools", host="127.0.0.1", port=9123, stateless_http=True, json_response=True)


from mcp import types
from app.tools.catalog import TOOLS, BY_NAME
from app.tools.registry import execute_tool
import asyncio
import json

@mcp._mcp_server.list_tools()
async def list_business_tools():
    # Standalone MCP carries identity, but no durable task/confirmation context.
    from app.tools.selection import global_tools
    allowed = global_tools()
    remote = get_remote_tool_context()
    if remote and remote.get('allowed_tools') is not None:
        allowed = allowed.intersection(remote['allowed_tools'])
    return [types.Tool(name=t.name, description=t.description, inputSchema=t.arguments.model_json_schema(),
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
        for t in TOOLS if not t.task_only and t.name in allowed]

@mcp._mcp_server.call_tool()
async def call_business_tool(name: str, arguments: dict):
    remote = get_remote_tool_context()
    if (name not in BY_NAME or BY_NAME[name].task_only
            or remote and remote.get('allowed_tools') is not None and name not in remote['allowed_tools']):
        from app.services.commerce.common import BusinessError, failure
        return failure(BusinessError('unsupported_capability', '此工具需要可信事项执行器'))
    return json.loads(await asyncio.to_thread(execute_tool, name, arguments))


class MCPAuthenticationMiddleware:
    """这是一个 ASGI 中间件。所有 HTTP 请求到达 MCP 协议处理层之前，先经过它。它验证两种东西：
        Authorization → 调用 MCP 的服务是不是可信 Agent API;
        X-MCP-User-Context → 当前请求代表哪个已认证用户"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:

        # 只处理 HTTP 请求；websocket、lifespan 启动信号等其他类型请求，直接放行，不做拦截
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        expected = f"Bearer {settings.mcp_service_token}"   # 构造预期的 Authorization 完整值
        context_token = headers.get("x-mcp-user-context", "")   # 自定义请求头，承载 MCP 侧传递的用户上下文令牌

        # 第一层鉴权：MCP服务认证。1、要求请求头 Authorization: Bearer {mcp_service_token} 2、使用hmac.compare_digest防时序攻击，禁止直接==字符串对比
        if not settings.mcp_service_token or not hmac.compare_digest(headers.get("authorization", ""), expected):
            await JSONResponse({"detail": "MCP 服务认证失败"}, status_code=401)(scope, receive, send)
            return

        # 第二层鉴权：解析真实操作用户
        try:
            claims = verify_mcp_claims(context_token)
        except Exception:
            await JSONResponse({"detail": "MCP 用户上下文无效或已过期"}, status_code=401)(scope, receive, send)
            return
        from app.tasks.candidates import BusinessScope
        user_context = set_current_user_id(claims['sub'])
        tool_context = set_remote_tool_context(dict(
            business_scope=BusinessScope(**claims.get('business_scope', {})),
            allowed_tools=claims.get('allowed_tools')))
        try:
            await self.app(scope, receive, send)
        finally:
            # ContextVar 是请求级状态，必须复位，不能泄漏到后续用户请求。
            reset_current_user_id(user_context)
            reset_remote_tool_context(tool_context)


if __name__ == "__main__":
    import uvicorn

    # Streamable HTTP MCP 应用外包认证中间件，再交由 Uvicorn 运行。
    app = MCPAuthenticationMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="127.0.0.1", port=9123)
