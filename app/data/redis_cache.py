"""fail-safe 的 Redis JSON 缓存（订单查询 cache-aside 用）。

- Redis 不可用（未配置 URL / 连接失败）时降级为「永不命中、写入丢弃」，
  不影响主查询链路——缓存只是加速层，不是正确性依赖。
- 连接失败后短暂禁用（5 分钟），避免每次查询都重试握手打日志。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

_DISABLED_WINDOW_SECONDS = 300


class JsonCache:
    """极简 JSON 缓存客户端；Redis 失败自动降级为 no-op。"""

    def __init__(self, redis_url: str, *, prefix: str = "ecom:order", ttl: int = 300):
        self._redis_url = redis_url
        self._prefix = prefix
        self._ttl = ttl
        self._client: Any | None = None
        self._lock = threading.Lock()
        self._disabled_until: float = 0.0

    def _get_client(self) -> Any | None:
        if not self._redis_url:
            return None
        now = time.monotonic()
        if now < self._disabled_until:
            return None
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                try:
                    import redis

                    # protocol=2：兼容不支持 RESP3 的旧版 Redis（Windows 常见），
                    # 避免 HELLO 握手失败导致缓存整体降级。
                    self._client = redis.Redis.from_url(
                        self._redis_url,
                        protocol=2,
                        socket_connect_timeout=2,
                        socket_timeout=2,
                    )
                    self._client.ping()
                except Exception:
                    self._client = None
                    self._disabled_until = now + _DISABLED_WINDOW_SECONDS
        return self._client

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def get(self, key: str) -> Any | None:
        """读取并反序列化 JSON；未命中/Redis 不可用返回 None。"""
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._key(key))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            self._disable()
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """序列化写入，带 TTL；失败静默降级，不抛给调用方。"""
        client = self._get_client()
        if client is None:
            return
        try:
            client.set(self._key(key), json.dumps(value, ensure_ascii=False), ex=ttl or self._ttl)
        except Exception:
            self._disable()

    def delete(self, key: str) -> None:
        """主动失效（订单写操作成功后由调用方触发）。"""
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(self._key(key))
        except Exception:
            self._disable()

    def _disable(self) -> None:
        self._disabled_until = time.monotonic() + _DISABLED_WINDOW_SECONDS
        self._client = None
