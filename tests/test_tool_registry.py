"""Task-level tool pruning keeps ReAct tool schemas small and bounded."""

from app.tools.manager import ToolManager
from app.tools.task_registry import tools_for_task


def _names(definitions):
    return {definition["function"]["name"] for definition in definitions}


def test_task_registry_prunes_logistics_tools():
    available = {
        "query_order", "query_logistics", "apply_refund",
        "list_user_orders", "search_knowledge", "recall_user_memory",
    }

    selection = tools_for_task(
        domain="postsale",
        task_type="logistics_track",
        available_tools=available,
    )

    assert selection.names == {"query_order", "query_logistics", "list_user_orders"}


def test_tool_manager_get_tools_intersects_agent_whitelist():
    manager = ToolManager(
        use_mcp=False,
        allowed_tools={"query_order", "search_knowledge", "recall_user_memory"},
    )

    tools = manager.get_tools(domain="complaint", task_type="complaint")

    assert _names(tools) == {"query_order", "search_knowledge", "recall_user_memory"}
    assert "query_product" not in _names(tools)
    assert "apply_refund" not in _names(tools)


def test_presale_product_consult_never_exposes_refund_tool():
    manager = ToolManager(
        use_mcp=False,
        allowed_tools={
            "query_product", "search_knowledge",
            "list_user_orders", "recall_user_memory",
        },
    )

    tools = manager.get_tools(domain="presale", task_type="product_consult")

    assert _names(tools) == {"query_product", "search_knowledge", "recall_user_memory"}
    assert "apply_refund" not in _names(tools)
