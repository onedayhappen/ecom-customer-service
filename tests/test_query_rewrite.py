"""查询改写层测试：case 记忆消歧（意图识别前第一步）。

覆盖用户设计的关键场景：跨会话指代（"上次的退款到账了吗"）→ 查 case →
唯一候选改写注入订单号 / 多候选澄清 / 无候选索凭证；精确订单号与无指代放行。
"""

from __future__ import annotations

import pytest

from app.intent.query_rewrite import (
    RewriteOutcome,
    needs_case_resolution,
    rewrite_query,
)
from app.memory.case_manager import CaseManager


@pytest.fixture
def manager(tmp_path) -> CaseManager:
    return CaseManager("user-a", storage_dir=str(tmp_path))


def _open_refund_case(mgr: CaseManager, order_id: str) -> None:
    """通过 resolve（权威路径）创建一个 open refund case。"""
    mgr.resolve(f"{order_id} 要退款", verify_order=lambda _: {"success": True})


# ----------------------------------------------------------------------
# needs_case_resolution：是否触发 case 记忆检查
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("上次的退款到账了吗", True),
    ("之前那个物流到哪了", True),
    ("昨天买的耳机怎么样", False),     # 有商品词 → 订单库，不是 Case
    ("上回申请的退货处理到哪一步了", True),
    ("我的退款到账了吗", True),        # 业务事项指代（退款 + 我的）
    ("我要退款", False),              # 发起新动作，非指代历史事项
    ("ORD-001 退款到哪了", False),    # 精确订单号放行
    ("", False),
])
def test_needs_case_resolution(text: str, expected: bool) -> None:
    assert needs_case_resolution(text) is expected


# ----------------------------------------------------------------------
# rewrite_query：唯一候选 → 改写注入订单号
# ----------------------------------------------------------------------

def test_rewrite_single_case_injects_order_id(manager: CaseManager) -> None:
    _open_refund_case(manager, "ORD-001")
    outcome = rewrite_query("上次的退款到账了吗", case_manager=manager)
    assert outcome.clarification is None
    assert outcome.needs_case is True
    assert outcome.case is not None
    assert "ORD-001" in outcome.message
    assert "上次的退款到账了吗" in outcome.message


def test_rewrite_uses_case_without_order_id(manager: CaseManager) -> None:
    # case 无 order_ids 时退化为原消息（不崩溃、不澄清）
    _open_refund_case(manager, "ORD-001")
    case = next(iter(manager.cases.values()))
    case.order_ids = []
    outcome = rewrite_query("上次的退款到账了吗", case_manager=manager)
    assert outcome.message == "上次的退款到账了吗"
    assert outcome.case is case


# ----------------------------------------------------------------------
# rewrite_query：多候选 → 澄清拦截
# ----------------------------------------------------------------------

def test_rewrite_multiple_cases_asks_clarification(manager: CaseManager) -> None:
    _open_refund_case(manager, "ORD-001")
    _open_refund_case(manager, "ORD-002")
    outcome = rewrite_query("上次的退款到账了吗", case_manager=manager)
    assert outcome.clarification is not None
    assert "ORD-001" in outcome.clarification
    assert "ORD-002" in outcome.clarification
    assert outcome.case is None


# ----------------------------------------------------------------------
# rewrite_query：无候选 → 索凭证
# ----------------------------------------------------------------------

def test_rewrite_no_case_asks_credential(manager: CaseManager) -> None:
    outcome = rewrite_query("上次的退款到账了吗", case_manager=manager)
    assert outcome.clarification is not None
    assert "订单号" in outcome.clarification


def test_rewrite_ignores_irrelevant_cases(manager: CaseManager) -> None:
    # 只有物流 case，用户指代退款 → 无匹配 → 索凭证（不误用物流 case）
    _open_refund_case(manager, "ORD-001")
    case = next(iter(manager.cases.values()))
    case.intent = "logistics"
    outcome = rewrite_query("上次的退款到账了吗", case_manager=manager)
    assert outcome.clarification is not None
    assert "订单号" in outcome.clarification


# ----------------------------------------------------------------------
# rewrite_query：放行（不触发 case 检查）
# ----------------------------------------------------------------------

def test_rewrite_exact_order_id_passes_through(manager: CaseManager) -> None:
    outcome = rewrite_query("ORD-001 退款到哪了", case_manager=manager)
    assert outcome.message == "ORD-001 退款到哪了"
    assert outcome.needs_case is False
    assert outcome.clarification is None


def test_rewrite_exact_tracking_number_passes_through(manager: CaseManager) -> None:
    outcome = rewrite_query("帮我查一下 SF1234567890 到哪了", case_manager=manager)
    assert outcome.source == "tracking"
    assert outcome.message == "帮我查一下 SF1234567890 到哪了"
    assert outcome.entities.tracking_number == "SF1234567890"
    assert outcome.clarification is None


def test_rewrite_no_pronoun_passes_through(manager: CaseManager) -> None:
    _open_refund_case(manager, "ORD-001")
    outcome = rewrite_query("我要申请退款", case_manager=manager)
    assert outcome.message == "我要申请退款"
    assert outcome.needs_case is False
    assert outcome.clarification is None
    assert outcome.case is None


def test_rewrite_empty_input_passes_through(manager: CaseManager) -> None:
    outcome = rewrite_query("", case_manager=manager)
    assert outcome.message == ""
    assert outcome.clarification is None


# ----------------------------------------------------------------------
# 实体抽取：订单号 / 商品词
# ----------------------------------------------------------------------

def test_extract_order_id():
    from app.intent.query_rewrite import extract_order_id

    assert extract_order_id("查订单 ORD-20240115-001") == "ORD-20240115-001"
    assert extract_order_id("我买的鞋到哪了") is None
    assert extract_order_id("") is None


def test_extract_tracking_number():
    from app.intent.query_rewrite import extract_tracking_number

    assert extract_tracking_number("查运单号 SF1234567890") == "SF1234567890"
    assert extract_tracking_number("JD9876543210 到哪了") == "JD9876543210"
    assert extract_tracking_number("订单 ORD-20240115-001 到哪了") is None  # 订单号不是运单号
    assert extract_tracking_number("我买的鞋到哪了") is None
    assert extract_tracking_number("") is None


def test_extract_product_keywords_rules():
    from app.intent.query_rewrite import extract_product_keywords

    assert "鞋" in extract_product_keywords("我买的鞋到哪了")
    assert extract_product_keywords("帮我查一下物流") == ()
    assert extract_product_keywords("") == ()


def test_rule_disambiguator():
    from app.intent.query_rewrite import rule_disambiguator

    # 商品指代 → 查订单库
    assert rule_disambiguator("我昨天买的鞋什么时候发货").product_keywords == ("鞋",)
    # 业务事项指代 → 查 Case
    assert rule_disambiguator("上次的退款到账了吗").needs_case is True
    # 发起新动作 → 放行
    hint = rule_disambiguator("我要退款")
    assert hint.needs_case is False and hint.product_keywords == ()


def test_make_llm_disambiguator_parses_json():
    from app.intent.query_rewrite import make_llm_disambiguator

    class _Msg:
        content = '{"needs_case": false, "product_keywords": ["鞋", "运动鞋"]}'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    disambiguator = make_llm_disambiguator(_Client(), "test")
    hint = disambiguator("我买的鞋到哪了")
    assert hint.needs_case is False
    assert hint.product_keywords == ("鞋", "运动鞋")


def test_make_llm_disambiguator_needs_case():
    from app.intent.query_rewrite import make_llm_disambiguator

    class _Msg:
        content = '{"needs_case": true, "product_keywords": []}'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    disambiguator = make_llm_disambiguator(_Client(), "test")
    hint = disambiguator("上次的退款到账了吗")
    assert hint.needs_case is True
    assert hint.product_keywords == ()


def test_make_llm_disambiguator_falls_back_on_bad_json():
    from app.intent.query_rewrite import make_llm_disambiguator

    class _Msg:
        content = "抱歉，我没有识别到"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    disambiguator = make_llm_disambiguator(_Client(), "test")
    hint = disambiguator("随便聊聊")
    assert hint.needs_case is False
    assert hint.product_keywords == ()


# ----------------------------------------------------------------------
# 订单定位：商品线索 → 候选订单
# ----------------------------------------------------------------------

def _fake_execute(products_by_keyword, orders):
    """构造工具执行器：query_product 按关键词返回商品，list_user_orders 返回订单。"""
    def execute(name, args):
        if name == "query_product":
            keyword = args.get("keyword", "")
            return {"success": True, "matched_products": products_by_keyword.get(keyword, [])}
        if name == "list_user_orders":
            return {"success": True, "orders": orders}
        return {"success": False}
    return execute


def test_resolve_orders_by_product_unique():
    from app.intent.query_rewrite import resolve_orders_by_product

    execute = _fake_execute(
        {"鞋": [{"name": "Nike 运动鞋"}]},
        [
            {"order_id": "ORD-001", "items_summary": "Nike 运动鞋", "status": "已发货"},
            {"order_id": "ORD-002", "items_summary": "AirPods Pro", "status": "待发货"},
        ],
    )
    assert resolve_orders_by_product(("鞋",), execute=execute) == [
        {"order_id": "ORD-001", "items_summary": "Nike 运动鞋", "status": "已发货"},
    ]


def test_resolve_orders_by_product_multiple():
    from app.intent.query_rewrite import resolve_orders_by_product

    execute = _fake_execute(
        {"鞋": [{"name": "Nike 运动鞋"}, {"name": "安踏跑鞋"}]},
        [
            {"order_id": "ORD-001", "items_summary": "Nike 运动鞋", "status": "已发货"},
            {"order_id": "ORD-006", "items_summary": "安踏跑鞋", "status": "已签收"},
        ],
    )
    result = resolve_orders_by_product(("鞋",), execute=execute)
    assert len(result) == 2
    assert {o["order_id"] for o in result} == {"ORD-001", "ORD-006"}


def test_resolve_orders_by_product_empty():
    from app.intent.query_rewrite import resolve_orders_by_product

    execute = _fake_execute(
        {"鞋": [{"name": "Nike 运动鞋"}]},
        [{"order_id": "ORD-002", "items_summary": "AirPods Pro", "status": "待发货"}],
    )
    assert resolve_orders_by_product(("鞋",), execute=execute) == []


# ----------------------------------------------------------------------
# rewrite_query：商品指代消歧（三级）
# ----------------------------------------------------------------------

def test_rewrite_product_ref_unique_injects_order_id(manager: CaseManager):
    execute = _fake_execute(
        {"鞋": [{"name": "Nike 运动鞋"}]},
        [{"order_id": "ORD-001", "items_summary": "Nike 运动鞋", "status": "已发货"}],
    )
    outcome = rewrite_query("我买的鞋到哪了", case_manager=manager, execute=execute)
    assert outcome.clarification is None
    assert "ORD-001" in outcome.message
    assert outcome.source == "product"
    assert "鞋" in outcome.entities.product_keywords


def test_rewrite_product_ref_multiple_clarifies(manager: CaseManager):
    execute = _fake_execute(
        {"鞋": [{"name": "Nike 运动鞋"}, {"name": "安踏跑鞋"}]},
        [
            {"order_id": "ORD-001", "items_summary": "Nike 运动鞋", "status": "已发货"},
            {"order_id": "ORD-006", "items_summary": "安踏跑鞋", "status": "已签收"},
        ],
    )
    outcome = rewrite_query("我买的鞋到哪了", case_manager=manager, execute=execute)
    assert outcome.clarification is not None
    assert "ORD-001" in outcome.clarification
    assert "ORD-006" in outcome.clarification
    assert len(outcome.order_candidates) == 2


def test_rewrite_product_ref_empty_passes_through(manager: CaseManager):
    # 商品词没匹配到订单 → 放行（执行层列全部兜底），不索凭证
    execute = _fake_execute({"鞋": [{"name": "Nike 运动鞋"}]}, [])
    outcome = rewrite_query("我买的鞋到哪了", case_manager=manager, execute=execute)
    assert outcome.clarification is None
    assert outcome.message == "我买的鞋到哪了"


def test_rewrite_order_id_priority_over_product(manager: CaseManager):
    # 精确订单号优先于商品指代
    execute = _fake_execute({"鞋": [{"name": "Nike 运动鞋"}]}, [])
    outcome = rewrite_query("ORD-20240115-001 的鞋到哪了", case_manager=manager, execute=execute)
    assert outcome.source == "order_id"
    assert outcome.message == "ORD-20240115-001 的鞋到哪了"
    assert outcome.entities.order_id == "ORD-20240115-001"


# ----------------------------------------------------------------------
# 会话内指代：短期记忆回填订单号
# ----------------------------------------------------------------------

def test_has_context_reference():
    from app.intent.query_rewrite import has_context_reference

    assert has_context_reference("那它到哪了") is True
    assert has_context_reference("那个订单什么状态") is True
    assert has_context_reference("我买的鞋到哪了") is False  # 商品指代，非会话内指代
    assert has_context_reference("帮我查一下物流") is False


def test_recall_order_id_from_context():
    from app.intent.query_rewrite import recall_order_id_from_context

    context = [
        {"role": "user", "content": "查下 ORD-20240115-001 的物流"},
        {"role": "assistant", "content": "已为您查到，正在派送中"},
    ]
    assert recall_order_id_from_context(context) == "ORD-20240115-001"


def test_recall_order_id_picks_most_recent():
    from app.intent.query_rewrite import recall_order_id_from_context

    context = [
        {"role": "user", "content": "查 ORD-11111111-111 的物流"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "再看看 ORD-22222222-222"},
        {"role": "assistant", "content": "好的"},
    ]
    assert recall_order_id_from_context(context) == "ORD-22222222-222"


def test_recall_order_id_ignores_outside_window():
    from app.intent.query_rewrite import recall_order_id_from_context

    # 订单号在窗口（最近 4 条）之外 → 不召回
    context = [
        {"role": "user", "content": "查 ORD-11111111-111 的物流"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
        {"role": "user", "content": "在吗"},
    ]
    assert recall_order_id_from_context(context) is None


def test_recall_order_id_empty_context():
    from app.intent.query_rewrite import recall_order_id_from_context

    assert recall_order_id_from_context(None) is None
    assert recall_order_id_from_context([]) is None


def test_rewrite_context_reference_recalls_order_id(manager: CaseManager):
    context = [
        {"role": "user", "content": "查下 ORD-20240115-001 的物流"},
        {"role": "assistant", "content": "已查到"},
    ]
    outcome = rewrite_query(
        "那它到哪了", case_manager=manager, session_context=context,
    )
    assert outcome.source == "context"
    assert "ORD-20240115-001" in outcome.message
    assert outcome.entities.order_id == "ORD-20240115-001"


def test_rewrite_context_reference_without_order_id_passes(manager: CaseManager):
    # 短期记忆里没有订单号 → 放行（不误改）
    context = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "您好"}]
    outcome = rewrite_query("那个怎么样", case_manager=manager, session_context=context)
    assert outcome.source == "none"
    assert outcome.message == "那个怎么样"


def test_rewrite_cross_session_priority_over_context(manager: CaseManager):
    # "上次" 跨会话时间指代优先于会话内指代
    _open_refund_case(manager, "ORD-001")
    context = [{"role": "user", "content": "查 ORD-99999999-999 的物流"}]
    outcome = rewrite_query(
        "上次那个退款到账了吗", case_manager=manager, session_context=context,
    )
    assert outcome.source == "case"
    assert "ORD-001" in outcome.message


# ----------------------------------------------------------------------
# 意图感知分派：只在需要订单定位的意图下消歧
# ----------------------------------------------------------------------

def test_rewrite_skips_for_dialog_intent(manager: CaseManager):
    """对话流意图直接放行，不触发 LLM/工具，即使消息含商品词。"""
    from app.data_plan.enums import Intent

    calls: list = []

    def fake_disambiguator(text):
        calls.append(text)
        from app.intent.query_rewrite import DisambiguationHint
        return DisambiguationHint(product_keywords=("鞋",))

    outcome = rewrite_query(
        "这双鞋不错", intent=Intent.GREETINGS, case_manager=manager,
        disambiguator=fake_disambiguator,
    )
    assert outcome.source == "none"
    assert outcome.message == "这双鞋不错"
    assert calls == []  # 未触发指代消歧
    assert calls == []  # 未触发商品词抽取


def test_rewrite_skips_for_product_inquiry(manager: CaseManager):
    """商品咨询意图不做订单定位。"""
    from app.data_plan.enums import Intent

    outcome = rewrite_query(
        "这个耳机有货吗", intent=Intent.PRODUCT_INQUIRY, case_manager=manager,
    )
    assert outcome.source == "none"
    assert outcome.message == "这个耳机有货吗"


def test_rewrite_runs_for_order_intent(manager: CaseManager):
    """订单相关意图走完整消歧（这里验证商品指代定位）。"""
    from app.data_plan.enums import Intent

    execute = _fake_execute(
        {"鞋": [{"name": "Nike 运动鞋"}]},
        [{"order_id": "ORD-001", "items_summary": "Nike 运动鞋", "status": "已发货"}],
    )
    outcome = rewrite_query(
        "我买的鞋到哪了", intent=Intent.LOGISTICS_INQUIRY,
        case_manager=manager, execute=execute,
    )
    assert outcome.source == "product"
    assert "ORD-001" in outcome.message


def test_rewrite_none_intent_runs_full_disambiguation(manager: CaseManager):
    """未提供意图（None）走完整消歧（向后兼容）。"""
    execute = _fake_execute(
        {"鞋": [{"name": "Nike 运动鞋"}]},
        [{"order_id": "ORD-001", "items_summary": "Nike 运动鞋", "status": "已发货"}],
    )
    outcome = rewrite_query(
        "我买的鞋到哪了", case_manager=manager, execute=execute,
    )
    assert outcome.source == "product"
    assert "ORD-001" in outcome.message
