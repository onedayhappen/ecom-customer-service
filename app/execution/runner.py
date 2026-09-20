"""工具执行器：统一封装工具调用，并对未实现能力显式降级。

Workflow 脚本通过 :class:`ToolRunner` 调用工具，而不是直接 import 各工具函数，
这样执行层与 `tools/registry.py` 解耦：测试可注入 fake 执行函数；未实现的
能力（``blocked``）返回结构化结果而非抛异常，Workflow 据此走降级路径。

异步入口 ``run_async`` 把同步工具调用包进 ``asyncio.to_thread``，
让事件循环在工具执行（DB / RAG 等 I/O）期间保持响应。

埋点（第12期）：工具的实际执行统一委托给
:class:`~app.observability.mcp_client.McpToolClient` —— 埋点全部在客户端
内部（trace span + 入参脱敏审计 + 指标），执行器不感知观测细节；
未注入 client 时自动创建默认客户端。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from app.data_plan.capabilities import IMPLEMENTED_CAPABILITIES

# 执行函数签名：execute(name, arguments) -> str | dict（JSON 字符串或 dict）
ToolExecutor = Callable[[str, dict[str, Any]], str | dict]


class ToolRunner:
    """按能力目录裁剪的工具执行入口。"""

    def __init__(
        self,
        execute: ToolExecutor | None = None,
        available_tools: set[str] | frozenset[str] | None = None,
        mutation_guard=None,
        mutation_intent: str = "",
        confirmation_token: str | None = None,
        client=None,
    ):
        if execute is None:
            # 默认走 registry 的本地分发（避免循环 import，延迟加载）
            from app.tools.registry import execute_tool

            execute = execute_tool
        self._execute = execute
        if client is None:
            from app.config.settings import settings
            from app.observability.mcp_client import McpToolClient

            client = McpToolClient(execute=execute, retries=settings.tool_retries)
        self._client = client
        # mutation guard 收到的是「观测化」的执行函数：写操作同样走埋点
        self._observed_execute: ToolExecutor = client.call_tool
        self.available: frozenset[str] = frozenset(
            available_tools if available_tools is not None else IMPLEMENTED_CAPABILITIES
        )
        self.mutation_guard = mutation_guard
        self.mutation_intent = mutation_intent
        self.confirmation_token = confirmation_token

    def request_mutation_confirmation(self, name: str, arguments: dict[str, Any]) -> str | None:
        if self.mutation_guard is None:
            return None
        return self.mutation_guard.request_confirmation(self.mutation_intent, name, arguments)

    def is_available(self, name: str) -> bool:
        return name in self.available

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行一个工具，返回结构化 dict；未实现能力返回 ``{"blocked": True, ...}``。"""
        args = arguments or {}
        if name not in self.available:
            return {
                "success": False,
                "blocked": True,
                "name": name,
                "error": f"工具 {name} 尚未实现",
            }
        if self.mutation_guard is not None:
            from app.data_plan.capabilities import MUTATING_CAPABILITIES
            if name in MUTATING_CAPABILITIES:
                outcome = self.mutation_guard.execute(
                    self.mutation_intent, name, args, self.confirmation_token,
                    self._observed_execute,
                )
                result = dict(outcome.result)
                if outcome.replayed:
                    result["replayed"] = True
                return result
        try:
            raw = self._observed_execute(name, args)
            if isinstance(raw, str):
                return json.loads(raw)
            return raw if isinstance(raw, dict) else {"success": True, "value": raw}
        except Exception as exc:  # noqa: BLE001 - 工具异常统一收敛为结构化结果
            return {"success": False, "name": name, "error": f"工具执行出错: {exc}"}

    async def run_async(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """异步执行一个工具：同步工具调用包进 ``asyncio.to_thread`` 不阻塞事件循环。

        逻辑与 ``run`` 完全一致，区别仅在 I/O 调用走线程池、事件循环保持响应。
        """
        args = arguments or {}
        if name not in self.available:
            return {
                "success": False,
                "blocked": True,
                "name": name,
                "error": f"工具 {name} 尚未实现",
            }
        if self.mutation_guard is not None:
            from app.data_plan.capabilities import MUTATING_CAPABILITIES
            if name in MUTATING_CAPABILITIES:
                outcome = await asyncio.to_thread(
                    self.mutation_guard.execute,
                    self.mutation_intent, name, args, self.confirmation_token,
                    self._observed_execute,
                )
                result = dict(outcome.result)
                if outcome.replayed:
                    result["replayed"] = True
                return result
        try:
            raw = await self._client.call_tool_async(name, args)
            if isinstance(raw, str):
                return json.loads(raw)
            return raw if isinstance(raw, dict) else {"success": True, "value": raw}
        except Exception as exc:  # noqa: BLE001 - 工具异常统一收敛为结构化结果
            return {"success": False, "name": name, "error": f"工具执行出错: {exc}"}
