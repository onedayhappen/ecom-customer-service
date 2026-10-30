# 文件说明：在受控后台事件循环中发现并调用 MCP 只读工具，处理超时与关闭。
"""Synchronous MCP reads with an owned background loop and bounded cleanup."""
import asyncio
import json
import threading

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from app.mcp.converter import mcp_tools_to_openai


def unavailable(code):
    # Remote errors may contain URLs, credentials or business payloads.
    return json.dumps({'success': False, 'error_code': 'transient_failure', 'error': code})


class MCPClient:
    def __init__(self, server_url: str, service_token: str = "", user_context_token: str = ""):
        self._server_url = server_url
        self._headers = {'Authorization': f'Bearer {service_token}', 'X-MCP-User-Context': user_context_token}
        self._loop = None
        self._thread = None
        self._session = None
        self._runner = None
        self._close_event = None
        self._connected = threading.Event()
        self._closing = threading.Event()
        self.timeout = 30

    def connect(self) -> list[dict]:
        if self._thread is not None or self._closing.is_set():
            raise RuntimeError('MCP client cannot be reused across connections')
        self._loop = asyncio.new_event_loop()
        tools, errors = [], []
        def run_loop():
            loop = self._loop
            asyncio.set_event_loop(loop)
            try:
                self._runner = loop.create_task(self._run(tools, errors))
                loop.run_until_complete(self._runner)
            finally:
                self._session = None
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
                self._connected.set()
        self._thread = threading.Thread(target=run_loop, daemon=True, name='mcp-client')
        self._thread.start()
        if not self._connected.wait(timeout=self.timeout):
            self.close()
            raise TimeoutError('MCP discovery timed out')
        if errors:
            self.close()
            raise errors[0]
        if self._session is None:
            self.close()
            raise ConnectionError('MCP connection closed during discovery')
        return tools

    async def _run(self, tools, errors):
        self._close_event = asyncio.Event()
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=self.timeout, trust_env=False) as http_client:
                async with streamable_http_client(self._server_url, http_client=http_client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        discovered = await session.list_tools()
                        tools.extend(mcp_tools_to_openai(discovered.tools))
                        self._session = session
                        self._connected.set()
                        if not self._closing.is_set():
                            await self._close_event.wait()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            errors.append(exc)
        finally:
            self._session = None
            self._connected.set()

    def call_tool(self, name: str, arguments: dict) -> str:
        if not self._session or not self._loop or not self._loop.is_running() or self._closing.is_set():
            return unavailable('mcp_not_connected')
        async def call():
            return await self._session.call_tool(name, arguments)
        coroutine = call()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError:
            coroutine.close()
            return unavailable('mcp_connection_closed')
        try:
            result = future.result(timeout=self.timeout)
        except Exception:
            future.cancel()
            # The request may already have been executed. Never retry locally.
            return unavailable('mcp_call_failed_or_unknown')
        if result.isError:
            return unavailable('mcp_tool_error')
        if isinstance(result.structuredContent, dict):
            return json.dumps(result.structuredContent, ensure_ascii=False)
        for block in result.content:
            if getattr(block, 'type', None) == 'text':
                try:
                    if isinstance(json.loads(block.text), dict):
                        return block.text
                except (ValueError, TypeError):
                    pass
        return unavailable('mcp_invalid_result')

    def close(self):
        self._closing.set()
        loop, thread = self._loop, self._thread
        def signal_close():
            if self._close_event:
                self._close_event.set()
            if self._session is None and self._runner and not self._runner.done():
                self._runner.cancel()
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(signal_close)
            except RuntimeError:
                pass  # Loop already finished cleanup.
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive() and loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._runner.cancel)
                except RuntimeError:
                    pass
                thread.join(timeout=5)
        # Keep ownership of a still-closing thread; do not discard its loop.
        if not thread or not thread.is_alive():
            self._session = self._loop = self._thread = self._runner = None
