# 文件说明：提供长期记忆持久化门面，协调 JSON、PostgreSQL 与 Redis 缓存。
"""长期记忆持久化门面。

本模块同时保留两套存储语义：

1. 旧 snapshot 存储
   ``agent_long_term_memory`` / ``{user_id}.json``，一个用户一份 JSON 文档。
   它适合迁移兼容和简单开发，但粒度太粗。

2. 新 Memory Item 存储
   ``agent_memory_items`` / ``items/{user_id}.items.json``，每条记忆独立治理。
   在线读取只读 active items（含冷热层）；历史版本和证据保留在同一个权威账本。

Redis 是 cache-aside 加速层，只缓存旧 snapshot 路径；Memory Item 写入后会主动删缓存，
避免旧 snapshot 被误用。
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from app.db import ensure_schema_once, get_connection_pool
from app.memory.items import (
    JsonMemoryItemArchive,
    MemoryItem,
    MemoryItemRepository,
    MemoryItemArchive,
    build_memory_item_repository,
)


class ConcurrentMemoryWriteError(RuntimeError):
    """两个服务实例同时更新同一用户记忆时，旧版本写入被拒绝。"""


class MemoryRepository(Protocol):
    """旧 snapshot repository 接口。

    这里仍保留版本号乐观锁，主要用于迁移期兼容；新 Memory Item 路径不依赖它。
    """
    def load(self, user_id: str) -> tuple[dict | None, int]: ...
    def save(self, user_id: str, payload: dict, expected_version: int) -> int: ...
    def delete(self, user_id: str) -> None: ...


class JsonMemoryRepository:
    """离线开发与测试兼容实现，不用于多实例生产部署。

    JSON 文件没有跨进程锁；它只适合本地 demo 或单进程测试。
    """

    def __init__(self, memory_dir: str):
        self.memory_dir = Path(memory_dir)

    def _path(self, user_id: str) -> Path:
        path = self.memory_dir / f"{user_id}.json"
        if path.resolve().parent != self.memory_dir.resolve():
            raise ValueError('Invalid memory user path')
        return path

    def load(self, user_id: str) -> tuple[dict | None, int]:
        path = self._path(user_id)
        if not path.exists():
            return None, 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, 0
        return payload, int(payload.get("version", 1))

    def save(self, user_id: str, payload: dict, expected_version: int) -> int:
        """按 expected_version 乐观写入 snapshot。"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        _, current_version = self.load(user_id)
        if current_version != expected_version:
            raise ConcurrentMemoryWriteError("长期记忆已被其他实例更新，请重新加载后重试")
        version = current_version + 1
        payload["version"] = version
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
        return version

    def delete(self, user_id: str) -> None:
        self._path(user_id).unlink(missing_ok=True)


class PostgresMemoryRepository:
    """旧版长期记忆 snapshot 主存储。

    一个用户一行 JSONB 快照，使用版本号乐观锁防止并发覆盖。
    现在主要作为历史数据回退层，新增长期记忆优先写 Memory Item。
    连接复用进程级共享连接池；建表 DDL 只执行一次。
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        ensure_schema_once(dsn, "agent_long_term_memory", self._schema_ddl)

    @staticmethod
    def _schema_ddl(cur) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_long_term_memory (
                user_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    def _connect(self):
        pool = get_connection_pool(self.dsn)
        if pool is not None:
            return pool.connection()
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("未安装 psycopg，请执行 pip install 'psycopg[binary]'") from error
        return psycopg.connect(self.dsn)

    def load(self, user_id: str) -> tuple[dict | None, int]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT payload, version FROM agent_long_term_memory WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None, 0
        payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return payload, int(row[1])

    def save(self, user_id: str, payload: dict, expected_version: int) -> int:
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._connect() as conn, conn.cursor() as cur:
            if expected_version == 0:
                cur.execute(
                    """
                    INSERT INTO agent_long_term_memory (user_id, version, payload)
                    VALUES (%s, 1, %s::jsonb)
                    ON CONFLICT (user_id) DO NOTHING
                    RETURNING version
                    """,
                    (user_id, serialized),
                )
            else:
                cur.execute(
                    """
                    UPDATE agent_long_term_memory
                    SET payload = %s::jsonb, version = version + 1, updated_at = NOW()
                    WHERE user_id = %s AND version = %s
                    RETURNING version
                    """,
                    (serialized, user_id, expected_version),
                )
            row = cur.fetchone()
        if row is None:
            raise ConcurrentMemoryWriteError("长期记忆已被其他实例更新，请重新加载后重试")
        return int(row[0])

    def delete(self, user_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_long_term_memory WHERE user_id = %s", (user_id,))


class RedisMemoryCache:
    """Redis 缓存层；缓存不可用时自动回源主存储，不影响主流程。

    Redis 在这里不是权威数据源。任何异常都会被吞掉并回源，避免缓存故障拖垮客服链路。
    ``redis.Redis.from_url`` 内部自带连接池；为避免每个 LongTermMemory 实例
    都重建连接池，这里按 URL 做进程级复用。
    """

    _CLIENTS: dict[str, "object"] = {}

    def __init__(self, url: str, ttl_seconds: int, key_prefix: str = "ecom:ltm"):
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        try:
            import redis
        except ImportError as error:
            raise RuntimeError("未安装 redis，请执行 pip install redis") from error
        client = RedisMemoryCache._CLIENTS.get(url)
        if client is None:
            client = redis.Redis.from_url(url, decode_responses=True)
            RedisMemoryCache._CLIENTS[url] = client
        self.client = client

    def _key(self, user_id: str) -> str:
        """旧 snapshot 缓存 key；新 item key 空间后续可单独扩展。"""
        return f"{self.key_prefix}:{user_id}"

    def get(self, user_id: str) -> tuple[dict | None, int]:
        try:
            raw = self.client.get(self._key(user_id))
            if raw is None:
                return None, 0
            data = json.loads(raw)
            return data["payload"], int(data["version"])
        except Exception:
            return None, 0

    def set(self, user_id: str, payload: dict, version: int) -> None:
        """写缓存。

        当前实现是 best-effort；DB 成功后才会调用这里。若 Redis 写失败，主流程仍成功。
        """
        try:
            value = json.dumps({"version": version, "payload": payload}, ensure_ascii=False)
            self.client.set(self._key(user_id), value, ex=self.ttl_seconds)
        except Exception:
            pass

    def delete(self, user_id: str) -> None:
        try:
            self.client.delete(self._key(user_id))
        except Exception:
            pass


class LongTermMemoryStore:
    """长期记忆存储门面。

    Memory Item 是新的主写入模型；JSONB snapshot 仍保留为旧数据回退和兼容层。
    """

    def __init__(
        self,
        repository: MemoryRepository,
        cache: RedisMemoryCache | None = None,
        item_repository: MemoryItemRepository | None = None,
        item_archive: MemoryItemArchive | None = None,
    ):
        self.repository = repository
        self.cache = cache
        self.item_repository = item_repository
        self.item_archive = item_archive

    def load(self, user_id: str) -> tuple[dict | None, int]:
        """读取旧 snapshot，优先缓存，未命中再回源。"""
        if self.cache:
            payload, version = self.cache.get(user_id)
            if payload is not None:
                return payload, version
        payload, version = self.repository.load(user_id)
        if payload is not None and self.cache:
            self.cache.set(user_id, payload, version)
        return payload, version

    def save(self, user_id: str, payload: dict, expected_version: int) -> int:
        # 数据库提交成功才操作缓存，避免缓存出现未提交或失败写入的数据。
        payload["version"] = expected_version + 1
        version = self.repository.save(user_id, payload, expected_version)
        payload["version"] = version
        if self.cache:
            self.cache.delete(user_id)
            self.cache.set(user_id, payload, version)
        return version

    def list_items(self, user_id: str) -> list[MemoryItem]:
        """读取所有 active 版本，冷热 tier 不影响检索资格。"""
        if self.item_repository is None:
            return []
        return self.item_repository.list_active(user_id)

    def list_versions(self, user_id: str) -> list[MemoryItem]:
        """读取全部生命周期状态；读取不触发旧归档迁移或写入。"""
        return self.item_repository.list_versions(user_id) if self.item_repository else []

    def list_evidence(self, user_id: str, memory_id: str) -> list[dict]:
        return self.item_repository.list_evidence(user_id, memory_id) if self.item_repository else []

    def commit_versions(self, user_id: str, items: list[MemoryItem]) -> None:
        """原子保存提供的版本及证据，遗漏的版本保留。"""
        if self.item_repository is None:
            raise RuntimeError('Memory version commits require an item repository')
        with self.transaction(user_id):
            self._migrate_archive(user_id)
            if self.item_archive:
                incoming_ids = {item.memory_id for item in items}
                changed = self._items_to_archive(
                    [item for item in self.list_items(user_id) if item.memory_id in incoming_ids], items)
                self.item_archive.append(user_id, changed)
            self.item_repository.commit_versions(user_id, items)
        if self.cache:
            self.cache.delete(user_id)

    def _migrate_archive(self, user_id: str) -> None:
        # Only explicit writes import legacy JSONL, once under the user lock.
        # Imported rows are historical and cannot re-enter the current timeline.
        marker = '__migration__:memory_archive_v1'
        if self.batch_processed(user_id, marker):
            return
        if self.item_archive and hasattr(self.item_archive, 'read_versions'):
            self.item_repository.commit_versions(user_id, self.item_archive.read_versions(user_id))
        self.record_batch(user_id, marker)

    def items_initialized(self, user_id: str) -> bool:
        return self.item_repository is not None and self.item_repository.initialized(user_id)

    @contextmanager
    def transaction(self, user_id: str):
        if self.item_repository is None:
            raise RuntimeError('Atomic memory merging requires an item repository')
        with self.item_repository.transaction(user_id):
            yield

    def batch_processed(self, user_id: str, batch_id: str) -> bool:
        return self.item_repository.batch_processed(user_id, batch_id)

    def record_batch(self, user_id: str, batch_id: str) -> None:
        self.item_repository.record_batch(user_id, batch_id)

    def source_through(self, user_id: str, session_id: str) -> int:
        return self.item_repository.source_through(user_id, session_id)

    def record_source(self, user_id: str, session_id: str, through: int) -> None:
        self.item_repository.record_source(user_id, session_id, through)

    def replace_items(self, user_id: str, items: list[MemoryItem]) -> None:
        """替换 Memory Item active 热层，并归档被替换/移除的旧 item。"""
        if self.item_repository is None:
            return
        with self.transaction(user_id):
            self._migrate_archive(user_id)
            if self.item_archive:
                # Archive is append-only; crash retries may duplicate an audit
                # line, but authoritative items/receipts commit together.
                existing = self.item_repository.list_active(user_id)
                archive_items = self._items_to_archive(existing, items)
                if archive_items:
                    self.item_archive.append(user_id, archive_items)
            self.item_repository.replace_user_items(user_id, items)
        if self.cache:
            self.cache.delete(user_id)

    def delete(self, user_id: str) -> None:
        """删除/软删除该用户记忆并清理缓存。"""
        if self.item_repository:
            self.item_repository.delete_user_items(user_id)
        self.repository.delete(user_id)
        if self.cache:
            self.cache.delete(user_id)

    @staticmethod
    def _items_to_archive(existing: list[MemoryItem], incoming: list[MemoryItem]) -> list[MemoryItem]:
        """找出即将离开 active 热层或发生关键字段变化的旧 item。"""
        incoming_by_id = {item.memory_id: item for item in incoming}
        archive = []
        for old in existing:
            new = incoming_by_id.get(old.memory_id)
            if new is None:
                archive.append(old)
                continue
            if _item_changed(old, new):
                archive.append(old)
        return archive


def build_memory_store(
    backend: str,
    memory_dir: str,
    postgres_dsn: str = "",
    redis_url: str = "",
    cache_ttl_seconds: int = 1800,
    cache_key_prefix: str = "ecom:ltm",
) -> LongTermMemoryStore:
    """按配置创建长期记忆存储门面。"""
    if backend == "postgres":
        if not postgres_dsn:
            raise RuntimeError("MEMORY_POSTGRES_DSN 未配置，无法启用 PostgreSQL 长期记忆主存储")
        repository: MemoryRepository = PostgresMemoryRepository(postgres_dsn)
    elif backend == "json":
        repository = JsonMemoryRepository(memory_dir)
    else:
        raise ValueError("memory_storage_backend 仅支持 postgres 或 json")

    cache = (
        RedisMemoryCache(redis_url, cache_ttl_seconds, cache_key_prefix)
        if redis_url else None
    )
    item_repository = build_memory_item_repository(
        backend=backend,
        memory_dir=memory_dir,
        postgres_dsn=postgres_dsn,
    )
    item_archive = JsonMemoryItemArchive(memory_dir)
    return LongTermMemoryStore(repository, cache, item_repository, item_archive)


def _item_changed(left: MemoryItem, right: MemoryItem) -> bool:
    """判断两个 item 是否发生需要归档旧版本的关键变化。"""
    return (
        left.content != right.content
        or left.status != right.status
        or left.source_type != right.source_type
        or left.memory_key != right.memory_key
        or left.category != right.category
        or left.supersedes_memory_id != right.supersedes_memory_id
        or left.superseded_by_memory_id != right.superseded_by_memory_id
    )
