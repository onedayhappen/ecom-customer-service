"""评测沙箱必须覆盖线上执行前的查询改写层。"""

from app.evaluation.dataset import EvalCase
from app.evaluation.sandbox import Sandbox




def test_unified_sandbox_preserves_user_question(monkeypatch):
    from app.config.settings import settings
    from tests.test_conversation_agent import Client, reply
    client = Client([reply('请问您想了解发票的哪方面？')])
    case = EvalCase(id='invoice-question', description='咨询不强制定位订单',
                    turns=['个人和公司发票有什么区别？'], expected_intent='invoice_inquiry')
    trace = Sandbox(client=client, model='test').run(case)
    assert trace.error is None
    assert trace.execution == 'react'
    assert trace.rewrites[0]['source'] == 'conversation'
    assert trace.rewrites[0]['rewritten_input'] == case.turns[0]
