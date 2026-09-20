"""结构化记忆合并与防膨胀测试。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.memory.long_term import LongTermMemory, MemoryFact


def _fact(content: str, key: str = "preference:electronics_color", confidence: float = 0.9) -> MemoryFact:
    return MemoryFact(
        content=content,
        category="preference",
        created_at=datetime.now().isoformat(timespec="seconds"),
        memory_key=key,
        source_type="user_explicit",
        source_span=content,
        confidence=confidence,
    )


def test_same_memory_key_replaces_old_active_fact():
    memory = LongTermMemory(storage_backend="json", memory_dir="data/sessions/test-memory")
    memory.facts = [_fact("用户喜欢黑色电子产品", confidence=0.8)]

    memory.add_facts([_fact("用户现在喜欢白色电子产品", confidence=0.95)])

    assert [fact.content for fact in memory.facts] == ["用户现在喜欢白色电子产品"]
    assert memory.facts[0].supersedes_memory_id


def test_same_fact_refreshes_instead_of_duplicating():
    memory = LongTermMemory(storage_backend="json", memory_dir="data/sessions/test-memory")
    fact = _fact("用户喜欢黑色电子产品")
    memory.facts = [fact]

    memory.add_facts([_fact("用户喜欢黑色电子产品", confidence=0.95)])

    assert len(memory.facts) == 1
    assert memory.facts[0].confidence == 0.95


if __name__ == "__main__":
    test_same_memory_key_replaces_old_active_fact()
    test_same_fact_refreshes_instead_of_duplicating()
    print("结构化记忆合并测试通过")
