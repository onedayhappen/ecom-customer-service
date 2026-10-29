"""ToolManager：统一管理本地工具和 MCP 工具。

当 MCP 启用时，通过 Streamable HTTP 获取远端实现，只接受本地目录中登记的工具；
当 MCP 未启用或连接失败时，退回本地工具。目录由类型化核心工具组成。
"""

import json
import logging
from typing import Optional

from app.tools.registry import TOOL_DEFINITIONS as LOCAL_TOOL_DEFINITIONS
from app.tools.registry import execute_tool as local_execute_tool


class ToolManager:
    """聚合本地工具和 MCP 工具，提供统一的工具定义和调度接口。"""

    def __init__(
        self,
        use_mcp: bool = False,
        mcp_server_url: str = "",
        allowed_tools: Optional[set] = None,
        mcp_service_token: str = "",
        mcp_user_context_token: str = "",
        mcp_timeout: float | None = None,
    ):
        self._mcp_client = None
        self._tool_source: dict[str, str] = {}
        self._tool_defs: list[dict] = []
        self._mcp_timeout = mcp_timeout

        # MCP 是可选增强：连接失败不阻断客服主流程，自动使用本地同名实现。
        if use_mcp and mcp_server_url:
            self._init_mcp(mcp_server_url, mcp_service_token, mcp_user_context_token)
        else:
            self._init_local()

        registered = {d["function"]["name"] for d in LOCAL_TOOL_DEFINITIONS}
        from app.tools.selection import global_tools
        registered &= global_tools()
        self._filter_tools(registered if allowed_tools is None else registered.intersection(allowed_tools))

    def _init_local(self):
        """只加载本地工具。"""
        self._tool_defs = list(LOCAL_TOOL_DEFINITIONS)
        for td in self._tool_defs:
            self._tool_source[td["function"]["name"]] = "local"

    def _init_mcp(self, server_url: str, service_token: str, user_context_token: str):
        """连接 MCP Server 加载工具；失败时降级到本地工具。"""
        try:
            from app.mcp import MCPClient

            self._mcp_client = MCPClient(server_url, service_token, user_context_token)
            if self._mcp_timeout is not None:
                self._mcp_client.timeout = min(30, self._mcp_timeout)
            mcp_tools = self._mcp_client.connect()
            logging.getLogger(__name__).info('MCP discovery completed (%d tools)', len(mcp_tools))

            mcp_names = set()
            for td in mcp_tools:
                name = td["function"]["name"]
                from app.tools.catalog import BY_NAME
                if name not in BY_NAME or BY_NAME[name].task_only:
                    continue
                mcp_names.add(name)
                self._tool_source[name] = "mcp"
            self._tool_defs = [td for td in LOCAL_TOOL_DEFINITIONS if td["function"]["name"] in mcp_names]

            # 远端没有暴露的本地工具仍保留，形成“远端优先、当地补齐”的工具集。
            for td in LOCAL_TOOL_DEFINITIONS:
                name = td["function"]["name"]
                if name not in mcp_names:
                    self._tool_defs.append(td)
                    self._tool_source[name] = "local"

        except Exception as e:
            logging.getLogger(__name__).warning('MCP discovery failed (%s); using local tools', type(e).__name__)
            if self._mcp_client:
                self._mcp_client.close()
                self._mcp_client = None
            self._init_local()

    def _filter_tools(self, allowed: set):
        """只保留服务端明确允许的工具。"""
        self._tool_defs = [
            d for d in self._tool_defs
            if d["function"]["name"] in allowed
        ]
        self._tool_source = {
            k: v for k, v in self._tool_source.items()
            if k in allowed
        }

    @property
    def tool_definitions(self) -> list[dict]:
        return self._tool_defs

    @property
    def tool_names(self) -> set[str]:
        return set(self._tool_source)

    @property
    def remote_tool_names(self) -> set[str]:
        return {name for name, source in self._tool_source.items() if source == 'mcp'}

    def filter_tool_definitions(self, names: set[str] | frozenset[str]) -> list[dict]:
        """Return definitions for explicit tool names, still bounded by this manager."""
        selected = set(names).intersection(self.tool_names)
        if not selected:
            return []
        return [
            definition for definition in self._tool_defs
            if definition["function"]["name"] in selected
        ]

    def execute_tool(self, name: str, arguments: dict, *, require_remote=False) -> str:
        """根据工具来源分发调用。"""
        source = self._tool_source.get(name)
        if require_remote and source != 'mcp':
            from app.mcp.client import unavailable
            return unavailable('mcp_remote_retry_unavailable')

        if source == "mcp" and self._mcp_client:
            from app.tools.catalog import BY_NAME
            from pydantic import ValidationError
            try:
                BY_NAME[name].arguments.model_validate(arguments)
            except ValidationError:
                return json.dumps({'success': False, 'error_code': 'validation_error', 'error': '工具参数无效'})
            return self._mcp_client.call_tool(name, arguments)

        if source == "local":
            return local_execute_tool(name, arguments)

        return json.dumps({"success": False, "error_code": "validation_error",
                           "error": f"未知工具: {name}"}, ensure_ascii=False)

    def close(self):
        """清理 MCP 连接。"""
        if self._mcp_client:
            self._mcp_client.close()
            self._mcp_client = None
