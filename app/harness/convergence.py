"""收敛提示（Harness 核心能力之二）：检测 Agent 死循环 / ReAct 无限循环 /
FSM 来回跳转。

解决的三种死循环形态：

1. **ReAct**：LLM 反复调用同一个工具，得不到结果不停循环；
2. **FSM 状态机**：A→B→A 来回跳转，状态震荡死锁；
3. **不停重试工具**，没有退出路径。

内置收敛判定规则（:class:`ConvergenceDetector`）：

1. **最大轮次阈值**：限制「思考 + 工具调用」总轮数，超过即判定不收敛；
2. **状态循环检测**：记录 FSM 状态流转序列，出现重复环（stateA→stateB→
   stateA）判定震荡；
3. **工具调用指纹检测**：连续多次调用相同工具 + 相同参数，判定无效循环；
4. **收敛提示**：检测到即将循环时输出收敛提示（:data:`CONVERGENCE_PROMPT`），
   强制 Agent 跳出循环，引导它向用户澄清 / 终止流程，而不是无限调用工具。

区分两个使用场景：

- **测试时**：检测到循环 → 测试用例失败，输出报告，定位 bug；
- **生产运行时**：ReAct 执行器 / 状态机基类复用同一套检测逻辑
  （``react.ReactExecutor(convergence_detector=...)`` /
  ``fsm.BaseStateMachine(convergence_detector=...)``），触发后追加收敛提示、
  终止循环，避免服务卡死。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

#: 收敛提示 prompt（给 LLM 的追加指令）：强制停止调用工具、向用户说明情况。
CONVERGENCE_PROMPT = (
    "多次调用工具未得到有效结果，请停止调用工具，"
    "直接向用户说明情况，不要继续重试。"
)

#: 终止循环后给用户的固定回复（不经过 LLM，保证语义稳定）。
CONVERGENCE_REPLY = (
    "抱歉，系统检测到多次尝试均未获得有效结果，为避免无限循环已停止自动处理。"
    "请您稍后重试，或联系人工客服核实。"
)


class ConvergenceReason(str, Enum):
    """不收敛的判定原因。"""

    MAX_STEPS = "max_steps"                  # 超过最大轮次阈值
    TOOL_LOOP = "tool_loop"                  # 相同工具 + 相同参数连续重复
    STATE_OSCILLATION = "state_oscillation"  # FSM 状态 A→B→A 震荡


@dataclass
class ConvergenceReport:
    """一次不收敛判定的完整报告（测试失败定位 / 生产终止原因）。"""

    reason: ConvergenceReason
    detail: str
    steps: int = 0
    tool_calls: int = 0
    streak: int = 0

    @property
    def failed(self) -> bool:
        """测试语义：判定不收敛 = 用例失败。"""
        return True


def _fingerprint(arguments: dict[str, Any]) -> str:
    """参数指纹：排序后 JSON 序列化，相同参数得到相同指纹。"""
    return json.dumps(
        arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


class ConvergenceDetector:
    """收敛检测器：三类规则的统一入口。

    :param max_steps: 最大「思考 + 工具调用」轮次阈值（默认 8）。
    :param max_same_tool_calls: 相同工具 + 相同参数的连续次数阈值（默认 3）。
    :param oscillation_threshold: 状态 A→B→A 回跳的连续出现次数阈值（默认 2）。
    :param enabled: False 时全部检测直接放行（生产默认不启用，测试按需开启）。

    用法（ReAct 执行器每轮）：:

        detector = ConvergenceDetector(max_steps=5)
        for _ in range(...):
            report = detector.record_agent_step()          # 轮次阈值
            if report: break
            ...
            report = detector.record_tool_call(name, args) # 工具指纹
            if report: break

    用法（FSM 状态机每次跳转）：:

        detector.record_state_transition(from_state, to_state)  # 状态震荡
    """

    def __init__(
        self,
        *,
        max_steps: int = 8,
        max_same_tool_calls: int = 3,
        oscillation_threshold: int = 2,
        enabled: bool = True,
    ):
        self.max_steps = max(1, int(max_steps))
        self.max_same_tool_calls = max(2, int(max_same_tool_calls))
        self.oscillation_threshold = max(1, int(oscillation_threshold))
        self.enabled = enabled

        self.steps = 0
        self.tool_calls: list[tuple[str, str]] = []      # (name, fingerprint)
        self.state_seq: list[str] = ["INIT"]             # 状态流转序列
        self._same_streak = 0
        self._last_fingerprint: tuple[str, str] | None = None
        self._oscillation_streak = 0
        self._triggered: Optional[ConvergenceReport] = None

    # ------------------------------------------------------------------
    # 规则 1：最大轮次阈值
    # ------------------------------------------------------------------

    def record_agent_step(self) -> Optional[ConvergenceReport]:
        """记录一轮「思考 + 工具调用」，超过阈值返回不收敛报告。"""
        if self._triggered is not None:
            return None
        if not self.enabled:
            return None
        self.steps += 1
        if self.steps >= self.max_steps:
            return self._trigger(ConvergenceReason.MAX_STEPS, self.steps)
        return None

    # ------------------------------------------------------------------
    # 规则 3：工具调用指纹检测
    # ------------------------------------------------------------------

    def record_tool_call(self, name: str, arguments: dict[str, Any] | None = None) -> Optional[ConvergenceReport]:
        """记录一次工具调用；相同工具 + 相同参数连续超阈值判定无效循环。"""
        if self._triggered is not None or not self.enabled:
            return None
        fingerprint = (name, _fingerprint(arguments))
        if fingerprint == self._last_fingerprint:
            self._same_streak += 1
        else:
            self._same_streak = 1
            self._last_fingerprint = fingerprint
        if self._same_streak >= self.max_same_tool_calls:
            return self._trigger(
                ConvergenceReason.TOOL_LOOP,
                f"连续 {self._same_streak} 次调用相同工具 {name} 与相同参数",
                streak=self._same_streak,
            )
        return None

    # ------------------------------------------------------------------
    # 规则 2：状态循环检测（A→B→A 震荡）
    # ------------------------------------------------------------------

    def record_state_transition(self, from_state: str, to_state: str) -> Optional[ConvergenceReport]:
        """记录一次状态跳转；连续出现 A→B→A 回跳判定状态震荡。"""
        if self._triggered is not None or not self.enabled:
            return None
        self.state_seq.append(to_state)
        seq = self.state_seq
        if len(seq) >= 3 and seq[-3] == seq[-1]:
            self._oscillation_streak += 1
        else:
            self._oscillation_streak = 0
        if self._oscillation_streak >= self.oscillation_threshold:
            return self._trigger(
                ConvergenceReason.STATE_OSCILLATION,
                f"状态震荡：{'→'.join(seq[-4:])} 连续 {self._oscillation_streak} 次 A→B→A 回跳",
                streak=self._oscillation_streak,
            )
        return None

    # ------------------------------------------------------------------
    # 汇总 / 提示
    # ------------------------------------------------------------------

    def detect(self) -> Optional[ConvergenceReport]:
        """返回当前是否已触发不收敛（未触发返回 None）。"""
        return self._triggered

    def convergence_prompt(self, report: Optional[ConvergenceReport] = None) -> str:
        """生成收敛提示 prompt：强制 Agent 跳出循环、向用户澄清而非继续调工具。"""
        if report is not None:
            return f"[收敛提示] {CONVERGENCE_PROMPT}（原因：{report.reason.value}，{report.detail}）"
        return CONVERGENCE_PROMPT

    def reset(self) -> None:
        """重置检测状态（新用例 / 新一轮前调用）。"""
        self.steps = 0
        self.tool_calls.clear()
        self.state_seq = ["INIT"]
        self._same_streak = 0
        self._last_fingerprint = None
        self._oscillation_streak = 0
        self._triggered = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _trigger(self, reason: ConvergenceReason, detail: str, *, streak: int = 0) -> ConvergenceReport:
        self._triggered = ConvergenceReport(
            reason=reason, detail=detail,
            steps=self.steps, tool_calls=len(self.tool_calls), streak=streak,
        )
        return self._triggered
