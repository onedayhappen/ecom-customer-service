"""长期记忆 PostgreSQL/Redis 存储门面的无外部依赖测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.memory.store import ConcurrentMemoryWriteError, LongTermMemoryStore


class FakeRepository:
    def __init__(self):
        self.data: dict[str, tuple[dict, int]] = {}
        self.load_count = 0

    def load(self, user_id: str):
        self.load_count += 1
        return self.data.get(user_id, (None, 0))

    def save(self, user_id: str, payload: dict, expected_version: int):
        _, current = self.data.get(user_id, (None, 0))
        if current != expected_version:
            raise ConcurrentMemoryWriteError("stale write")
        version = current + 1
        self.data[user_id] = (payload.copy(), version)
        return version

    def delete(self, user_id: str):
        self.data.pop(user_id, None)


class FakeCache:
    def __init__(self):
        self.data: dict[str, tuple[dict, int]] = {}
        self.delete_count = 0

    def get(self, user_id: str):
        return self.data.get(user_id, (None, 0))

    def set(self, user_id: str, payload: dict, version: int):
        self.data[user_id] = (payload.copy(), version)

    def delete(self, user_id: str):
        self.delete_count += 1
        self.data.pop(user_id, None)


def test_cache_aside_and_version_consistency():
    repository = FakeRepository()
    cache = FakeCache()
    store = LongTermMemoryStore(repository, cache)

    payload = {"user_id": "u1", "facts": [{"content": "喜欢黑色"}]}
    assert store.save("u1", payload, expected_version=0) == 1
    assert repository.data["u1"][0]["version"] == 1
    assert cache.data["u1"][1] == 1

    # 第二次读取命中缓存，不应再访问主存储。
    loaded, version = store.load("u1")
    assert loaded["facts"][0]["content"] == "喜欢黑色"
    assert version == 1
    assert repository.load_count == 0

    updated = {"user_id": "u1", "facts": [{"content": "喜欢白色"}]}
    assert store.save("u1", updated, expected_version=1) == 2
    assert cache.delete_count == 2
    assert cache.data["u1"][0]["facts"][0]["content"] == "喜欢白色"
    assert cache.data["u1"][1] == 2

    try:
        store.save("u1", {"user_id": "u1", "facts": []}, expected_version=1)
    except ConcurrentMemoryWriteError:
        pass
    else:
        raise AssertionError("旧版本写入必须被乐观锁拒绝")


if __name__ == "__main__":
    test_cache_aside_and_version_consistency()
    print("长期记忆存储与缓存一致性测试通过")
