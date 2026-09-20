"""RT 指标聚合（第12期）：整条请求总 RT + 各阶段 RT 拆分。

需求：

- 整条 query 总 RT：从收到用户输入到返回用户的总毫秒；
- 拆分各阶段 RT：查询改写 RT、RAG 检索 RT、MCP 工具 RT、LLM 生成 RT。

数据来源：trace 结束时的 span 列表（状态 span / LLM span / 工具 span 都携带
``cost_ms``）。:func:`finalize_trace` 在请求结束时聚合：

- ``total``：trace 全程（duration_ms）；
- ``rewrite``：查询改写阶段 = ``stage/rewrite`` span + ``llm/rewrite`` span；
- ``llm_generate``：主生成类 LLM span（main / workflow_reply / react）；
- ``tool``：全部工具 span 之和（不含 search_knowledge，它归入 rag）；
- ``rag``：``tool/search_knowledge`` 的耗时（RAG 检索发生在该工具内）。

聚合结果喂给 :func:`~app.observability.metrics.emit_rt` 的直方图，
监控大盘由 histogram 直接出 P50/P95/P99。
"""

from __future__ import annotations

from typing import Any

from app.observability.metrics import emit_rt

#: 查询改写场景（LLM span 的 scene 标签）
_REWRITE_SCENES = frozenset({"rewrite"})
#: 主生成类场景（LLM span 的 scene 标签）
_MAIN_LLM_SCENES = frozenset({"main", "workflow_reply", "react"})


def _stage_cost(spans: list[dict], *, kind: str, name: str) -> float:
    """单个 stage span 的耗时（匹配 kind + name）。"""
    for span in spans:
        if span.get("kind") == kind and span.get("name") == name:
            return float(span.get("cost_ms", 0.0))
    return 0.0


def _sum_llm(spans: list[dict], scenes: frozenset[str]) -> float:
    return sum(
        float(span.get("cost_ms", 0.0))
        for span in spans
        if span.get("kind") == "llm" and span.get("name") in scenes
    )


def _sum_tools(spans: list[dict], *, exclude_rag: bool) -> float:
    total = 0.0
    for span in spans:
        if span.get("kind") != "tool":
            continue
        if exclude_rag and span.get("name") == "search_knowledge":
            continue
        total += float(span.get("cost_ms", 0.0))
    return total


def _sum_tool_by_name(spans: list[dict], name: str) -> float:
    return sum(
        float(span.get("cost_ms", 0.0))
        for span in spans
        if span.get("kind") == "tool" and span.get("name") == name
    )


def aggregate_stages(trace_payload: dict) -> dict[str, float]:
    """从 trace payload（含 spans）聚合各阶段 RT。"""
    spans: list[dict] = trace_payload.get("spans") or []
    stages: dict[str, float] = {
        "rewrite": _stage_cost(spans, kind="stage", name="rewrite")
        + _sum_llm(spans, _REWRITE_SCENES),
        "llm_generate": _sum_llm(spans, _MAIN_LLM_SCENES),
        "rag": _sum_tool_by_name(spans, "search_knowledge"),
        "tool": _sum_tools(spans, exclude_rag=True),
    }
    return stages


def finalize_trace(trace_payload: dict[str, Any]) -> None:
    """trace 收尾：聚合阶段 RT 并发射指标（best-effort）。"""
    try:
        total_ms = float(trace_payload.get("duration_ms", 0.0))
        stages = aggregate_stages(trace_payload)
        emit_rt(total_ms=total_ms, stages=stages)
    except Exception:  # noqa: BLE001 —— 指标发射失败不影响业务
        pass
