"""长期记忆按 Query 检索回归测试，无需 LLM 或本地向量模型。"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.memory.long_term import LongTermMemory, MemoryFact


def _memory() -> LongTermMemory:
    memory = LongTermMemory(
        memory_dir=tempfile.mkdtemp(),
        retrieval_enabled=False,
        retrieval_top_k=2,
        retrieval_min_score=0.3,
    )
    now = datetime.now().isoformat(timespec="seconds")
    memory.facts = [
        MemoryFact("用户偏好黑色运动鞋", "preference", now),
        MemoryFact("上次耳机退款申请已提交", "issue", now),
        MemoryFact("用户是钻石会员", "identity", now),
    ]
    return memory


def test_recommendation_retrieves_preference_only():
    memory = _memory()

    facts = memory.retrieve_relevant("帮我推荐一双黑色运动鞋")
    contents = [fact.content for fact in facts]

    assert "用户偏好黑色运动鞋" in contents
    assert "上次耳机退款申请已提交" not in contents
    section = memory.build_prompt_section("帮我推荐一双黑色运动鞋") or ""
    assert "用户偏好黑色运动鞋" in section
    assert "上次耳机退款申请已提交" not in section


def test_refund_retrieves_issue_not_preference():
    memory = _memory()

    facts = memory.retrieve_relevant("我的退款申请处理到哪里了")
    contents = [fact.content for fact in facts]

    assert "上次耳机退款申请已提交" in contents
    assert "用户偏好黑色运动鞋" not in contents


if __name__ == "__main__":
    test_recommendation_retrieves_preference_only()
    test_refund_retrieves_issue_not_preference()
    print("长期记忆按 Query 检索测试通过")
