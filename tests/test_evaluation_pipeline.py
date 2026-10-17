"""评测沙箱必须覆盖线上执行前的查询改写层。"""

from app.evaluation.dataset import EvalCase
from app.evaluation.sandbox import Sandbox


def test_sandbox_records_query_rewrite_before_workflow_execution(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, 'assistant_just_react', False)
    case = EvalCase(
        id="eval-rewrite-order-id", description="精确订单号改写轨迹",
        turns=["查询 ORD-20240115-001 的订单状态"], expected_intent="order_inquiry",
    )

    trace = Sandbox(client=None).run(case)

    assert trace.error is None
    assert trace.rewrites == [{
        "raw_input": "查询 ORD-20240115-001 的订单状态",
        "rewritten_input": "查询 ORD-20240115-001 的订单状态",
        "source": "order_id",
        "clarification": None,
        "order_candidates": [],
    }]
    assert trace.execution == "workflow"


def test_unified_sandbox_preserves_user_question(monkeypatch):
    from app.config.settings import settings
    from tests.test_conversation_agent import Client, reply
    monkeypatch.setattr(settings, 'assistant_just_react', True)
    client = Client([reply('请问您想了解发票的哪方面？')])
    case = EvalCase(id='invoice-question', description='咨询不强制定位订单',
                    turns=['个人和公司发票有什么区别？'], expected_intent='invoice_inquiry')
    trace = Sandbox(client=client, model='test').run(case)
    assert trace.error is None
    assert trace.execution == 'react'
    assert trace.rewrites[0]['source'] == 'conversation'
    assert trace.rewrites[0]['rewritten_input'] == case.turns[0]
