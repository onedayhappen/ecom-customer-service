"""执行层单元测试：Workflow 确定性脚本、确认门控、未实现降级、ReAct 循环、三路分发。

测试通过注入 fake 工具执行器模拟已实现的 7 个能力，不依赖真实 registry
（registry 会拉入 pydantic/chromadb 等重依赖），使执行层逻辑可独立、快速验证。
"""

from __future__ import annotations

import json

import pytest


from app.agents.engine import AgentEngine
from app.data_plan import DataPlan, build_data_plan
from app.data_plan.enums import Execution
from app.execution import ReactExecutor


def _plan(intent: str) -> DataPlan:
    return build_data_plan(intent)


# ---------------------------------------------------------------------------
# fake 工具环境（模拟已实现的 7 个能力）
# ---------------------------------------------------------------------------

_MOCK_ORDERS = {
    "ORD-20240115-001": {
        "order_id": "ORD-20240115-001", "status": "shipped",
        "items": [{"name": "Nike 运动鞋", "quantity": 1}], "total": 899.0,
        "carrier": "顺丰速运", "tracking_number": "SF1234567890",
    },
    "ORD-20240120-002": {
        "order_id": "ORD-20240120-002", "status": "pending",
        "items": [{"name": "AirPods Pro 2", "quantity": 1}], "total": 1828.9,
    },
    "ORD-20240118-004": {
        "order_id": "ORD-20240118-004", "status": "refund_processing",
        "items": [{"name": "Levi's 牛仔裤", "quantity": 1}], "total": 699.0,
        "refund_status": "审核中", "refund_reason": "尺码不合适",
        "refund_requested_at": "2024-01-22 10:00:00",
    },
}


def _fake_execute(name: str, args: dict) -> str:
    if name == "query_order":
        order = _MOCK_ORDERS.get(args.get("order_id"))
        if order:
            return json.dumps({"success": True, "order": order}, ensure_ascii=False)
        return json.dumps({"success": False, "error": "未找到订单"}, ensure_ascii=False)
    if name == "list_user_orders":
        orders = [
            {"order_id": k, "items_summary": "商品", "status": v["status"], "total": v["total"]}
            for k, v in _MOCK_ORDERS.items()
        ]
        return json.dumps({"success": True, "orders": orders, "count": len(orders)}, ensure_ascii=False)
    if name == "query_logistics":
        return json.dumps({"success": True, "logistics": {
            "carrier": "顺丰速运", "tracking_number": "SF1234567890",
            "events": [{"time": "2024-01-18 08:30", "location": "上海浦东区", "description": "正在派送中"}],
        }}, ensure_ascii=False)
    if name == "apply_refund":
        return json.dumps({"success": True, "message": "退款申请已提交"}, ensure_ascii=False)
    if name == "query_product":
        return json.dumps({"success": True, "matched_products": [
            {"name": "Sony WF-1000XM5 降噪耳机", "price": 1999.0, "stock": 46, "description": "旗舰真无线降噪"},
        ], "total_matches": 1}, ensure_ascii=False)
    if name == "search_knowledge":
        return json.dumps({"success": True, "results": [{"text": "支持七天无理由退货"}], "query": args.get("query")}, ensure_ascii=False)
    return json.dumps({"success": False, "error": f"unknown tool {name}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# ReAct
# ---------------------------------------------------------------------------

class _Msg:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _TC:
    def __init__(self, tc_id: str, name: str, args: dict):
        self.id = tc_id
        self.function = type("F", (), {"name": name, "arguments": json.dumps(args)})()


class _Completions:
    def __init__(self, script):
        self.script = list(script)

    def create(self, **kwargs):
        return type("Resp", (), {"choices": [type("Ch", (), {"message": self.script.pop(0)})()]})()


class _Client:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


def test_react_loop_executes_tool_then_replies():
    script = [
        _Msg(content="", tool_calls=[_TC("c1", "query_order", {"order_id": "ORD-20240115-001"})]),
        _Msg(content="已为您查到订单"),
    ]
    executor = ReactExecutor(client=_Client(script), model="test", execute=_fake_execute)
    result = executor.run(
        messages=[{"role": "user", "content": "查一下我的订单"}],
        plan=_plan("issue_report"),
        tool_definitions=[],
    )
    assert result.reply == "已为您查到订单"
    assert result.tool_steps[0].name == "query_order"


def test_react_knowledge_unavailable_fails_closed():
    def empty_kb(name, args):
        return json.dumps({"success": True, "results": []})

    script = [_Msg(content="", tool_calls=[_TC("c1", "search_knowledge", {"query": "退货政策"})])]
    executor = ReactExecutor(client=_Client(script), model="test", execute=empty_kb)
    result = executor.run(
        messages=[{"role": "user", "content": "退货政策是什么"}],
        plan=_plan("complaint"),
        tool_definitions=[],
    )
    assert "不能确认" in result.reply


def test_react_tools_pruned_by_plan():
    # 工具裁剪由 DataPlan 负责（已在 test_intentspec 验证），这里验证 plan 层契约
    plan = _plan("complaint")
    assert "apply_refund" not in plan.prompt_tool_names
    assert "query_order" in plan.prompt_tool_names


def test_react_confirmation_gate_blocks_mutating_tool():
    # Even a plan containing a write tool cannot authorize a direct mutation.
    script = [_Msg(content="", tool_calls=[_TC("c1", "apply_refund", {"order_id": "ORD-20240120-002", "reason": "不想要了"})])]
    executor = ReactExecutor(client=_Client(script), model="test", execute=_fake_execute)
    result = executor.run(
        messages=[{"role": "user", "content": "帮我退款"}],
        plan=_plan("after_sale_request"),
        tool_definitions=[],
    )
    assert result.blocked is True
    assert result.tool_steps == []          # 写操作未真正执行
    assert "确认" in result.reply


def test_react_readonly_tool_not_gated():
    # READ_ONLY 意图（issue_report）的读操作不受确认门控影响
    script = [
        _Msg(content="", tool_calls=[_TC("c1", "query_order", {"order_id": "ORD-20240118-004"})]),
        _Msg(content="已核实订单状态"),
    ]
    executor = ReactExecutor(client=_Client(script), model="test", execute=_fake_execute)
    result = executor.run(
        messages=[{"role": "user", "content": "我收到的货有问题"}],
        plan=_plan("issue_report"),
        tool_definitions=[],
    )
    assert result.needs_confirmation is False
    assert result.tool_steps[0].name == "query_order"
