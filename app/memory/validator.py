"""长期记忆写入准入规则。

LLM 可以帮助“发现候选记忆”，但不应该直接决定“什么可以永久写入”。
这个模块就是 LLM extraction 与 MemoryStore 之间的闸门。

职责边界：
- Validator：判断候选事实是否可信、是否属于长期记忆、证据是否足够。
- Consolidator：处理已通过准入事实之间的去重、替换、容量控制。
- Store：只负责持久化，不重新解释事实语义。

这样拆开后，记忆污染问题可以集中在 Validator 里治理，而不会散落到存储层或 Agent
prompt 里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from app.memory.long_term import MemoryFact


@dataclass(frozen=True)
class MemoryValidationResult:
    """一条候选记忆的治理结果。

    ``reason`` 保留拒绝原因，便于后续做审计、debug 和离线调参。
    """

    accepted: bool
    reason: str = ""


class MemoryValidator:
    """判断 LLM 抽取出的候选事实是否允许写入长期记忆。

    Validator 只负责准入，不负责事实抽取。这样 LLM 可以提出候选记忆，
    但最终是否落盘由确定性规则控制。
    """

    # 只有这三种来源能进入准入流程。temporary/session_state 这类内容通过 scope 拦截。
    allowed_source_types = {"user_explicit", "tool_verified", "llm_inferred"}

    def __init__(
        self,
        min_confidence: float = 0.6,
        allow_llm_inferred: bool = False,
    ):
        self.min_confidence = min_confidence
        self.allow_llm_inferred = allow_llm_inferred

    def filter_facts(self, facts: Iterable["MemoryFact"], transcript: str = "") -> list["MemoryFact"]:
        """批量过滤候选事实，返回允许写入长期记忆的事实。

        transcript 是可选的；传入后可以验证 ``source_span`` 是否真的来自用户原话。
        """
        accepted = []
        for fact in facts:
            result = self.validate(fact, transcript)
            if result.accepted:
                accepted.append(fact)
        return accepted

    def validate(self, fact: "MemoryFact", transcript: str = "") -> MemoryValidationResult:
        """校验单条候选事实。

        这里偏保守：宁可少写一条记忆，也不要把模型猜测、临时状态或缺少证据的内容
        存进长期层。被拒绝的内容仍可能存在于会话摘要或事项状态里，不会直接丢失业务上下文。
        """
        from app.memory.source_policy import memory_safe
        if not memory_safe(fact.content) or not memory_safe(fact.source_span):
            return MemoryValidationResult(False, "sensitive_or_authorization_content")
        content = fact.content.strip()
        if not content:
            return MemoryValidationResult(False, "empty_content")

        if fact.source_type not in self.allowed_source_types:
            return MemoryValidationResult(False, "invalid_source_type")

        if fact.confidence < self.min_confidence:
            return MemoryValidationResult(False, "low_confidence")

        if fact.scope in {"temporary", "session_state", "case_progress"}:
            return MemoryValidationResult(False, "non_long_term_scope")

        # 默认不自动接纳 llm_inferred：它可以作为候选进入人工审核或低置信通道，
        # 但不直接进入 active 长期记忆，防止“模型脑补”污染画像。
        if fact.source_type == "llm_inferred" and not self.allow_llm_inferred:
            return MemoryValidationResult(False, "llm_inferred_requires_review")

        # 用户明确陈述必须能回溯到原文 span；否则模型可能把自己的推断包装成用户事实。
        if fact.source_type == "user_explicit":
            source_span = fact.source_span.strip()
            if not source_span:
                return MemoryValidationResult(False, "missing_source_span")
            if transcript and source_span not in transcript:
                return MemoryValidationResult(False, "source_span_not_in_transcript")

        return MemoryValidationResult(True)
