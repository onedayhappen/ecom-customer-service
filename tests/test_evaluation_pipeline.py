"""评测沙箱必须覆盖线上执行前的查询改写层。"""

from app.evaluation.dataset import EvalCase
from app.evaluation.sandbox import Sandbox


def test_sandbox_records_query_rewrite_before_workflow_execution():
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
