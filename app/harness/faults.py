"""异常注入（Harness 核心能力之一）：向 MCP 工具层动态注入各类故障。

解决「工具调用不稳定、各种异常分支」的测试问题——不是简单 mock 固定返回，
而是提供一层**代理 Mock 层**（:class:`FaultInjector`），可动态向工具执行
注入故障，自动复现线上抖动。

可注入故障类型（:class:`FaultKind`）：

- 网络类：``timeout`` 超时、``connection_error`` 连接失败、``jitter``
  随机抖动（按 ``ratio`` 部分请求成功、部分失败）；
- 业务返回异常：``invalid_json`` 非法 JSON、``missing_field`` 缺失字段、
  ``bad_business_code`` 错误业务码、``dirty_data`` 脏数据；
- 限流报错：``rate_limit`` 工具返回 429 限流；
- 延迟注入：``delay`` 故意拉长响应时间，测试 Agent 超时处理逻辑。

接入方式（与第12期 McpToolClient 无缝衔接）：FaultInjector 本身就是
工具执行函数（``execute(name, arguments)``），把它作为
``McpToolClient(execute=injector)`` 的底层执行层即可——

- 测试环境：Agent 所有工具调用经过 Harness 代理（注入故障）；
- 生产环境：直接走真实客户端，**零侵入**（不注入时原样透传）。

注入的故障结果与 ``app.reliability.failure_policy.FailureClassifier``
的判定约定对齐：限流/超时返回 ``error_code="transient_failure"``、
业务拒绝返回 ``error_code="business_rejection"``，让 ReActPolicy 能正确
分类并触发透明重试 / 幂等检查 / 转人工等既有防护路径。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

#: 工具执行函数签名：execute(name, arguments) -> str | dict（JSON 字符串或 dict）
ToolExecutor = Callable[[str, dict[str, Any]], Any]


class FaultKind(str, Enum):
    """可注入的故障类型。"""

    TIMEOUT = "timeout"                    # 抛 TimeoutError（网络超时）
    CONNECTION_ERROR = "connection_error"  # 抛 ConnectionError（连接失败）
    JITTER = "jitter"                      # 按 ratio 随机注入（部分成功部分失败）
    INVALID_JSON = "invalid_json"          # 返回非法 JSON 字符串
    MISSING_FIELD = "missing_field"        # 合法 JSON 但缺失关键字段
    BAD_BUSINESS_CODE = "bad_business_code"  # 错误业务码（business_rejection）
    DIRTY_DATA = "dirty_data"              # 字段类型错乱的脏数据
    RATE_LIMIT = "rate_limit"              # 429 限流（transient_failure）
    DELAY = "delay"                        # 延迟注入（sleep 后正常返回）


@dataclass
class FaultSpec:
    """一次故障注入的规格。

    :param kind: 故障类型。
    :param ratio: 触发概率（0~1）。JITTER 语义 = 以该概率注入故障；
        其他类型配 ``ratio<1`` 同样变成「部分请求失败」的抖动。
    :param delay_ms: 延迟注入时长（DELAY 必填；其他类型可配 0）。
    :param field_name: MISSING_FIELD 要删除的字段名 / DIRTY_DATA 要改脏的字段名。
    :param message: 覆盖注入的错误文本（缺省按类型生成）。
    :param tools: 命中工具名正则（空元组 = 全部工具）。
    """

    kind: FaultKind               # 故障类型，必填
    ratio: float = 1.0            # 触发概率0~1，1代表每次命中都触发故障
    delay_ms: int = 0             # delay故障专用，毫秒延迟时长
    field_name: str = ""          # missing_field / dirty_data 要操作的字段名
    message: str = ""             # 自定义错误提示文本，为空用内置默认
    tools: tuple[str, ...] = field(default_factory=tuple) # 正则匹配工具名，空=全部工具生效


class FaultInjector:
    """工具执行代理：按 FaultSpec 向工具调用注入故障，未命中则原样透传。

    用法::

        injector = FaultInjector(spec=FaultSpec(FaultKind.TIMEOUT, tools=(r"query_.*",)))
        client = McpToolClient(execute=injector)          # 测试环境
        result = client.call_tool("query_order", {...})   # 命中 → 抛 TimeoutError

    生产环境不构造 injector（或 ratio=0），工具调用直接走真实客户端，无侵入。
    """

    def __init__(
        self,
        spec: FaultSpec | None = None,
        target: ToolExecutor | None = None,
        rng: random.Random | None = None,
    ):
        """
        :param spec: 故障规格；None 表示只透传（可用于计量/链路观察）。
        :param target: 底层真实工具执行函数；None 时默认
            ``app.tools.registry.execute_tool``。
        :param rng: 随机源（测试可注入固定种子复现抖动）。
        """
        if target is None:
            from app.tools.registry import execute_tool

            target = execute_tool
        self.target = target
        self.spec = spec
        self._rng = rng or random.Random()
        # 计量（可观测注入效果）
        self.calls = 0
        self.injected = 0

    # ------------------------------------------------------------------
    # 代理入口
    # ------------------------------------------------------------------

    def __call__(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """代理执行：命中规则且概率触发 → 注入故障；否则透传 target。"""
        self.calls += 1
        spec = self.spec
        if spec is None or not self._match(name, spec):
            return self.target(name, arguments or {})
        if self._rng.random() >= spec.ratio:
            return self.target(name, arguments or {})
        self.injected += 1
        return self._inject(spec, name, arguments or {})

    def attach(self, client: Any) -> Any:
        """挂到已有 McpToolClient：把代理注入为其底层执行函数。

        原执行函数保存为 :attr:`target`（代理内部继续调用它），
        ``client.set_execute`` 之后所有工具调用经过本代理。
        """
        self.target = client._execute  # noqa: SLF001 - 读取原执行链
        client.set_execute(self)
        return client

    def reset(self) -> None:
        """清零计量（新用例前调用）。"""
        self.calls = 0
        self.injected = 0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _match(name: str, spec: FaultSpec) -> bool:
        if not spec.tools:
            return True
        return any(re.search(pattern, name) for pattern in spec.tools)

    def _inject(self, spec: FaultSpec, name: str, arguments: dict) -> Any:
        kind = spec.kind
        if kind is FaultKind.DELAY and spec.delay_ms > 0:
            time.sleep(spec.delay_ms / 1000.0)
        if kind is FaultKind.TIMEOUT or kind is FaultKind.JITTER:
            raise TimeoutError(spec.message or f"工具 {name} 调用超时（注入）")
        if kind is FaultKind.CONNECTION_ERROR:
            raise ConnectionError(spec.message or f"工具 {name} 连接失败（注入）")
        if kind is FaultKind.INVALID_JSON:
            return spec.message or "{not-valid-json!!"
        if kind is FaultKind.RATE_LIMIT:
            return json.dumps({
                "success": False,
                "error_code": "transient_failure",
                "error": spec.message or "429 Too Many Requests，服务限流，请稍后重试",
            }, ensure_ascii=False)
        if kind is FaultKind.BAD_BUSINESS_CODE:
            return json.dumps({
                "success": False,
                "error_code": "business_rejection",
                "error": spec.message or "业务规则拒绝（注入）",
            }, ensure_ascii=False)
        if kind in (FaultKind.MISSING_FIELD, FaultKind.DIRTY_DATA):
            return self._mutate_result(spec, name, arguments)
        return self.target(name, arguments)

    def _mutate_result(self, spec: FaultSpec, name: str, arguments: dict) -> str:
        """对真实返回做字段级篡改（缺字段 / 脏数据）。"""
        raw = self.target(name, arguments)
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, json.JSONDecodeError):
            return raw  # 底层本就不是 JSON，保持原样
        if not isinstance(data, dict):
            return raw
        target_field = spec.field_name or "orders"
        if spec.kind is FaultKind.MISSING_FIELD:
            data.pop(target_field, None)
        else:  # DIRTY_DATA：把字段改成类型错乱的值
            data[target_field] = "dirty-not-a-list"
        return json.dumps(data, ensure_ascii=False)


def fault_client(
    spec: FaultSpec | None,
    *,
    retries: int = 0,
    timeout_ms: int = 0,
    execute: ToolExecutor | None = None,
):
    """构建「已挂故障代理」的 McpToolClient（测试环境便捷入口）。

    ::

        client = fault_client(FaultSpec(FaultKind.RATE_LIMIT, tools=(r"apply_.*",)))
        # 之后所有 apply_* 工具调用都会返回 429 限流
    """
    from app.observability.mcp_client import McpToolClient

    injector = FaultInjector(spec=spec, target=execute)
    return McpToolClient(execute=injector, retries=retries, timeout_ms=timeout_ms)
