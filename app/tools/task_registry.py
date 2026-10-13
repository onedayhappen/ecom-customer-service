"""Task-level tool groups for dynamic tool pruning.

The registry is intentionally conservative: each task starts with the smallest
useful tool set, while complex/general tasks fall back to the domain-safe tools
already assigned to the sub-agent.
"""

from __future__ import annotations

from dataclasses import dataclass


DOMAIN_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "presale": frozenset({
        "query_product",
        "search_knowledge",
        "list_user_orders",
        "recall_user_memory",
    }),
    "postsale": frozenset({
        "query_order",
        "query_logistics",
        "apply_refund",
        "list_user_orders",
        "search_knowledge",
        "recall_user_memory",
    }),
    "complaint": frozenset({
        "escalate_to_human",
        "query_order",
        "search_knowledge",
        "recall_user_memory",
    }),
}


TASK_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "greeting": frozenset(),
    "end_conversation": frozenset(),
    "affirmation": frozenset(),
    "order_status": frozenset({"query_order"}),
    "logistics_track": frozenset({"query_order", "query_logistics", "list_user_orders"}),
    "list_orders": frozenset({"list_user_orders"}),
    "refund": frozenset({"query_order", "apply_refund", "search_knowledge", "list_user_orders"}),
    "product_consult": frozenset({"query_product", "recall_user_memory", "search_knowledge"}),
    "complaint": frozenset({"query_order", "search_knowledge", "recall_user_memory", "escalate_to_human"}),
    "general": frozenset(),
}


TASK_TOOL_EXPANSIONS: dict[str, dict[str, frozenset[str]]] = {
    "logistics_track": {
        "after_query_order": frozenset({"query_logistics"}),
    },
    "refund": {
        "after_query_order": frozenset({"apply_refund", "search_knowledge"}),
    },
    "product_consult": {
        "policy_terms": frozenset({"search_knowledge"}),
        "memory_terms": frozenset({"recall_user_memory"}),
    },
    "complaint": {
        "policy_terms": frozenset({"search_knowledge"}),
        "memory_terms": frozenset({"recall_user_memory"}),
    },
}


@dataclass(frozen=True)
class ToolSelection:
    """Resolved tool names plus the reason used for observability/tests."""

    names: frozenset[str]
    reason: str


def tools_for_task(
    *,
    domain: str,
    task_type: str,
    available_tools: set[str],
) -> ToolSelection:
    """Return task-level tools intersected with the sub-agent's available tools."""
    task_tools = TASK_TOOL_GROUPS.get(task_type, frozenset())
    if task_tools:
        return ToolSelection(
            names=frozenset(task_tools.intersection(available_tools)),
            reason=f"task:{task_type}",
        )

    domain_tools = DOMAIN_TOOL_GROUPS.get(domain, frozenset(available_tools))
    return ToolSelection(
        names=frozenset(domain_tools.intersection(available_tools)),
        reason=f"domain:{domain}",
    )


def expand_tools(
    *,
    task_type: str,
    available_tools: set[str],
    completed_tools: list[str],
    latest_tool_result: dict | None,
    text: str,
) -> frozenset[str]:
    """Add narrowly-scoped tools when the compact state shows they are needed."""
    expansions = TASK_TOOL_EXPANSIONS.get(task_type, {})
    selected: set[str] = set()

    latest_tool = (latest_tool_result or {}).get("tool")
    if latest_tool == "query_order" and "query_order" in completed_tools:
        selected.update(expansions.get("after_query_order", frozenset()))

    if any(term in text for term in ("政策", "运费", "七天", "会员", "配送", "赔偿")):
        selected.update(expansions.get("policy_terms", frozenset()))
    if any(term in text for term in ("上次", "之前", "记得", "偏好", "历史", "我说过")):
        selected.update(expansions.get("memory_terms", frozenset()))

    return frozenset(selected.intersection(available_tools))
