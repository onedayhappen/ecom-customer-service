"""Memory Item 持久化模型回归测试。"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.memory.long_term import LongTermMemory, MemoryFact


def test_long_term_memory_saves_and_loads_memory_items():
    with TemporaryDirectory() as memory_dir:
        now = datetime.now().isoformat(timespec="seconds")
        memory = LongTermMemory(user_id="u1", memory_dir=memory_dir, storage_backend="json")
        memory.facts = [
            MemoryFact(
                content="用户喜欢黑色电子产品",
                category="preference",
                created_at=now,
                source_type="user_explicit",
                source_span="我喜欢黑色电子产品",
                confidence=0.95,
            )
        ]
        memory.save()

        item_path = Path(memory_dir) / "items" / "u1.items.json"
        assert item_path.exists()
        saved = json.loads(item_path.read_text(encoding="utf-8"))
        assert [item["memory_type"] for item in saved["items"]] == ["fact"]

        loaded = LongTermMemory(user_id="u1", memory_dir=memory_dir, storage_backend="json")
        loaded.load()

        assert [fact.content for fact in loaded.facts] == ["用户喜欢黑色电子产品"]


def test_legacy_snapshot_is_fallback_when_items_are_absent():
    with TemporaryDirectory() as memory_dir:
        now = datetime.now().isoformat(timespec="seconds")
        memory = LongTermMemory(user_id="u1", memory_dir=memory_dir, storage_backend="json")
        memory.store.repository.save(
            "u1",
            {
                "facts": [{
                    "content": "用户是钻石会员",
                    "category": "identity",
                    "created_at": now,
                }],
            },
            expected_version=0,
        )

        memory.load()

        assert [fact.content for fact in memory.facts] == ["用户是钻石会员"]


def test_old_memory_is_archived_out_of_hot_store():
    with TemporaryDirectory() as memory_dir:
        now = datetime.now().isoformat(timespec="seconds")
        memory = LongTermMemory(user_id="u1", memory_dir=memory_dir, storage_backend="json")
        memory.facts = [
            MemoryFact(
                content="用户喜欢黑色电子产品",
                category="preference",
                created_at=now,
                memory_key="preference:electronics_color",
                source_type="user_explicit",
                source_span="我喜欢黑色电子产品",
                confidence=0.9,
            )
        ]
        memory.save()
        memory.facts = [
            MemoryFact(
                content="用户现在喜欢白色电子产品",
                category="preference",
                created_at=now,
                memory_key="preference:electronics_color",
                source_type="user_explicit",
                source_span="我现在喜欢白色电子产品",
                confidence=0.95,
            )
        ]
        memory.save()

        item_path = Path(memory_dir) / "items" / "u1.items.json"
        archive_path = Path(memory_dir) / "archive" / "memory_items" / "u1.jsonl"
        saved = json.loads(item_path.read_text(encoding="utf-8"))
        assert len(saved["items"]) == 1
        assert saved["items"][0]["content"] == "用户现在喜欢白色电子产品"
        assert archive_path.exists()
        archived_lines = archive_path.read_text(encoding="utf-8").strip().splitlines()
        assert archived_lines
        assert "用户喜欢黑色电子产品" in archived_lines[-1]


if __name__ == "__main__":
    test_long_term_memory_saves_and_loads_memory_items()
    test_legacy_snapshot_is_fallback_when_items_are_absent()
    test_old_memory_is_archived_out_of_hot_store()
    print("Memory Item 持久化测试通过")
