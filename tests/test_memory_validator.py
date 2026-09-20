"""长期记忆 Validator 准入规则测试。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.memory.long_term import MemoryFact
from app.memory.validator import MemoryValidator


def _fact(**changes) -> MemoryFact:
    data = {
        "content": "用户喜欢黑色电子产品",
        "category": "preference",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_type": "user_explicit",
        "source_span": "我就喜欢黑色电子产品",
        "confidence": 0.95,
        "scope": "user_preference",
    }
    data.update(changes)
    return MemoryFact(**data)


def test_user_explicit_fact_requires_source_span_in_transcript():
    validator = MemoryValidator()

    accepted = validator.validate(
        _fact(),
        transcript="用户：我就喜欢黑色电子产品",
    )
    missing_evidence = validator.validate(
        _fact(source_span="我喜欢苹果产品"),
        transcript="用户：我就喜欢黑色电子产品",
    )

    assert accepted.accepted
    assert not missing_evidence.accepted
    assert missing_evidence.reason == "source_span_not_in_transcript"


def test_llm_inferred_fact_is_not_written_by_default():
    validator = MemoryValidator()

    result = validator.validate(_fact(source_type="llm_inferred", source_span="", confidence=0.99))

    assert not result.accepted
    assert result.reason == "llm_inferred_requires_review"


def test_low_confidence_and_temporary_scope_are_rejected():
    validator = MemoryValidator()

    assert not validator.validate(_fact(confidence=0.4), transcript="用户：我就喜欢黑色电子产品").accepted
    assert not validator.validate(
        _fact(scope="case_progress"),
        transcript="用户：我就喜欢黑色电子产品",
    ).accepted


if __name__ == "__main__":
    test_user_explicit_fact_requires_source_span_in_transcript()
    test_llm_inferred_fact_is_not_written_by_default()
    test_low_confidence_and_temporary_scope_are_rejected()
    print("长期记忆 Validator 测试通过")
