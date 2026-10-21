"""并发基础设施的无外部依赖测试。"""

from __future__ import annotations

import time
import threading
import os
from uuid import uuid4

import pytest

from app.runtime.concurrency import DistributedLease, SessionLock


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
