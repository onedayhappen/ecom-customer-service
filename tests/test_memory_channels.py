# 文件说明：测试长期事实的相关性召回与偏好过期行为。
"""长期事实记忆检索与过期的无网络测试。"""

from datetime import datetime, timedelta
from tempfile import TemporaryDirectory, mkdtemp

from app.memory.long_term import LongTermMemory, MemoryFact


def _memory() -> LongTermMemory:
    memory = LongTermMemory(
        memory_dir=mkdtemp(),
        retrieval_enabled=False,
        retrieval_min_score=0.3,
        semantic_retrieval_top_k=1,
    )
    now = datetime.now().isoformat(timespec="seconds")
    memory.facts = [
        MemoryFact("用户偏好黑色运动鞋", "preference", now),
        MemoryFact("ORD-001 退款申请已提交", "issue", now),
    ]
    return memory


def test_retrieves_relevant_facts_only():
    memory = _memory()

    facts = memory.retrieve_relevant("ORD-001 的退款到账了吗")

    assert [fact.content for fact in facts] == ["ORD-001 退款申请已提交"]
    prompt = memory.build_prompt_section("ORD-001 的退款到账了吗") or ""
    assert "用户历史记忆" in prompt


def test_preference_has_ttl():
    memory = _memory()
    now = datetime(2026, 7, 21, 12, 0, 0)
    memory.facts.append(MemoryFact(
        "用户偏好红色运动鞋", "preference", "2026-01-01T00:00:00",
    ))

    assert memory.prune_expired(now) == 1
    assert len(memory.facts) == 2
