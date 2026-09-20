"""ReAct 失败状态机：只用于执行控制，不保存业务状态。

核心原则：

> ReAct 可以决定"查什么"，但不能决定"失败后无限试、没有证据也回答、
> 状态未知仍继续写"。

本模块把该原则编码为可独立测试的纯状态机：

- :class:`FailureClassifier` 把工具结果权威归类为七类执行控制信号。
  优先读取工具显式声明的 ``error_code``（权威信号，不靠猜错误文本），
  仅对未改造的旧工具保留基于错误文本的保守推断。
- :class:`ReActPolicy` 跟踪连续失败计数、每工具瞬时重试预算、
  重复调用（同工具 + 同参数），输出继续 / 重试 / 缓存回放 / 终止决策。

终止策略为双阈值：

- 单一工具的瞬时失败：最多重试 1 次（写操作永不自动重试，转幂等状态查询）；
- 全链路连续失败：达到 3 次立即终止 ReAct 并降级。

本模块只依赖标准库，供执行器（react.py）与故障注入评测直接驱动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureCategory(str, Enum):
    """工具结果的执行控制分类（区别于业务状态）。"""

    SUCCESS = "success"
    #: 权威业务规则拒绝（订单已发货不可取消、重复退款、无权操作），重试无意义
    BUSINESS_REJECTION = "business_rejection"
    #: 参数/槽位不完整或格式错误
    VALIDATION_ERROR = "validation_error"
    #: 超时、网络、服务暂不可用——读工具可有限重试，写工具禁止自动重试
    TRANSIENT_FAILURE = "transient_failure"
    #: RAG 空召回、查询无数据——fail-closed，不凭常识回答
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    #: 多源数据冲突——不采信任一结果
    EVIDENCE_CONFLICT = "evidence_conflict"
    #: 确认令牌无效/过期/参数不匹配——需重新走确认流程，而非转人工
    CONFIRMATION_INVALID = "confirmation_invalid"


class Action(str, Enum):
    """执行器收到决策后应采取的动作。"""

    #: 正常继续循环，把结果交给模型
    CONTINUE = "continue"
    #: 瞬时失败且重试预算未耗尽：执行器用相同参数透明重试一次（模型不感知中间失败）
    RETRY_ONCE = "retry_once"
    #: 重复调用（同工具+同参数）：不真正执行，把缓存结果反馈给模型
    RETURN_CACHED = "return_cached"
    #: 写操作瞬时失败：执行器查询幂等记录状态；状态未知则转人工，绝不盲目重提
    CHECK_IDEMPOTENCY = "check_idempotency"
    #: 终止：向用户澄清/索取缺失字段
    STOP_CLARIFY = "stop_clarify"
    #: 终止：转人工
    STOP_HANDOFF = "stop_handoff"
    #: 终止：基于已有证据收尾（禁用工具，用已有结果组织答复）
    STOP_WITH_EVIDENCE = "stop_with_evidence"
    #: 终止：确认令牌失效，请求用户重新确认
    STOP_RECONFIRM = "stop_reconfirm"


# ---------------------------------------------------------------------------
# 终止动作对应的固定降级回复（不经过 LLM 改写，保证降级语义稳定）
# ---------------------------------------------------------------------------

HANDOFF_REPLY = (
    "抱歉，系统在处理您的请求时遇到异常。为避免误操作，"
    "当前未创建人工工单，请联系客服核实处理。"
)

CLARIFY_REPLY = (
    "抱歉，我暂时无法完成这项查询。请您补充完整的信息"
    "（例如准确的订单号），我再为您继续处理。"
)

RECONFIRM_REPLY = (
    "您的确认已过期或与当前操作不匹配。为保障账户安全，"
    "请重新确认是否继续执行该操作。"
)

EVIDENCE_FINALIZE_NOTE = (
    "（以上信息均基于已核实的查询结果；如需进一步帮助，请联系人工客服。）"
)


# 终止时按失败类别选择降级动作
_TERMINAL_ACTION = {
    FailureCategory.VALIDATION_ERROR: Action.STOP_CLARIFY,
    FailureCategory.EVIDENCE_UNAVAILABLE: Action.STOP_CLARIFY,
    FailureCategory.BUSINESS_REJECTION: Action.STOP_WITH_EVIDENCE,
    FailureCategory.EVIDENCE_CONFLICT: Action.STOP_HANDOFF,
    FailureCategory.TRANSIENT_FAILURE: Action.STOP_HANDOFF,
    # CONFIRMATION_INVALID 在达到阈值前就已被单独处理，此处仅兜底
    FailureCategory.CONFIRMATION_INVALID: Action.STOP_RECONFIRM,
}


@dataclass(frozen=True)
class PolicyDecision:
    """一次决策的完整输出。"""

    action: Action
    category: FailureCategory = FailureCategory.SUCCESS
    reason: str = ""
    #: RETURN_CACHED 时携带的上次结果
    cached_result: dict[str, Any] | None = None


class FailureClassifier:
    """把工具返回的 dict 归类为 :class:`FailureCategory`。

    分类优先级：

    1. 工具显式声明的 ``error_code``（权威信号，与 FailureCategory 值一致）；
    2. MutationGuard 的确认令牌拒绝（权威消息格式由本系统签发）；
    3. 保守的文本推断（仅服务未改造的旧工具）；
    4. 无法识别的失败默认按 ``evidence_unavailable`` 处理——fail-closed，
       不重试、不编造。
    """

    CODE_MAP: dict[str, FailureCategory] = {
        category.value: category
        for category in FailureCategory
        if category is not FailureCategory.SUCCESS
    }

    TRANSIENT_MARKERS = ("超时", "timeout", "网络", "network", "暂不可用", "服务繁忙", "稍后重试")
    VALIDATION_MARKERS = ("请提供", "不能为空", "缺少", "必填", "参数不", "参数错误")
    NOT_FOUND_MARKERS = ("未找到", "不存在", "暂无", "没有找到", "无数据")

    def classify(self, result: dict[str, Any]) -> FailureCategory:
        if not isinstance(result, dict) or result.get("success") is not False:
            return FailureCategory.SUCCESS

        code = result.get("error_code")
        if isinstance(code, str):
            mapped = self.CODE_MAP.get(code)
            if mapped is not None:
                return mapped

        error = str(result.get("error", ""))
        if "确认令牌" in error:
            return FailureCategory.CONFIRMATION_INVALID
        if any(marker in error for marker in self.TRANSIENT_MARKERS):
            return FailureCategory.TRANSIENT_FAILURE
        if any(marker in error for marker in self.VALIDATION_MARKERS):
            return FailureCategory.VALIDATION_ERROR
        # 未找到/无数据，以及一切无法识别的失败，都按无证据处理（fail-closed）
        return FailureCategory.EVIDENCE_UNAVAILABLE


class ReActPolicy:
    """ReAct 循环的失败控制状态机。

    用法（执行器视角）::

        policy = ReActPolicy(mutating_tools=frozenset(MUTATING_CAPABILITIES))

        # 每次工具调用前：拦截重复调用
        pre = policy.before_call(tool_name, arguments)

        # 每次工具调用后：分类结果并决策
        post = policy.after_call(tool_name, arguments, result)

    计数语义：

    - ``consecutive_failures`` 是全链路计数（跨工具）：任何一次失败 +1，
      任何一次成功清零。达到 ``max_consecutive_failures``（默认 3）立即终止。
    - 瞬时重试预算按工具计（每工具每轮最多 1 次），只授予非写工具；
      重试在执行器层透明完成，模型只看到重试后的最终结果。
    - 重复调用（同工具+同参数）同样计入连续失败——模型不应原地打转。
    """

    def __init__(
        self,
        *,
        max_consecutive_failures: int = 3,
        max_transient_retries: int = 1,
        mutating_tools: frozenset[str] = frozenset(),
        classifier: FailureClassifier | None = None,
    ):
        self.max_consecutive_failures = max_consecutive_failures
        self.max_transient_retries = max_transient_retries
        self.mutating_tools = mutating_tools
        self.classifier = classifier or FailureClassifier()
        self.consecutive_failures = 0
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._transient_retries: dict[str, int] = {}

    # -- 调用前：重复调用拦截 ---------------------------------------------

    def before_call(self, tool_name: str, arguments: dict[str, Any]) -> PolicyDecision:
        cached = self._cache.get(self._call_key(tool_name, arguments))
        if cached is None:
            return PolicyDecision(Action.CONTINUE)

        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_consecutive_failures:
            return PolicyDecision(
                Action.STOP_WITH_EVIDENCE,
                reason="重复调用达到终止阈值，基于已有证据收尾",
                cached_result=cached,
            )
        return PolicyDecision(
            Action.RETURN_CACHED,
            reason="重复调用同一工具与参数，返回缓存结果",
            cached_result=cached,
        )

    # -- 调用后：分类与决策 -------------------------------------------------

    def after_call(self, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> PolicyDecision:
        category = self.classifier.classify(result)
        self._cache[self._call_key(tool_name, arguments)] = result

        if category is FailureCategory.SUCCESS:
            self.consecutive_failures = 0
            return PolicyDecision(Action.CONTINUE, category=category)

        if category is FailureCategory.CONFIRMATION_INVALID:
            # 确认令牌问题不是环境故障：立即回到确认流程，不计连续失败
            return PolicyDecision(Action.STOP_RECONFIRM, category=category, reason="确认令牌无效或已过期")

        if category is FailureCategory.EVIDENCE_CONFLICT:
            # 多源数据冲突一次即停：不采信任一结果，也不给模型"再试一次"的机会
            return PolicyDecision(Action.STOP_HANDOFF, category=category, reason="工具返回冲突数据，停止执行并转人工")

        if category is FailureCategory.TRANSIENT_FAILURE:
            if tool_name in self.mutating_tools:
                # 写操作永不自动重试：先查幂等状态，状态未知则转人工
                return PolicyDecision(Action.CHECK_IDEMPOTENCY, category=category, reason="写操作瞬时失败，需查询幂等状态")
            if self._transient_retries.get(tool_name, 0) < self.max_transient_retries:
                self._transient_retries[tool_name] = self._transient_retries.get(tool_name, 0) + 1
                # 透明重试不增加连续失败计数；若重试仍失败会再次进入本方法并计数
                return PolicyDecision(Action.RETRY_ONCE, category=category, reason="瞬时失败，透明重试一次")

        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_consecutive_failures:
            action = _TERMINAL_ACTION.get(category, Action.STOP_HANDOFF)
            return PolicyDecision(action, category=category, reason=f"连续失败达到 {self.consecutive_failures} 次，终止 ReAct")
        # 未达阈值：把权威失败原因交给模型，由其调整路径（换工具/澄清/答复）
        return PolicyDecision(Action.CONTINUE, category=category, reason="失败已记录，等待模型调整")

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _call_key(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return tool_name, canonical
