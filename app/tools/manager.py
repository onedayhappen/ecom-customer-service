"""ToolManager：统一管理本地工具和 MCP 工具。

当 MCP 启用时，通过 Streamable HTTP 获取远端实现，只接受本地目录中登记的工具；
当 MCP 未启用或连接失败时，退回本地工具。目录由类型化核心工具组成。
"""

import json
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
    ):
        self._mcp_client = None
        self._tool_source: dict[str, str] = {}
        self._tool_defs: list[dict] = []

        # MCP 是可选增强：连接失败不阻断客服主流程，自动使用本地同名实现。
        if use_mcp and mcp_server_url:
            self._init_mcp(mcp_server_url, mcp_service_token, mcp_user_context_token)
        else:
            self._init_local()

        registered = {d["function"]["name"] for d in LOCAL_TOOL_DEFINITIONS}
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
            mcp_tools = self._mcp_client.connect()
            print(f"🔗 [MCP] 已连接 {server_url}，发现 {len(mcp_tools)} 个工具")

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
            print(f"⚠️  [MCP] 连接失败 ({e})，降级使用本地工具")
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

    def filter_tool_definitions(self, names: set[str] | frozenset[str]) -> list[dict]:
        """Return definitions for explicit tool names, still bounded by this manager."""
        selected = set(names).intersection(self.tool_names)
        if not selected:
            return []
        return [
            definition for definition in self._tool_defs
            if definition["function"]["name"] in selected
        ]

    def execute_tool(self, name: str, arguments: dict) -> str:
        """根据工具来源分发调用。"""
        source = self._tool_source.get(name)

        if source == "mcp" and self._mcp_client:
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
