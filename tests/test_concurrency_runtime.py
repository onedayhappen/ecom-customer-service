"""并发基础设施的无外部依赖测试。"""

from __future__ import annotations

import time
import threading

import pytest

from app.runtime.chat_queue import ChatJob, ChatQueue, ChatWorker, SessionBusy
from app.runtime.concurrency import DistributedLease, SessionLock
from app.session.repository import SessionConflict, load_session, save_session


def test_local_distributed_lease_enforces_limit():
    lease = DistributedLease("", "test:leases", 1)
    first = lease.acquire(0.05)
    assert first
    assert lease.acquire(0.01) is None
    lease.release(first)
    assert lease.acquire(0.05)


def test_session_lock_serializes_local_workers():
    result = []
    with SessionLock("", "same-session", timeout=0.05):
        def contender():
            try:
                with SessionLock("", "same-session", timeout=0.01):
                    result.append(False)
            except TimeoutError:
                result.append(True)

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join()
    assert result == [True]


def test_chat_queue_worker_persists_result():
    redis_url = "redis://127.0.0.1:6399/15"
    queue_backend = ChatQueue(redis_url, prefix="test:ecom:chat", max_depth=4)
    try:
        queue_backend.ping()
    except Exception:
        pytest.skip("local Redis is not running")
    queue_backend.submit(ChatJob("job-1", "user-1", "session-1", "hello"))
    worker = ChatWorker(queue_backend, lambda job: {"reply": job.message})
    worker.start()
    try:
        queue_backend._redis.flushdb()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = queue_backend.get("job-1")
            if status and status.get("status") == "completed":
                assert status["result"] == {"reply": "hello"}
                break
            time.sleep(0.02)
        else:
            pytest.fail("worker did not complete job")
    finally:
        worker.stop()


def test_redis_queue_rejects_overlapping_session_and_allows_idempotent_retry():
    queue_backend = ChatQueue("redis://127.0.0.1:6399/15", prefix="test:ecom:busy", max_depth=4)
    try:
        queue_backend.ping()
    except Exception:
        pytest.skip("local Redis is not running")
    queue_backend._redis.flushdb()
    first = ChatJob("job-a", "u", "same", "a")
    assert queue_backend.submit(first) is True
    assert queue_backend.submit(first) is False
    with pytest.raises(SessionBusy):
        queue_backend.submit(ChatJob("job-b", "u", "same", "b"))


def test_json_session_optimistic_version(tmp_path, monkeypatch):
    import app.session.repository as repository

    monkeypatch.setattr(repository, "JSON_SESSION_DIR", tmp_path)
    assert save_session(
        "session-1", [{"role": "user", "content": "a"}], None,
        {"owner_user_id": "u"}, "json", "", expected_state_version=0,
    ) == 1
    assert load_session("session-1", "json")["state_version"] == 1
    with pytest.raises(SessionConflict):
        save_session(
            "session-1", [{"role": "user", "content": "stale"}], None,
            {"owner_user_id": "u"}, "json", "", expected_state_version=0,
        )
