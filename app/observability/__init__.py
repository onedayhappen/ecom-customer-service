"""业务链可观测性（第11期 + 第12期）：全链路 trace / span 埋点体系。

- 第11期：BusinessTracer（阶段事件）+ patch_client（LLM 插桩）+ query CLI。
- 第12期（本包扩展）：Trace/Span 核心、Prometheus 指标、审计日志、
  统一 LLM / 工具客户端（埋点在客户端内部 + 装饰器发射指标）、
  FSM 状态流转观测、RT 全链路与阶段拆分。

对外入口：

- :class:`~app.observability.tracer.BusinessTracer`：业务链轨迹（阶段事件 + span 树）
- :class:`~app.observability.trace.TraceManager`：Trace/Span 核心（内存埋点）
- :class:`~app.observability.llm_client.LlmClient`：统一 LLM 客户端
- :class:`~app.observability.mcp_client.McpToolClient`：统一工具客户端
- :mod:`~app.observability.metrics`：Prometheus 指标（/metrics 端点）
- :mod:`~app.observability.audit`：审计日志（异常 100% + 抽样）
- :mod:`~app.observability.fsm`：FSM 状态流转观测
- :mod:`~app.observability.rt`：RT 指标聚合
"""

from app.observability.tracer import BusinessTracer, patch_client, suspend_active_tracer

__all__ = [
    "BusinessTracer",
    "patch_client",
    "suspend_active_tracer",
]
