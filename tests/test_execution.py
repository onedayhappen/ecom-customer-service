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
from app.execution import ReactExecutor, ToolRunner, dispatch
from app.execution.workflow import ORDER_MUTATIONS, WORKFLOW_HANDLERS, run_workflow
from app.intent.query_rewrite import EntityExtraction


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


def _runner() -> ToolRunner:
    return ToolRunner(execute=_fake_execute)


# ---------------------------------------------------------------------------
# Workflow：查询类（只读）
# ---------------------------------------------------------------------------

def test_order_inquiry_with_order_id():
    result = run_workflow("order_inquiry", "帮我查一下订单 ORD-20240115-001", _plan("order_inquiry"), _runner())
    assert "ORD-20240115-001" in result.reply
    assert result.tool_steps[0].name == "query_order"
    assert result.tool_steps[0].success


def test_order_inquiry_without_order_id_lists_orders():
    result = run_workflow("order_inquiry", "我买过哪些东西", _plan("order_inquiry"), _runner())
    assert result.tool_steps[0].name == "list_user_orders"
    assert "订单" in result.reply


def test_logistics_inquiry():
    result = run_workflow("logistics_inquiry", "ORD-20240115-001 到哪了", _plan("logistics_inquiry"), _runner())
    assert [s.name for s in result.tool_steps] == ["query_order", "query_logistics"]
    assert "顺丰" in result.reply


def test_logistics_inquiry_by_tracking_number():
    # 用户直接给运单号（无订单号）→ 无需先定位订单，直接按运单号查物流
    result = run_workflow(
        "logistics_inquiry", "帮我查一下运单号 SF1234567890 到哪了",
        _plan("logistics_inquiry"), _runner(),
    )
    assert [s.name for s in result.tool_steps] == ["query_logistics"]
    assert result.tool_steps[0].arguments == {"tracking_number": "SF1234567890"}
    assert "SF1234567890" in result.reply
    assert "顺丰" in result.reply


def test_logistics_inquiry_unknown_tracking_number():
    # 运单号查无轨迹 → 明确提示，不列全部订单
    def execute(name, args):
        if name == "query_logistics":
            return json.dumps({"success": False, "error": "物流单号 X 暂无轨迹信息"}, ensure_ascii=False)
        return json.dumps({"success": False}, ensure_ascii=False)

    result = run_workflow(
        "logistics_inquiry", "查 SF9999999999", _plan("logistics_inquiry"),
        ToolRunner(execute=execute),
    )
    assert result.tool_steps[0].name == "query_logistics"
    assert "SF9999999999" in result.reply


def test_logistics_list_pushes_status_filter_and_selection_state():
    calls = []

    def execute(name, args):
        calls.append((name, args))
        if name == "list_user_orders":
            return json.dumps({"success": True, "orders": [
                {"order_id": "ORD-20240115-001", "items_summary": "运动鞋", "status": "shipped"},
                {"order_id": "ORD-20240120-002", "items_summary": "耳机", "status": "shipped"},
            ]}, ensure_ascii=False)
        return json.dumps({"success": False}, ensure_ascii=False)

    result = run_workflow(
        "logistics_inquiry", "还有一笔已发货的物流", _plan("logistics_inquiry"),
        ToolRunner(execute=execute),
        entities=EntityExtraction(order_status="shipped"),
    )

    assert calls == [("list_user_orders", {"status": "shipped"})]
    assert [item["order_id"] for item in result.selection_candidates] == [
        "ORD-20240115-001", "ORD-20240120-002",
    ]


def test_logistics_failure_keeps_authoritative_order_evidence():
    def execute(name, args):
        if name == "query_order":
            return json.dumps({"success": True, "order": {
                "order_id": "ORD-20240115-001", "status": "shipped",
                "carrier": "顺丰速运", "tracking_number": "SF1234567890",
            }}, ensure_ascii=False)
        if name == "query_logistics":
            return json.dumps({"success": False, "error": "轨迹服务暂时不可用"}, ensure_ascii=False)
        return json.dumps({"success": False}, ensure_ascii=False)

    result = run_workflow(
        "logistics_inquiry", "ORD-20240115-001 到哪了", _plan("logistics_inquiry"),
        ToolRunner(execute=execute),
    )

    assert "顺丰速运" in result.reply
    assert "SF1234567890" in result.reply
    assert "无法确认具体到达时间" in result.reply


def test_refund_status_degrades_to_query_order():
    # query_refund 未实现 → 降级用 query_order 的 refund_status 字段
    result = run_workflow("refund_status", "我的退款到哪了 ORD-20240118-004", _plan("refund_status"), _runner())
    assert result.tool_steps[0].name == "query_order"
    assert "审核中" in result.reply


def test_refund_status_no_refund_record():
    result = run_workflow("refund_status", "ORD-20240115-001 退款到哪了", _plan("refund_status"), _runner())
    assert "没有退款记录" in result.reply


def test_product_inquiry_returns_products():
    result = run_workflow("product_inquiry", "降噪耳机哪款好", _plan("product_inquiry"), _runner())
    assert result.tool_steps[0].name == "query_product"
    assert "耳机" in result.reply


# ---------------------------------------------------------------------------
# Workflow：写操作（确认门控 + 未实现降级）
# ---------------------------------------------------------------------------

def test_order_mutations_are_declared_and_registered():
    """同类订单写操作通过配置扩展，不应各自复制完整的条件分支。"""
    assert {spec.intent.value for spec in ORDER_MUTATIONS} == {
        "address_change", "order_cancel", "expedite",
    }
    assert all(WORKFLOW_HANDLERS[spec.intent] for spec in ORDER_MUTATIONS)


def test_address_change_degrades_when_tool_missing():
    result = run_workflow("address_change", "帮我改地址 ORD-20240120-002 改成浦东新区", _plan("address_change"), _runner())
    assert result.blocked is True
    assert result.needs_human is True


def test_address_change_rejects_shipped_order():
    result = run_workflow("address_change", "帮我改地址 ORD-20240115-001", _plan("address_change"), _runner())
    assert "已无法修改" in result.reply


def test_order_cancel_degrades_when_tool_missing():
    result = run_workflow("order_cancel", "取消订单 ORD-20240120-002", _plan("order_cancel"), _runner())
    assert result.blocked is True
    assert result.needs_human is True


def test_after_sale_refund_requires_confirmation_then_executes():
    plan = _plan("after_sale_request")
    runner = _runner()
    gated = run_workflow("after_sale_request", "帮我退款 ORD-20240120-002 不想要了", plan, runner, confirmed=False)
    assert gated.needs_confirmation is True
    assert not any(s.name == "apply_refund" for s in gated.tool_steps)

    done = run_workflow("after_sale_request", "帮我退款 ORD-20240120-002 不想要了", plan, runner, confirmed=True)
    assert done.tool_steps[-1].name == "apply_refund"
    assert done.tool_steps[-1].success


def test_after_sale_exchange_degrades():
    result = run_workflow("after_sale_request", "帮我换货 ORD-20240120-002", _plan("after_sale_request"), _runner(), confirmed=True)
    assert result.blocked is True
    assert result.needs_human is True


def test_custom_available_tools_unblock_write_flow():
    calls: list[dict] = []

    def fake(name: str, args: dict) -> str:
        calls.append({"name": name, "args": args})
        if name == "query_order":
            return json.dumps({"success": True, "order": {"order_id": "ORD-20240120-002", "status": "pending", "items": []}})
        if name == "update_address":
            return json.dumps({"success": True, "message": "地址已修改成功"})
        return json.dumps({"success": False})

    runner = ToolRunner(execute=fake, available_tools={"query_order", "update_address"})
    result = run_workflow("address_change", "帮我改地址 ORD-20240120-002 改成 浦东新区张江路100号",
                          _plan("address_change"), runner, confirmed=True)
    assert result.needs_human is False
    assert any(c["name"] == "update_address" for c in calls)


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
    # CONFIRM_REQUIRED 意图（after_sale_request）的写操作未确认 → 不执行、等待确认
    script = [_Msg(content="", tool_calls=[_TC("c1", "apply_refund", {"order_id": "ORD-20240120-002", "reason": "不想要了"})])]
    executor = ReactExecutor(client=_Client(script), model="test", execute=_fake_execute)
    result = executor.run(
        messages=[{"role": "user", "content": "帮我退款"}],
        plan=_plan("after_sale_request"),
        tool_definitions=[],
        confirmed=False,
    )
    assert result.needs_confirmation is True
    assert result.tool_steps == []          # 写操作未真正执行
    assert "确认" in result.reply


def test_react_confirmation_gate_executes_when_confirmed():
    # 用户确认后 → 写操作正常执行并进入证据包
    script = [
        _Msg(content="", tool_calls=[_TC("c1", "query_order", {"order_id": "ORD-20240120-002"})]),
        _Msg(content="", tool_calls=[_TC("c2", "apply_refund", {"order_id": "ORD-20240120-002", "reason": "不想要了"})]),
        _Msg(content="退款申请已提交"),
    ]
    executor = ReactExecutor(client=_Client(script), model="test", execute=_fake_execute)
    result = executor.run(
        messages=[{"role": "user", "content": "确认退款"}],
        plan=_plan("after_sale_request"),
        tool_definitions=[],
        confirmed=True,
    )
    assert result.needs_confirmation is False
    assert [s.name for s in result.tool_steps] == ["query_order", "apply_refund"]
    assert result.tool_steps[-1].success
    assert result.reply == "退款申请已提交"


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
        confirmed=False,
    )
    assert result.needs_confirmation is False
    assert result.tool_steps[0].name == "query_order"


# ---------------------------------------------------------------------------
# dispatch 三路分发
# ---------------------------------------------------------------------------

def test_dispatch_workflow_path():
    plan = _plan("order_inquiry")
    result = dispatch(plan, user_input="查订单 ORD-20240115-001", runner=_runner())
    assert result.execution == Execution.WORKFLOW
    assert "ORD-20240115-001" in result.reply


def test_dispatch_rule_path_greetings():
    """对话流并入 Workflow：greetings 走 workflow handler，模板兜底。"""
    result = dispatch(_plan("greetings"), user_input="你好", runner=_runner())
    assert result.execution == Execution.WORKFLOW
    assert "小夕" in result.reply


def test_dispatch_dialog_uses_reply_generator():
    """对话流也可注入生成器：回复由模型生成而非固定模板。"""
    captured: dict = {}

    def generator(plan, tool_steps, user_input):
        captured["steps"] = tool_steps
        return "模型生成的问候语"

    result = dispatch(
        _plan("greetings"), user_input="你好", runner=_runner(),
        reply_generator=generator,
    )
    assert result.reply == "模型生成的问候语"
    assert captured["steps"] == []  # 对话流无工具证据


def test_dialog_handlers_registered_for_all_dialog_intents():
    """四个对话流标签都注册了 workflow handler。"""
    from app.data_plan.enums import Intent
    for intent in (Intent.GREETINGS, Intent.CLOSURE,
                   Intent.CONFIRMATION, Intent.ANSWER_QUESTION):
        assert intent in WORKFLOW_HANDLERS
        result = run_workflow(intent.value, "你好", _plan(intent.value), _runner())
        assert result.execution == Execution.WORKFLOW
        assert result.reply


def test_dispatch_react_requires_executor():
    with pytest.raises(ValueError):
        dispatch(_plan("complaint"), user_input="我要投诉")


# ---------------------------------------------------------------------------
# Workflow 回复生成器（确定性工具编排 + LLM 表述）
# ---------------------------------------------------------------------------

def test_workflow_success_uses_reply_generator():
    """注入生成器后，成功路径的回复交给 LLM，且收到完整证据包。"""
    captured: dict = {}

    def generator(plan, tool_steps, user_input):
        captured["plan"] = plan
        captured["steps"] = tool_steps
        captured["user_input"] = user_input
        return "LLM 生成的物流回复"

    result = run_workflow(
        "logistics_inquiry", "查物流 ORD-20240115-001",
        _plan("logistics_inquiry"), _runner(),
        reply_generator=generator,
    )
    assert result.reply == "LLM 生成的物流回复"
    assert [s.name for s in captured["steps"]] == ["query_order", "query_logistics"]
    assert captured["user_input"] == "查物流 ORD-20240115-001"


def test_workflow_without_generator_falls_back_to_template():
    """缺省生成器时回退模板拼接（兼容测试/纯本地环境）。"""
    result = run_workflow(
        "logistics_inquiry", "查物流 ORD-20240115-001", _plan("logistics_inquiry"), _runner(),
    )
    assert "最新物流" in result.reply


def test_workflow_confirmation_gate_skips_generator():
    """确认门控等交互分支不触发 LLM（安全底线 + 成本控制）。"""
    calls: list = []

    def generator(plan, tool_steps, user_input):
        calls.append(1)
        return "不应被调用"

    result = run_workflow(
        "after_sale_request", "退款 ORD-20240120-002",
        _plan("after_sale_request"), _runner(),
        confirmed=False, reply_generator=generator,
    )
    assert result.needs_confirmation is True
    assert calls == []


def test_workflow_degrade_skips_generator():
    """能力降级分支同样不触发 LLM。"""
    calls: list = []

    def generator(plan, tool_steps, user_input):
        calls.append(1)
        return "不应被调用"

    result = run_workflow(
        "order_cancel", "取消订单 ORD-20240120-002",
        _plan("order_cancel"), _runner(),
        confirmed=True, reply_generator=generator,
    )
    assert result.blocked is True
    assert calls == []


def test_engine_injects_workflow_reply_generator():
    """AgentEngine 将规则与可独立预算的业务证据一起传给模型。"""
    captured: dict = {}

    class _FakeMessage:
        content = "LLM 回复：您的订单正在派送中"

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    engine = AgentEngine("logistics_inquiry", client=_FakeClient(), model="test-model")
    result = engine.execute("查物流 ORD-20240115-001", runner=_runner())
    assert result.reply == "LLM 回复：您的订单正在派送中"
    # system prompt 携带工具返回的真实数据（运单号），LLM 只能基于证据回答
    evidence = next(m for m in captured['messages'] if m.get('name') == 'workflow_evidence')
    assert "SF1234567890" in evidence['content']
    assert captured["messages"][-1]["role"] == "user"


def test_engine_workflow_without_client_falls_back():
    """无 client/model（测试/本地）时回退模板，不发起 LLM 调用。"""
    engine = AgentEngine("logistics_inquiry")
    result = engine.execute("查物流 ORD-20240115-001", runner=_runner())
    assert "最新物流" in result.reply


def test_workflow_reply_generator_injects_tone_by_domain():
    """Workflow 回复 prompt 按域注入精简语气（从旧子 Agent prompt 提取）。"""
    captured: dict = {}

    class _Msg:
        content = "好的"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            captured["system"] = kwargs["messages"][0]["content"]
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    engine = AgentEngine("refund_status", client=_Client(), model="test")
    engine.execute("退款进度 ORD-20240118-004", runner=_runner())
    # 售后域 → 耐心细致语气；订单域 → 清晰准确（两域不同）
    assert "耐心细致" in captured["system"]

    engine = AgentEngine("logistics_inquiry", client=_Client(), model="test")
    engine.execute("查物流 ORD-20240115-001", runner=_runner())
    assert "清晰准确" in captured["system"]


def test_workflow_reply_dialog_tone():
    """对话流回复 prompt 注入自然友好简洁语气。"""
    captured: dict = {}

    class _Msg:
        content = "你好"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            captured["system"] = kwargs["messages"][0]["content"]
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    engine = AgentEngine("greetings", client=_Client(), model="test")
    engine.execute("你好", runner=_runner())
    assert "自然、友好、简洁" in captured["system"]


def test_dispatch_passes_reply_generator():
    """dispatch 透传 reply_generator 到 Workflow 路径。"""
    captured: dict = {}

    def generator(plan, tool_steps, user_input):
        captured["steps"] = tool_steps
        return "dispatch 透传成功"

    result = dispatch(
        _plan("order_inquiry"),
        user_input="查订单 ORD-20240115-001",
        runner=_runner(),
        reply_generator=generator,
    )
    assert result.reply == "dispatch 透传成功"
    assert captured["steps"][0].name == "query_order"


# ---------------------------------------------------------------------------
# ReAct system prompt：行为准则注入（改编自旧子 Agent prompt）
# ---------------------------------------------------------------------------

def test_react_system_prompt_injects_guideline_by_intent():
    from app.execution.react import default_system_prompt

    # 动态装配：任务描述 + DataPlan 裁剪的工具清单
    prompt = default_system_prompt(_plan("complaint"))
    assert "当前任务" in prompt
    assert "可用工具" in prompt
    # 静态准则：投诉 → 先安抚后处理 + 强制升级
    assert "先安抚后处理" in prompt
    assert "转人工" in prompt


def test_react_system_prompt_guidelines_for_presale():
    from app.execution.react import default_system_prompt

    prompt = default_system_prompt(_plan("product_inquiry"))
    assert "购物顾问" in prompt
    assert "不编造" in prompt


def test_react_system_prompt_without_guideline():
    """对话流意图无 ReAct 准则（不走 ReAct，本函数不适用）；兜底意图有兜底准则。"""
    from app.execution.react import default_system_prompt

    prompt = default_system_prompt(_plan("others"))
    assert "无法明确归类" in prompt
