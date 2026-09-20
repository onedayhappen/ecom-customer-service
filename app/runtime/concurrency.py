"""Redis coordination is fail-closed; an empty URL explicitly selects local mode."""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path


class CoordinationUnavailable(RuntimeError):
    pass


def redis_client(url):
    if not url:
        return None
    import redis
    return redis.Redis.from_url(url, decode_responses=True, protocol=2,
                               socket_connect_timeout=2, socket_timeout=2)


class DistributedLease:
    """Atomic, renewable global admission slots, using Redis server time.

    This limits complete Agent runs, conservatively covering their sequential LLM
    calls. It is not an RPM/TPM limiter. All replicas must use the same key/limit.
    """
    _ACQUIRE = """
    local t = redis.call('TIME')
    local now = t[1] * 1000 + math.floor(t[2] / 1000)
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
    if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
    redis.call('ZADD', KEYS[1], now + ARGV[2], ARGV[3])
    redis.call('PEXPIRE', KEYS[1], ARGV[2] * 2)
    return 1
    """
    _RENEW = """
    local t = redis.call('TIME')
    local now = t[1] * 1000 + math.floor(t[2] / 1000)
    local expires = redis.call('ZSCORE', KEYS[1], ARGV[1])
    if not expires or tonumber(expires) <= now then return 0 end
    redis.call('ZADD', KEYS[1], now + ARGV[2], ARGV[1])
    redis.call('PEXPIRE', KEYS[1], ARGV[2] * 2)
    return 1
    """

    def __init__(self, redis_url, key, limit, *, lease_seconds=90.0):
        if limit < 1 or lease_seconds <= 0:
            raise ValueError('limit and lease_seconds must be positive')
        self.key, self.limit, self.lease_seconds = key, limit, lease_seconds
        self._redis = redis_client(redis_url)
        self._local = threading.BoundedSemaphore(limit)
        self._tokens = set()
        self._guard = threading.Lock()

    def acquire(self, timeout=0.0):
        token = uuid.uuid4().hex
        if self._redis is None:
            if not self._local.acquire(timeout=max(0, timeout)):
                return None
            with self._guard:
                self._tokens.add(token)
            return token
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self._redis.eval(self._ACQUIRE, 1, self.key, self.limit,
                                    int(self.lease_seconds * 1000), token):
                    return token
            except Exception as exc:
                raise CoordinationUnavailable('并发协调服务暂时不可用') from exc
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def release(self, token):
        if token is None:
            return
        if self._redis is None:
            with self._guard:
                if token in self._tokens:
                    self._tokens.remove(token)
                    self._local.release()
        else:
            try:
                self._redis.zrem(self.key, token)
            except Exception:
                pass  # Expiry is the crash recovery path. Never release someone else's slot.

    @contextmanager
    def slot(self, timeout=0.0):
        token = self.acquire(timeout)
        stop, lost = threading.Event(), threading.Event()
        def heartbeat():
            while not stop.wait(self.lease_seconds / 3):
                try:
                    if not self._redis.eval(self._RENEW, 1, self.key, token,
                                            int(self.lease_seconds * 1000)):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return
        thread = None
        if token and self._redis is not None:
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
        try:
            yield token is not None
            if lost.is_set():
                raise CoordinationUnavailable('运行期间并发租约丢失，结果需要核实')
        finally:
            stop.set()
            if thread:
                thread.join(timeout=3)
            self.release(token)


class SessionLock:
    """Renewable cross-instance session lock; file lock in explicit local mode.

    The queue also reserves a session from admission through completion, rejecting
    overlapping turns. This lock additionally coordinates sync/reset/close paths.
    """
    def __init__(self, redis_url, session_id, *, timeout=5.0, lease_seconds=90.0,
                 prefix='ecom:chat'):
        self._lease = DistributedLease(redis_url, f'{prefix}:session-lock:{session_id}',
                                       1, lease_seconds=lease_seconds)
        self.timeout = timeout
        self._local_path = Path(__file__).resolve().parents[2] / 'data/sessions/locks' / (
            sha256(session_id.encode()).hexdigest() + '.lock') if not redis_url else None

    def __enter__(self):
        if self._local_path is not None:
            from app.memory.file_lock import ProcessFileLock
            self._context = ProcessFileLock(self._local_path, timeout=self.timeout)
            self._context.__enter__()
        else:
            self._context = self._lease.slot(self.timeout)
            if not self._context.__enter__():
                self._context.__exit__(None, None, None)
                raise TimeoutError('会话正在处理中，请稍后重试')
        return self

    def __exit__(self, *exc):
        return self._context.__exit__(*exc)
