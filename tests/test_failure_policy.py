# 文件说明：测试执行失败分类、有限重试与熔断策略的状态流转。
"""ReActPolicy / FailureClassifier 纯状态机单元测试。

不依赖 LLM 与真实工具——故障注入评测可直接复用这里的构造方式。
"""

from __future__ import annotations

from app.reliability.failure_policy import (
    Action,
    FailureCategory,
    FailureClassifier,
    ReActPolicy,
)

READ_TOOL = "query_order"
WRITE_TOOL = "apply_refund"


def _policy() -> ReActPolicy:
    return ReActPolicy(mutating_tools=frozenset({WRITE_TOOL}))


def _result(error: str, code: str | None = None) -> dict:
    payload = {"success": False, "error": error}
    if code:
        payload["error_code"] = code
    return payload


# ---------------------------------------------------------------------------
# FailureClassifier：error_code 权威映射
# ---------------------------------------------------------------------------

class TestClassifier:
    def test_success_and_non_dict(self):
        clf = FailureClassifier()
        assert clf.classify({"success": True, "order": {}}) is FailureCategory.SUCCESS
        assert clf.classify({}) is FailureCategory.SUCCESS

    def test_error_code_is_authoritative(self):
        clf = FailureClassifier()
        # error_code 优先于错误文本（文本看起来像超时，但权威码是业务拒绝）
        r = _result("订单已发货不可取消", "business_rejection")
        assert clf.classify(r) is FailureCategory.BUSINESS_REJECTION
        assert clf.classify(_result("x", "validation_error")) is FailureCategory.VALIDATION_ERROR
        assert clf.classify(_result("x", "transient_failure")) is FailureCategory.TRANSIENT_FAILURE
        assert clf.classify(_result("x", "evidence_unavailable")) is FailureCategory.EVIDENCE_UNAVAILABLE
        assert clf.classify(_result("x", "evidence_conflict")) is FailureCategory.EVIDENCE_CONFLICT

    def test_confirmation_token_rejection(self):
        clf = FailureClassifier()
        r = _result("确认令牌无效、已过期或与当前操作不匹配")
        assert clf.classify(r) is FailureCategory.CONFIRMATION_INVALID

    def test_text_fallback_for_legacy_tools(self):
        clf = FailureClassifier()
        assert clf.classify(_result("网络连接超时")) is FailureCategory.TRANSIENT_FAILURE
        assert clf.classify(_result("请提供订单号或物流单号")) is FailureCategory.VALIDATION_ERROR

    def test_unknown_failure_defaults_to_evidence_unavailable(self):
        # fail-closed：无法识别的失败按无证据处理，不重试
        clf = FailureClassifier()
        assert clf.classify(_result("一些奇怪的错误")) is FailureCategory.EVIDENCE_UNAVAILABLE


# ---------------------------------------------------------------------------
# 瞬时失败：读工具重试一次，写工具查幂等
# ---------------------------------------------------------------------------

class TestTransientFailure:
    def test_read_tool_gets_single_retry(self):
        policy = _policy()
        d1 = policy.after_call(READ_TOOL, {"order_id": "ORD-001"}, _result("网络超时", "transient_failure"))
        assert d1.action is Action.RETRY_ONCE

        # 重试后仍失败：预算耗尽，计入连续失败，交还模型调整
        d2 = policy.after_call(READ_TOOL, {"order_id": "ORD-001"}, _result("网络超时", "transient_failure"))
        assert d2.action is Action.CONTINUE
        assert policy.consecutive_failures == 1

    def test_retry_budget_not_reset_within_run(self):
        policy = _policy()
        args = {"order_id": "ORD-001"}
        transient = _result("超时", "transient_failure")
        policy.after_call(READ_TOOL, args, transient)          # RETRY_ONCE
        policy.after_call(READ_TOOL, args, transient)          # CONTINUE（计数 1）
        policy.after_call(READ_TOOL, args, {"success": True})  # 成功清零
        d = policy.after_call(READ_TOOL, args, transient)
        # 同一轮运行内重试预算已耗尽，不再授予 RETRY_ONCE
        assert d.action is Action.CONTINUE

    def test_write_tool_never_auto_retries(self):
        policy = _policy()
        d = policy.after_call(WRITE_TOOL, {"order_id": "ORD-001"}, _result("下游超时", "transient_failure"))
        assert d.action is Action.CHECK_IDEMPOTENCY


# ---------------------------------------------------------------------------
# 连续失败双阈值：3 次终止
# ---------------------------------------------------------------------------

class TestConsecutiveFailures:
    def test_success_resets_counter(self):
        policy = _policy()
        policy.after_call(READ_TOOL, {"a": 1}, _result("x", "validation_error"))
        policy.after_call(READ_TOOL, {"a": 2}, _result("x", "validation_error"))
        assert policy.consecutive_failures == 2
        policy.after_call(READ_TOOL, {"a": 3}, {"success": True})
        assert policy.consecutive_failures == 0

    def test_validation_errors_terminate_with_clarify(self):
        policy = _policy()
        args = {"n": 1}
        d1 = policy.after_call(READ_TOOL, args, _result("缺少参数", "validation_error"))
        assert d1.action is Action.CONTINUE
        policy.after_call(READ_TOOL, {"n": 2}, _result("缺少参数", "validation_error"))
        d3 = policy.after_call(READ_TOOL, {"n": 3}, _result("缺少参数", "validation_error"))
        assert d3.action is Action.STOP_CLARIFY

    def test_evidence_conflict_terminates_with_handoff(self):
        policy = _policy()
        for i in range(3):
            d = policy.after_call(READ_TOOL, {"n": i}, _result("状态冲突", "evidence_conflict"))
        assert d.action is Action.STOP_HANDOFF

    def test_business_rejection_terminates_with_evidence(self):
        policy = _policy()
        for i in range(3):
            d = policy.after_call(WRITE_TOOL, {"n": i}, _result("订单已发货不可取消", "business_rejection"))
        assert d.action is Action.STOP_WITH_EVIDENCE

    def test_transient_exhausted_terminates_with_handoff(self):
        policy = _policy()
        transient = _result("超时", "transient_failure")
        decisions = [
            policy.after_call(READ_TOOL, {"n": i}, transient)
            for i in range(4)
        ]
        # 第 1 次：RETRY_ONCE（不计数）；第 2、3 次：计数；第 4 次：达到阈值终止
        assert decisions[0].action is Action.RETRY_ONCE
        assert decisions[1].action is Action.CONTINUE
        assert decisions[2].action is Action.CONTINUE
        assert decisions[3].action is Action.STOP_HANDOFF


# ---------------------------------------------------------------------------
# 确认令牌：立即回到确认流程
# ---------------------------------------------------------------------------

class TestConfirmationInvalid:
    def test_immediate_reconfirm_without_counting(self):
        policy = _policy()
        d = policy.after_call(WRITE_TOOL, {"order_id": "ORD-001"}, _result("确认令牌无效、已过期或与当前操作不匹配"))
        assert d.action is Action.STOP_RECONFIRM
        assert policy.consecutive_failures == 0


# ---------------------------------------------------------------------------
# 重复调用：同工具+同参数
# ---------------------------------------------------------------------------

class TestDuplicateCalls:
    def test_same_args_returns_cached(self):
        policy = _policy()
        args = {"order_id": "ORD-001"}
        policy.after_call(READ_TOOL, args, {"success": True, "order": {"order_id": "ORD-001"}})

        pre = policy.before_call(READ_TOOL, args)
        assert pre.action is Action.RETURN_CACHED
        assert pre.cached_result["success"] is True

    def test_same_tool_different_args_executes(self):
        policy = _policy()
        policy.after_call(READ_TOOL, {"order_id": "ORD-001"}, {"success": True})
        pre = policy.before_call(READ_TOOL, {"order_id": "ORD-002"})
        assert pre.action is Action.CONTINUE

    def test_failed_result_also_cached_for_duplicate_check(self):
        policy = _policy()
        args = {"order_id": "ORD-404"}
        policy.after_call(READ_TOOL, args, _result("未找到订单", "evidence_unavailable"))
        pre = policy.before_call(READ_TOOL, args)
        assert pre.action is Action.RETURN_CACHED
        assert pre.cached_result["success"] is False

    def test_three_consecutive_duplicates_stop_with_evidence(self):
        policy = _policy()
        args = {"order_id": "ORD-001"}
        policy.after_call(READ_TOOL, args, {"success": True})
        assert policy.before_call(READ_TOOL, args).action is Action.RETURN_CACHED
        assert policy.before_call(READ_TOOL, args).action is Action.RETURN_CACHED
        pre = policy.before_call(READ_TOOL, args)
        assert pre.action is Action.STOP_WITH_EVIDENCE
