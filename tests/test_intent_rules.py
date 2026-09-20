"""规则意图识别回归测试：覆盖自然说法与显式订单号兜底。"""

from __future__ import annotations

import pytest

from app.data_plan.enums import Intent
from app.intent.rules import recognize


@pytest.mark.parametrize("text,expected", [
    ("帮我看下 ORD-20240125-006 这单现在什么状态", Intent.ORDER_INQUIRY),
    ("帮我查 ORD-20240125-006", Intent.ORDER_INQUIRY),
    ("Nike Air Max 270 还有 42 码吗", Intent.PRODUCT_INQUIRY),
    ("MUJI 新疆棉四件套有灰色的吗，1.8米床能用吗", Intent.PRODUCT_INQUIRY),
    ("新疆地区包邮吗，大概几天送到", Intent.PRODUCT_INQUIRY),
    ("ORD-20240118-004 的退款怎么还没到", Intent.REFUND_STATUS),
    ("退款几天到账", Intent.REFUND_STATUS),
])
def test_rules_recognize_natural_phrasings(text: str, expected: Intent) -> None:
    assert recognize(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("我的快递到哪了", Intent.LOGISTICS_INQUIRY),
    ("快递送到哪了", Intent.LOGISTICS_INQUIRY),
    ("ORD-20240115-001 的物流", Intent.LOGISTICS_INQUIRY),
    ("这个耳机有货吗", Intent.PRODUCT_INQUIRY),
    ("退款到账了吗", Intent.REFUND_STATUS),
    ("我要退款", Intent.AFTER_SALE_REQUEST),
    ("我要投诉", Intent.COMPLAINT),
    ("你好", Intent.GREETINGS),
    ("谢谢", Intent.CLOSURE),
])
def test_rules_recognize_regressions(text: str, expected: Intent) -> None:
    assert recognize(text) == expected


def test_rules_recognize_empty_input() -> None:
    assert recognize("") == Intent.OTHERS


@pytest.mark.parametrize("text", [
    "先别查物流了，查一下耳机订单的金额和明细",
    "好的，运单号收到了。回到刚才吸尘器那单，再查一下当前状态，我怕弄混",
])
def test_rules_use_latest_actionable_clause(text: str) -> None:
    """背景中的旧物流词不能覆盖本轮明确的新订单查询。"""
    assert recognize(text) == Intent.ORDER_INQUIRY


def test_rules_keep_logistics_clause_before_delivery_question() -> None:
    assert recognize("这单 ORD-ADMIN-001 的物流到哪了，明天能到吗") == Intent.LOGISTICS_INQUIRY


def test_rules_keep_request_after_logistics_condition_clause() -> None:
    assert recognize("如果查不到轨迹，就给我这单 ORD-ADMIN-002 的快递公司和运单号") == Intent.LOGISTICS_INQUIRY
