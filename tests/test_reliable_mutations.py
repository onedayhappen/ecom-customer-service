from __future__ import annotations

import threading

from app.memory.case_manager import CaseManager
from app.reliability import MutationGuard


def _guard(tmp_path):
    return MutationGuard(CaseManager("user-1", str(tmp_path)), "session-1")


def test_confirmation_token_is_bound_to_action_and_arguments(tmp_path):
    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    token = guard.request_confirmation("after_sale_request", "apply_refund", args)

    calls: list[tuple] = []
    outcome = guard.execute(
        "after_sale_request", "apply_refund",
        {"order_id": "ORD-001", "reason": "质量问题"}, token,
        lambda name, payload: calls.append((name, payload)) or {"success": True},
    )

    assert outcome.result["success"] is False
    assert "不匹配" in outcome.result["error"]
    assert calls == []


def test_mutation_executes_once_and_is_audited(tmp_path):
    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    token = guard.request_confirmation("after_sale_request", "apply_refund", args)
    calls: list[tuple] = []

    first = guard.execute(
        "after_sale_request", "apply_refund", args, token,
        lambda name, payload: calls.append((name, payload)) or {"success": True, "message": "已提交"},
    )

    assert first.result["success"] is True
    assert calls == [("apply_refund", args)]
    events = [event["event_type"] for event in guard.case_manager.events]
    assert events[-3:] == ["confirmation_requested", "mutation_started", "mutation_succeeded"]


def test_repeated_request_with_new_confirmation_replays_successful_result(tmp_path):
    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    calls: list[tuple] = []
    first_token = guard.request_confirmation("after_sale_request", "apply_refund", args)
    guard.execute(
        "after_sale_request", "apply_refund", args, first_token,
        lambda name, payload: calls.append((name, payload)) or {"success": True, "message": "已提交"},
    )

    second_token = guard.request_confirmation("after_sale_request", "apply_refund", args)
    replay = guard.execute(
        "after_sale_request", "apply_refund", args, second_token,
        lambda name, payload: calls.append((name, payload)) or {"success": True},
    )

    assert replay.replayed is True
    assert replay.result["message"] == "已提交"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 失败后重试：幂等记录的三状态语义
# ---------------------------------------------------------------------------

def test_failed_mutation_allows_retry_with_new_confirmation(tmp_path):
    """下游超时/失败后，旧令牌不可复用；用新确认令牌重试并成功。

    幂等记录的三状态语义：
      - succeeded → 重放缓存结果，不再调用下游
      - in_progress → 拒绝并发重复
      - failed → 不命中缓存，允许重新执行（但需要新确认令牌）
    """
    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    calls: list[tuple] = []

    # --- 第一次：下游超时 ---
    token1 = guard.request_confirmation("after_sale_request", "apply_refund", args)
    first = guard.execute(
        "after_sale_request", "apply_refund", args, token1,
        lambda n, p: calls.append((n, p)) or {"success": False, "error": "工具超时"},
    )
    assert first.result["success"] is False
    assert first.result["error"] == "工具超时"
    assert first.replayed is False
    assert len(calls) == 1

    # 旧令牌已消费（used=True），直接复用应被拒绝
    stale = guard.execute(
        "after_sale_request", "apply_refund", args, token1,
        lambda n, p: calls.append((n, p)) or {"success": True},
    )
    assert stale.result["success"] is False
    assert "不匹配" in stale.result["error"]
    assert len(calls) == 1  # 工具没有被再次调用

    # --- 第二次：新确认令牌 → 重新执行并成功 ---
    token2 = guard.request_confirmation("after_sale_request", "apply_refund", args)
    second = guard.execute(
        "after_sale_request", "apply_refund", args, token2,
        lambda n, p: calls.append((n, p)) or {"success": True, "message": "已提交"},
    )
    assert second.result["success"] is True
    assert second.replayed is False  # 不是重放，是真实重新执行
    assert len(calls) == 2  # 工具被调用了两次

    # --- 第三次：成功后重放，重放的是成功结果而非失败结果 ---
    token3 = guard.request_confirmation("after_sale_request", "apply_refund", args)
    replay = guard.execute(
        "after_sale_request", "apply_refund", args, token3,
        lambda n, p: calls.append((n, p)) or {"success": True, "error": "不应该被调用"},
    )
    assert replay.replayed is True
    assert replay.result["message"] == "已提交"
    assert "error" not in replay.result  # 重放的是第二次的成功结果，不是第一次的失败
    assert len(calls) == 2  # 工具仍然只被调用两次

    # 审计事件：失败和成功都记录了，且失败在成功之前
    mutation_events = [
        e["event_type"] for e in guard.case_manager.events
        if e["event_type"].startswith("mutation_")
    ]
    assert "mutation_failed" in mutation_events
    assert "mutation_succeeded" in mutation_events
    assert mutation_events.index("mutation_failed") < mutation_events.index("mutation_succeeded")
    assert "mutation_replayed" in mutation_events
    assert mutation_events.index("mutation_succeeded") < mutation_events.index("mutation_replayed")


# ---------------------------------------------------------------------------
# 并发保护：in_progress 状态拒绝第二个请求
# ---------------------------------------------------------------------------

def test_in_progress_rejects_concurrent_duplicate(tmp_path):
    """同一个幂等键处于 in_progress 时，第二个并发请求被拒绝。

    场景：线程 A 的 execute 已通过确认验证并开始调用下游工具（锁已释放），
    线程 B 在此期间获取了新的确认令牌并尝试 execute —— 应命中 in_progress
    检查并返回"正在处理中"，而不是再次调用下游工具。
    """
    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    token1 = guard.request_confirmation("after_sale_request", "apply_refund", args)

    entered_execute = threading.Event()
    allow_complete = threading.Event()
    second_outcome: dict = {}

    def slow_tool(name, payload):
        """模拟下游工具执行耗时：通知线程 B 可以发起请求，然后等待 B 完成。"""
        entered_execute.set()
        allow_complete.wait(timeout=5)
        return {"success": True, "message": "已提交"}

    def second_call():
        # 等待线程 A 进入工具执行阶段（此时锁已释放，in_progress 已写入）
        entered_execute.wait(timeout=5)
        # 线程 B 获取新的确认令牌（覆写 pending_mutation，used=False）
        token2 = guard.request_confirmation("after_sale_request", "apply_refund", args)
        # 线程 B 尝试执行 —— 应被 in_progress 拒绝
        second_outcome["result"] = guard.execute(
            "after_sale_request", "apply_refund", args, token2,
            lambda n, p: {"success": True, "error": "不应该被调用"},
        )
        allow_complete.set()

    t = threading.Thread(target=second_call)
    t.start()

    first = guard.execute(
        "after_sale_request", "apply_refund", args, token1,
        slow_tool,
    )
    t.join(timeout=10)

    # 线程 A 正常完成
    assert first.result["success"] is True
    assert first.result["message"] == "已提交"

    # 线程 B 被拒绝
    assert "result" in second_outcome, "线程 B 未在超时内完成"
    second = second_outcome["result"]
    assert second.result["success"] is False
    assert "处理中" in second.result["error"]
    assert second.replayed is False


# ---------------------------------------------------------------------------
# 写操作瞬时失败：状态未知而非失败，禁止自动重试
# ---------------------------------------------------------------------------

def test_transient_failure_on_mutation_records_unknown_and_locks(tmp_path):
    """下游超时（error_code=transient_failure）→ 记录 unknown → 后续执行被锁定转人工。"""
    from datetime import timedelta
    guard = MutationGuard(CaseManager("user-1", str(tmp_path)), "session-1", in_progress_stale_after=timedelta(seconds=60))
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    token = guard.request_confirmation("after_sale_request", "apply_refund", args)

    timeout_result = {"success": False, "error": "下游服务超时", "error_code": "transient_failure"}
    first = guard.execute("after_sale_request", "apply_refund", args, token, lambda n, p: timeout_result)
    assert first.result["success"] is False

    # 幂等状态查询：应为 unknown（不是 failed）
    status = guard.query_idempotency_status("after_sale_request", "apply_refund", args)
    assert status["status"] == "unknown"

    # 新确认令牌也无法再次执行：unknown 锁定，只能人工介入
    token2 = guard.request_confirmation("after_sale_request", "apply_refund", args)
    retry = guard.execute("after_sale_request", "apply_refund", args, token2, lambda n, p: {"success": True})
    assert retry.result["success"] is False
    assert "状态未知" in retry.result["error"]

    # 审计记录了 mutation_unknown 事件
    events = [e["event_type"] for e in guard.case_manager.events]
    assert "mutation_unknown" in events


def test_known_failure_still_records_failed_and_allows_retry(tmp_path):
    """非瞬时失败（如业务拒绝）仍记录 failed，保持失败后重新确认即可重试。"""
    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    token = guard.request_confirmation("after_sale_request", "apply_refund", args)

    rejection = {"success": False, "error": "订单已发货不可取消", "error_code": "business_rejection"}
    guard.execute("after_sale_request", "apply_refund", args, token, lambda n, p: rejection)

    status = guard.query_idempotency_status("after_sale_request", "apply_refund", args)
    assert status["status"] == "failed"


def test_exception_after_business_effect_remains_unknown(tmp_path):
    guard = _guard(tmp_path)
    args = {'order_id': 'ORD-001', 'reason': '不想要了'}
    calls = []
    def lost_response(name, params):
        calls.append(params)
        raise TimeoutError('business committed, response lost')
    token = guard.request_confirmation('after_sale_request', 'apply_refund', args)
    guard.execute('after_sale_request', 'apply_refund', args, token, lost_response)
    assert guard.query_idempotency_status('after_sale_request', 'apply_refund', args)['status'] == 'unknown'
    fresh = guard.request_confirmation('after_sale_request', 'apply_refund', args)
    guard.execute('after_sale_request', 'apply_refund', args, fresh, lost_response)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# query_idempotency_status：五种状态
# ---------------------------------------------------------------------------

def test_query_idempotency_status_states(tmp_path):
    from datetime import datetime, timedelta

    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}

    # 1. 从未执行 → not_found（且不创建 Case，无副作用）
    assert guard.query_idempotency_status("after_sale_request", "apply_refund", args) == {"status": "not_found"}
    assert guard.case_manager.find_case_for_mutation("after_sale_request", "ORD-001") is None

    # 2. 成功 → succeeded + 结果
    token = guard.request_confirmation("after_sale_request", "apply_refund", args)
    guard.execute("after_sale_request", "apply_refund", args, token,
                  lambda n, p: {"success": True, "message": "已提交"})
    status = guard.query_idempotency_status("after_sale_request", "apply_refund", args)
    assert status["status"] == "succeeded"
    assert status["result"]["message"] == "已提交"


def test_stale_in_progress_rejected_and_reported(tmp_path):
    """in_progress 超过 TTL：execute 拒绝并提示状态未知；查询报告 stale=True。

    模拟进程在工具执行阶段崩溃：手工构造一条时间戳回拨的 in_progress
    遗留记录（正常流程中终态总会被写入，崩溃时来不及）。
    """
    from datetime import datetime, timedelta

    guard = _guard(tmp_path)
    args = {"order_id": "ORD-001", "reason": "不想要了"}
    guard.request_confirmation("after_sale_request", "apply_refund", args)

    case = guard.case_manager.open_case_for_mutation("after_sale_request", "ORD-001")
    key = guard._key(case.case_id, "apply_refund", args)
    case.conversation_state.setdefault("idempotency", {})[key] = {
        "status": "in_progress", "action": "apply_refund", "arguments": args,
        "started_at": (datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds"),
    }

    # 查询：in_progress 且 stale=True
    status = guard.query_idempotency_status("after_sale_request", "apply_refund", args)
    assert status["status"] == "in_progress"
    assert status["stale"] is True

    # 新令牌重试：被拒绝（状态未知，不允许自动重试）
    calls: list[tuple] = []
    token2 = guard.request_confirmation("after_sale_request", "apply_refund", args)
    retry = guard.execute("after_sale_request", "apply_refund", args, token2,
                          lambda n, p: calls.append((n, p)) or {"success": True})
    assert retry.result["success"] is False
    assert "状态未知" in retry.result["error"]
    assert calls == []  # 工具没有被执行
