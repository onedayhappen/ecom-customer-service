"""MCP Client：同步封装，通过 Streamable HTTP 连接 MCP Server。

内部使用后台线程运行异步事件循环，对外暴露同步接口，
使得现有的同步 Agent 代码无需改动即可调用 MCP 工具。

1. 前端携带用户 JWT 请求 FastAPI
2. FastAPI 用 API_JWT_SECRET 验证 JWT
   → 验证签名、exp、sub
   → 得到可信 user_id
3. FastAPI 签发短时效 MCP 用户上下文 JWT
   → sub = user_id
   → aud = ecom-mcp
   → scope = ["mcp:tools"]
   → exp = 当前时间 + 300 秒
   → 使用 MCP_CONTEXT_JWT_SECRET 签名
4. MCP Client 发起 Streamable HTTP 请求
   请求头包含：
   Authorization: Bearer <MCP_SERVICE_TOKEN>
   X-MCP-User-Context: <短时用户上下文 JWT>
5. MCP Server 中间件验证 MCP_SERVICE_TOKEN
   → 不正确：401 MCP 服务认证失败
6. MCP Server 验证 X-MCP-User-Context
   → 验签、检查 exp、aud、scope、sub
   → 无效或过期：401 MCP 用户上下文无效或已过期
7. MCP Server 将可信 user_id 写入 ContextVar
   → set_current_user_id(user_id)
8. MCP 工具执行
   → query_order / query_logistics / apply_refund
   → 从 ContextVar 读取 user_id
   → 校验订单是否属于当前用户
9. 请求结束
   → reset_current_user_id(...)
   → 防止身份泄漏到下一次请求
"""

import asyncio
import json
import threading

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.mcp.converter import mcp_tools_to_openai


class MCPClient:
    """同步 MCP 客户端，通过 Streamable HTTP 连接远程 MCP Server。"""

    def __init__(self, server_url: str, service_token: str = "", user_context_token: str = ""):
        self._server_url = server_url
        self._headers = {
            "Authorization": f"Bearer {service_token}",
            "X-MCP-User-Context": user_context_token,
        }
        self._loop: asyncio.AbstractEventLoop | None = None     # 后台线程专属的 asyncio 事件循环
        self._thread: threading.Thread | None = None       # 后台线程本身
        self._session: ClientSession | None = None      # 已完成初始化的 MCP 会话。call_tool() 通过它执行远程工具
        self._connected = threading.Event()     # 线程间同步信号
        self._close_event: asyncio.Event | None = None

    def connect(self) -> list[dict]:
        """连接 MCP Server，发现工具，返回 OpenAI 格式的工具定义列表。"""
        
        # 后台线程独占事件循环，使同步 Agent 不必感知 MCP 的异步连接生命周期。
        self._loop = asyncio.new_event_loop()
        self._close_event = asyncio.Event()
        tool_definitions: list[dict] = []
        error_holder: list[Exception] = []

        def run_loop():
            self._loop.run_until_complete(self._run(tool_definitions, error_holder))

        self._thread = threading.Thread(target=run_loop, daemon=True)
        self._thread.start()
        self._connected.wait(timeout=30)

        if error_holder:
            raise error_holder[0]

        return tool_definitions

    async def _run(self, tool_definitions: list[dict], error_holder: list[Exception]):
        """后台协程：建立连接 → 发现工具 → 保持存活等待调用。"""      
        try:
            # 1. 建立Streamable HTTP长连接
            async with httpx.AsyncClient(headers=self._headers, timeout=30) as http_client:
                async with streamable_http_client(
                    self._server_url, http_client=http_client,
                ) as (read, write, _):
                    # 2. 初始化MCP会话
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        # 3. 拉取服务端全部工具定义
                        tools_result = await session.list_tools()
                        openai_tools = mcp_tools_to_openai(tools_result.tools)
                        tool_definitions.extend(openai_tools)
                        # 4. 发送就绪信号，主线程解除阻塞
                        self._connected.set()
                        # 5. 长连接阻塞等待关闭指令，保持会话存活
                        await self._close_event.wait()
        except Exception as e:
            error_holder.append(e)
            self._connected.set()


    def call_tool(self, name: str, arguments: dict) -> str:
        """调用 MCP 工具，返回 JSON 字符串结果。"""
        if not self._session or not self._loop:
            return json.dumps({
                "success": False,
                "error": "MCP 客户端未连接",
                "error_code": "transient_failure",
            }, ensure_ascii=False)

        # 将协程调度回后台循环，并在同步边界施加超时保护。
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        )
        try:
            result = future.result(timeout=30)
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"MCP 工具调用出错: {e}",
                    "error_code": "transient_failure",
                }, ensure_ascii=False
            )

        if result.isError:
            text = result.content[0].text if result.content else "未知错误"
            return json.dumps({
                "success": False,
                "error": f"工具执行出错: {text}",
                "error_code": "transient_failure",
            }, ensure_ascii=False)

        return result.content[0].text if result.content else "{}"

    def close(self):
        """关闭 MCP 连接，清理后台线程。"""
        if self._close_event and self._loop:
            self._loop.call_soon_threadsafe(self._close_event.set)
        if self._thread:
            self._thread.join(timeout=5)
        self._session = None
        self._loop = None
        self._thread = None
