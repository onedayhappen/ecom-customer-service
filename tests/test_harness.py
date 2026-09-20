"""Harness 工程（第13期）测试：故障注入 / 收敛检测 / fork 子进程隔离 / 生产集成。

覆盖点：
- FaultInjector：9 类故障（超时/连接失败/抖动/非法 JSON/缺字段/错误业务码/
  脏数据/429 限流/延迟）+ 工具名过滤 + 概率触发 + 挂到 McpToolClient
- ConvergenceDetector：轮次阈值 / 工具指纹 / 状态震荡 / 收敛提示 / 开关
- 生产集成：ReactExecutor 收敛终止返回固定回复；BaseStateMachine 震荡优雅停止
- ForkRunner：正常回传 / 错误回传 / 死循环超时 kill / 资源指标采集
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app.harness.convergence import (
    CONVERGENCE_PROMPT,
    CONVERGENCE_REPLY,
    ConvergenceDetector,
    ConvergenceReason,
)
from app.harness.faults import FaultInjector, FaultKind, FaultSpec, fault_client
from app.harness.fork_runner import ForkRunner
from app.observability.mcp_client import McpToolClient


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _fake_execute(name: str, arguments: dict) -> str:
    """模拟真实工具：返回固定 JSON（带 orders 字段）。"""
    return json.dumps({"success": True, "orders": [{"order_id": "O1"}]})


def _client_with(injector: FaultInjector) -> McpToolClient:
    return McpToolClient(execute=injector)


# ---------------------------------------------------------------------------
# 故障注入
# ---------------------------------------------------------------------------


class TestFaultInjector:
    def test_timeout_raises(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.TIMEOUT), target=_fake_execute,
        )
        client = _client_with(injector)
        with pytest.raises(TimeoutError):
            client.call_tool("query_order", {"order_id": "O1"})
        assert injector.injected == 1

    def test_connection_error_raises(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.CONNECTION_ERROR), target=_fake_execute,
        )
        with pytest.raises(ConnectionError):
            injector("query_logistics", {})

    def test_rate_limit_returns_transient(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.RATE_LIMIT), target=_fake_execute,
        )
        raw = injector("apply_refund", {"order_id": "O1"})
        data = json.loads(raw)
        assert data["success"] is False
        assert data["error_code"] == "transient_failure"  # ReActPolicy 会判瞬时重试
        assert "429" in data["error"]

    def test_bad_business_code(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.BAD_BUSINESS_CODE), target=_fake_execute,
        )
        data = json.loads(injector("order_cancel", {}))
        assert data["error_code"] == "business_rejection"

    def test_invalid_json(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.INVALID_JSON), target=_fake_execute,
        )
        raw = injector("query_order", {})
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_missing_field(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.MISSING_FIELD, field_name="orders"),
            target=_fake_execute,
        )
        data = json.loads(injector("list_user_orders", {}))
        assert "orders" not in data  # 合法 JSON 但缺关键字段

    def test_dirty_data(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.DIRTY_DATA, field_name="orders"),
            target=_fake_execute,
        )
        data = json.loads(injector("list_user_orders", {}))
        assert data["orders"] == "dirty-not-a-list"  # 类型错乱

    def test_delay(self):
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.DELAY, delay_ms=50), target=_fake_execute,
        )
        start = time.perf_counter()
        injector("query_order", {})
        assert (time.perf_counter() - start) * 1000 >= 45

    def test_jitter_ratio_all_or_none(self):
        # ratio=1：全注入；ratio=0：全透传（固定 rng 保证可复现）
        injector_all = FaultInjector(
            spec=FaultSpec(FaultKind.TIMEOUT, ratio=1.0),
            target=_fake_execute, rng=__import__("random").Random(42),
        )
        with pytest.raises(TimeoutError):
            injector_all("query_order", {})
        injector_none = FaultInjector(
            spec=FaultSpec(FaultKind.TIMEOUT, ratio=0.0),
            target=_fake_execute, rng=__import__("random").Random(42),
        )
        result = injector_none("query_order", {})
        assert json.loads(result)["success"] is True

    def test_tool_name_filter(self):
        # 只对 apply_* 注入：query_* 透传
        injector = FaultInjector(
            spec=FaultSpec(FaultKind.RATE_LIMIT, tools=(r"^apply_.*",)),
            target=_fake_execute,
        )
        assert json.loads(injector("query_order", {}))["success"] is True
        assert json.loads(injector("apply_refund", {}))["success"] is False

    def test_attach_replaces_mcp_client_execute(self):
        client = McpToolClient(execute=_fake_execute)
        injector = FaultInjector(spec=FaultSpec(FaultKind.TIMEOUT))
        injector.attach(client)
        with pytest.raises(TimeoutError):
            client.call_tool("query_order", {"order_id": "O1"})

    def test_fault_client_helper(self):
        client = fault_client(FaultSpec(FaultKind.RATE_LIMIT, tools=(r"apply_.*",)))
        data = client.call_tool("apply_refund", {"order_id": "O1"})
        assert json.loads(data)["error_code"] == "transient_failure"
        # 未命中的工具走真实 registry（query_product 存在 → 正常返回）
        assert client.call_tool("list_user_orders", {})  # 不抛异常即可


# ---------------------------------------------------------------------------
# 收敛检测
# ---------------------------------------------------------------------------


class TestConvergenceDetector:
    def test_max_steps_threshold(self):
        detector = ConvergenceDetector(max_steps=3)
        assert detector.record_agent_step() is None
        assert detector.record_agent_step() is None
        report = detector.record_agent_step()
        assert report is not None
        assert report.reason == ConvergenceReason.MAX_STEPS

    def test_tool_loop_fingerprint(self):
        detector = ConvergenceDetector(max_same_tool_calls=3)
        args = {"order_id": "NOT-EXIST"}
        assert detector.record_tool_call("query_order", args) is None
        assert detector.record_tool_call("query_order", args) is None
        report = detector.record_tool_call("query_order", args)
        assert report is not None
        assert report.reason == ConvergenceReason.TOOL_LOOP
        assert "query_order" in report.detail

    def test_tool_loop_resets_on_argument_change(self):
        detector = ConvergenceDetector(max_same_tool_calls=3)
        detector.record_tool_call("query_order", {"order_id": "A"})
        detector.record_tool_call("query_order", {"order_id": "B"})  # 参数变了 → 重置
        assert detector.record_tool_call("query_order", {"order_id": "B"}) is None

    def test_state_oscillation(self):
        detector = ConvergenceDetector(oscillation_threshold=2)
        assert detector.record_state_transition("A", "B") is None
        assert detector.record_state_transition("B", "A") is None
        assert detector.record_state_transition("A", "B") is None  # 第 1 次 A→B→A 回跳
        report = detector.record_state_transition("B", "A")        # 第 2 次连续回跳
        assert report is not None
        assert report.reason == ConvergenceReason.STATE_OSCILLATION

    def test_disabled_passes_through(self):
        detector = ConvergenceDetector(enabled=False, max_steps=1)
        assert detector.record_agent_step() is None

    def test_convergence_prompt_text(self):
        detector = ConvergenceDetector()
        report = detector.record_tool_call("query_order", {"order_id": "X"})
        report = detector.record_tool_call("query_order", {"order_id": "X"})
        report = detector.record_tool_call("query_order", {"order_id": "X"})
        prompt = detector.convergence_prompt(report)
        assert "停止调用工具" in prompt
        assert CONVERGENCE_PROMPT in prompt

    def test_reset_clears_state(self):
        detector = ConvergenceDetector(max_same_tool_calls=3)
        detector.record_tool_call("t", {})
        detector.record_tool_call("t", {})
        detector.reset()
        assert detector.record_tool_call("t", {}) is None  # streak 已清


# ---------------------------------------------------------------------------
# 生产集成：ReactExecutor 收敛终止
# ---------------------------------------------------------------------------


class TestReactConvergenceIntegration:
    def _react_executor(self, detector):
        """构造始终返回同一个工具调用的 fake LLM + fake execute。"""
        def fake_create(**kwargs):
            return SimpleNamespace(
                model="m",
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="查一下",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            type="function",
                            function=SimpleNamespace(
                                name="query_order",
                                arguments='{"order_id": "NOT-EXIST"}',
                            ),
                        )
                    ],
                ))],
            )

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
        from app.execution.react import ReactExecutor

        return ReactExecutor(
            client=client, model="m", temperature=0.0,
            execute=lambda name, args: '{"success": false, "error": "未找到订单"}',
            convergence_detector=detector,
        )

    def _plan(self):
        from app.data_plan import build_data_plan
        from app.data_plan.enums import Intent

        return build_data_plan(Intent.ORDER_INQUIRY)

    def test_react_terminates_with_convergence_reply(self):
        detector = ConvergenceDetector(max_same_tool_calls=3)
        executor = self._react_executor(detector)
        result = executor.run([{"role": "user", "content": "查订单 NOT-EXIST"}], self._plan())
        assert result.reply == CONVERGENCE_REPLY
        assert len(result.tool_steps) <= 3  # 未无限循环

    def test_react_default_no_detector_unchanged(self):
        executor = self._react_executor(None)
        result = executor.run([{"role": "user", "content": "查订单"}], self._plan())
        assert result.reply != CONVERGENCE_REPLY  # 默认不触发收敛，走正常循环


# ---------------------------------------------------------------------------
# 生产集成：BaseStateMachine 震荡终止
# ---------------------------------------------------------------------------


class TestFSMConvergenceIntegration:
    def test_machine_stops_on_oscillation(self):
        detector = ConvergenceDetector(oscillation_threshold=2)
        from app.observability.fsm import BaseStateMachine

        class OscillatingMachine(BaseStateMachine):
            def step(self, state: str):
                # A→B→A 来回跳转
                return None, "B" if state == "A" else "A"

        machine = OscillatingMachine(name="osc", convergence_detector=detector)
        machine.run()
        assert machine.converged  # 优雅停止，不抛异常
        assert "state_oscillation" in machine._convergence_reason  # noqa: SLF001

    def test_machine_without_detector_runs_forever_budget(self):
        # 无检测器时，用内置步数限制避免测试挂死（正常状态机不设限）
        from app.observability.fsm import BaseStateMachine

        class OscillatingMachine(BaseStateMachine):
            def __init__(self):
                super().__init__(name="osc")
                self.n = 0

            def step(self, state: str):
                self.n += 1
                if self.n > 10:
                    return None, BaseStateMachine.STOP
                return None, "B" if state == "A" else "A"

        machine = OscillatingMachine()
        machine.run()
        assert not machine.converged
        assert machine.n == 11  # 跑到内置上限（检测器未启用）


# ---------------------------------------------------------------------------
# ForkRunner：进程隔离 + 超时 kill + 资源采集
# ---------------------------------------------------------------------------


class TestForkRunner:
    def test_normal_case_returns(self):
        runner = ForkRunner(timeout_s=10)
        result = runner.run_case("app.harness.test_procs:normal_case", (0.05,))
        assert result.ok
        assert result.value == "case-ok"
        assert result.duration_ms > 0

    def test_error_case_reported(self):
        runner = ForkRunner(timeout_s=10)
        result = runner.run_case("app.harness.test_procs:raise_error")
        assert not result.ok
        assert "case boom" in (result.error or "")

    def test_infinite_loop_killed_by_timeout(self):
        runner = ForkRunner(timeout_s=1.0)
        result = runner.run_case("app.harness.test_procs:infinite_loop")
        assert result.timed_out
        assert not result.ok
        assert "超时" in (result.error or "")
        assert result.duration_ms >= 900  # 确实等了超时阈值

    def test_resources_collected(self):
        runner = ForkRunner(timeout_s=10)
        result = runner.run_case("app.harness.test_procs:memory_allocator", (4,))
        assert result.ok
        # Unix 上报 maxrss_kb；Windows/psutil 上报 rss_kb；都不支持则空
        assert result.resources != {}
        any_key = ("maxrss_kb", "rss_kb")
        assert any(k in result.resources for k in any_key) or result.resources == {}

    def test_batch_does_not_block_on_deadlock(self):
        runner = ForkRunner(timeout_s=1.0)
        entries = [
            ("app.harness.test_procs:normal_case", (0.01,), None),
            ("app.harness.test_procs:infinite_loop", (), None),   # 死循环 → kill
            ("app.harness.test_procs:normal_case", (0.01,), None),
        ]
        results = runner.run_cases(entries)
        assert [r.timed_out for r in results] == [False, True, False]
        assert results[2].ok  # 第三条正常跑完，未被死循环堵死
