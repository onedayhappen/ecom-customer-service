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

import json
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
from app.tools.request_context import reset_current_user_id, set_current_user_id
from app.mcp.auth import verify_mcp_user_context
from app.tools.order import query_order as _query_order
from app.tools.product import query_product as _query_product
from app.tools.logistics import query_logistics as _query_logistics
from app.tools.refund import apply_refund as _apply_refund
from app.tools.knowledge import search_knowledge as _search_knowledge
# 订单详情（第15期）：MCP 工具只做编排，SQL 在 app/data 数据访问层。
from app.data.order_service import query_order_detail as _query_order_detail

mcp = FastMCP("ecom-tools", host="127.0.0.1", port=9123)


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
            user_id = verify_mcp_user_context(context_token)
        except Exception:
            await JSONResponse({"detail": "MCP 用户上下文无效或已过期"}, status_code=401)(scope, receive, send)
            return
        user_context = set_current_user_id(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            # ContextVar 是请求级状态，必须复位，不能泄漏到后续用户请求。
            reset_current_user_id(user_context)


@mcp.tool()
def query_order(order_id: str) -> str:
    """根据订单号查询订单详情，包括订单状态、商品信息、金额、物流单号等"""
    # MCP 边界统一返回 JSON 字符串，便于远端 Agent 无歧义地解析工具结果。
    result = _query_order(order_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def query_product(keyword: str) -> str:
    """根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。支持模糊搜索"""
    result = _query_product(keyword)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def query_order_detail(order_id: str, platform_code: str = "pdd") -> str:
    """根据订单号与平台代码查询订单详情（状态、商品、金额、物流信息）。
    归属校验由服务端完成，不要传 user_id。platform_code 仅支持配置的白名单平台。
    数据源：本地订单库（MySQL），未配置时降级内置演示数据。"""
    result = _query_order_detail(order_id, platform_code)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def query_logistics(order_id: str) -> str:
    """根据订单号查询物流轨迹信息，包括快递公司、运单号、运输状态和轨迹事件"""
    result = _query_logistics(order_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def apply_refund(order_id: str, reason: str) -> str:
    """为指定订单申请退款。注意：这是一个敏感操作，调用前应先与用户确认"""
    result = _apply_refund(order_id, reason)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def search_knowledge(query: str, top_k: int = 3) -> str:
    """检索并夕夕的政策与帮助文档（退换货政策、配送说明、会员权益、FAQ）。
    当顾客询问规则、流程、时效、是否支持等政策类问题时使用。
    返回 Top-K 命中片段及来源文档，请基于检索结果回答，不要编造政策。
    """
    result = _search_knowledge(query, top_k=top_k)
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    import uvicorn

    # Streamable HTTP MCP 应用外包认证中间件，再交由 Uvicorn 运行。
    app = MCPAuthenticationMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="127.0.0.1", port=9123)
