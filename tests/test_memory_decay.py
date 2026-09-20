"""长期记忆偏好 TTL 的无网络单元测试。"""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.memory.long_term import LongTermMemory, MemoryFact


def test_expired_preference_is_pruned():
    with TemporaryDirectory() as memory_dir:
        memory = LongTermMemory(memory_dir=memory_dir, preference_ttl_days=90)
        memory.facts = [
            MemoryFact(
                content="用户偏好红色",
                category="preference",
                created_at="2026-01-01T00:00:00",
            ),
            MemoryFact(
                content="用户是钻石会员",
                category="identity",
                created_at="2026-01-01T00:00:00",
            ),
        ]

        assert memory.prune_expired(datetime(2026, 5, 1)) == 1
        assert [fact.content for fact in memory.facts] == ["用户是钻石会员"]


def test_reconfirmed_preference_refreshes_expiry():
    with TemporaryDirectory() as memory_dir:
        memory = LongTermMemory(memory_dir=memory_dir, preference_ttl_days=90)
        fact = MemoryFact(
            content="用户偏好红色",
            category="preference",
            created_at="2026-01-01T00:00:00",
        )
        memory.add_facts([fact])

        assert fact.updated_at
        assert datetime.fromisoformat(fact.expires_at) > datetime.now() + timedelta(days=89)


def test_expired_preference_is_excluded_from_prompt():
    with TemporaryDirectory() as memory_dir:
        memory = LongTermMemory(memory_dir=memory_dir, preference_ttl_days=90)
        memory.facts = [
            MemoryFact(
                content="用户偏好红色",
                category="preference",
                created_at="2026-01-01T00:00:00",
            )
        ]

        assert memory.build_prompt_section("我喜欢什么颜色") is None
