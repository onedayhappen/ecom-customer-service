# 文件说明：测试 ReAct 工具调用、计划白名单、写入阻断及执行证据校验。
"""ReAct tool boundaries, business-write blocking and evidence checks."""

from __future__ import annotations

import json

import pytest


from app.data_plan import DataPlan, build_task_plan
from app.execution import ReactExecutor


def _plan(intent: str) -> DataPlan:
    allowed = {"query_order", "list_user_orders", "search_knowledge"}
    if intent == "after_sale_request":
        allowed.add("apply_refund")
    return build_task_plan(intent, allowed_tools=allowed)


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
    # 工具范围由服务端明确提供，模型不能通过标签扩大范围。
    plan = _plan("complaint")
    assert "apply_refund" not in plan.prompt_tool_names
    assert "query_order" in plan.prompt_tool_names


def test_react_confirmation_gate_blocks_mutating_tool(monkeypatch):
    # Synthetic capability tests the framework gate without restoring a retired tool.
    monkeypatch.setattr("app.execution.react.MUTATING_CAPABILITIES", frozenset({"fixture_write"}))
    script = [_Msg(content="", tool_calls=[_TC("c1", "fixture_write", {})])]
    executor = ReactExecutor(client=_Client(script), model="test", execute=lambda *_: pytest.fail("write bypassed confirmation"))
    result = executor.run(
        messages=[{"role": "user", "content": "帮我退款"}],
        plan=build_task_plan("after_sale_request", allowed_tools={"fixture_write"}),
        tool_definitions=[],
    )
    assert result.blocked is True
    assert result.tool_steps == []          # 写操作未真正执行
    assert "确认" in result.reply


def test_react_readonly_tool_not_gated():
    # 只读操作无需业务提交确认。
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


@pytest.mark.parametrize('intent', ['order_inquiry', 'refund_service', 'confirmation'])
def test_label_and_model_schema_cannot_expand_task_whitelist(intent):
    calls = []
    script = [_Msg(tool_calls=[_TC('c1', 'query_order', {'order_id': 'ORD-A'})])]
    executor = ReactExecutor(client=_Client(script), model='test',
                             execute=lambda name, args: calls.append(name) or '{}')
    result = executor.run(messages=[{'role': 'user', 'content': '查订单'}],
        plan=build_task_plan(intent, allowed_tools=set()),
        tool_definitions=[{'type': 'function', 'function': {'name': 'query_order',
                          'parameters': {'type': 'object'}}}])
    assert result.blocked and result.needs_human
    assert calls == []
