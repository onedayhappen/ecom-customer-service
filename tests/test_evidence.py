"""Evidence Pack 校验：实体提取、证据比对与保守替换。"""

from __future__ import annotations

from app.reliability.evidence import (
    UNVERIFIED_SENTENCE,
    extract_entities,
    sanitize_reply,
    verify_reply,
)


def _evidence(*results: dict) -> list[dict]:
    return list(results)


# ---------------------------------------------------------------------------
# 实体提取
# ---------------------------------------------------------------------------

class TestExtractEntities:
    def test_order_ids_normalized_to_upper(self):
        entities = extract_entities("已取消订单 ord-001，订单 ORD-002 保持不变")
        assert entities["order_ids"] == ["ORD-001", "ORD-002"]

    def test_amounts_with_currency_variants(self):
        entities = extract_entities("共支付 ¥129.00，运费￥12.5")
        assert len(entities["amounts"]) == 2

    def test_status_keywords(self):
        entities = extract_entities("您的订单已发货，预计明天送达")
        assert "已发货" in entities["statuses"]

    def test_empty_text(self):
        assert extract_entities("") == {"order_ids": [], "amounts": [], "statuses": []}


# ---------------------------------------------------------------------------
# 证据比对
# ---------------------------------------------------------------------------

class TestVerifyReply:
    def test_supported_entities(self):
        evidence = _evidence({
            "success": True,
            "order": {"order_id": "ORD-001", "status": "已发货", "total": 129.0},
        })
        report = verify_reply("订单 ORD-001 已发货，金额 ¥129。", evidence)
        assert not report.has_unsupported
        assert len(report.supported) == 3

    def test_unsupported_order_id(self):
        evidence = _evidence({"success": True, "order": {"order_id": "ORD-001"}})
        report = verify_reply("订单 ORD-999 已取消。", evidence)
        assert report.has_unsupported
        assert "ORD-999" in report.unsupported

    def test_amount_normalization_matches_evidence(self):
        # 回复 ¥129.00 / ¥1,299，证据中是数字 129.0 / 1299
        evidence = _evidence({"success": True, "total": 129.0, "freight": 1299})
        report = verify_reply("支付了 ¥129.00 和 ¥1,299。", evidence)
        assert not report.has_unsupported

    def test_no_evidence_at_all(self):
        report = verify_reply("订单 ORD-001 已签收。", [])
        assert report.has_unsupported
        assert len(report.unsupported) == 2  # 订单号 + 状态


# ---------------------------------------------------------------------------
# 保守替换
# ---------------------------------------------------------------------------

class TestSanitizeReply:
    def test_unsupported_sentence_replaced(self):
        evidence = _evidence({"success": True, "order": {"order_id": "ORD-001", "status": "已发货"}})
        reply = "订单 ORD-001 已发货。另外订单 ORD-999 已取消。"
        report = verify_reply(reply, evidence)
        sanitized = sanitize_reply(reply, report)
        assert "ORD-001" in sanitized
        assert "ORD-999 已取消" not in sanitized
        assert UNVERIFIED_SENTENCE in sanitized

    def test_supported_reply_unchanged(self):
        evidence = _evidence({"success": True, "order": {"order_id": "ORD-001", "status": "已发货"}})
        reply = "您的订单 ORD-001 已发货。"
        report = verify_reply(reply, evidence)
        assert sanitize_reply(reply, report) == reply

    def test_all_unsupported_all_replaced(self):
        report = verify_reply("订单 ORD-001 已签收。金额 ¥500 已退回。", [])
        sanitized = sanitize_reply("订单 ORD-001 已签收。金额 ¥500 已退回。", report)
        assert "ORD-001" not in sanitized
        assert sanitized.count(UNVERIFIED_SENTENCE) == 2
