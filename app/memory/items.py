"""单条长期记忆存储。

Memory Item 是长期记忆的持久化原子单位。相比“一个用户一个 JSON 快照”，它能支持：
- 单条删除/过期/替换
- 按 user/case/session 作用域检索
- 按来源、置信度、权限做治理
- 把被替换的旧版本移入冷归档，而不是永远塞在 active 热层

本文件只定义底层 item repository 与 archive。具体哪些事实能写、如何合并替换，
由 validator/consolidator 决定。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.db import ensure_schema_once, get_connection_pool


@dataclass
class MemoryItem:
    """一条可独立治理的长期记忆。

    字段约定：
    - scope_type/scope_id：这条记忆属于用户、case 还是 session。
    - memory_type：fact 表示结构化事实。
    - memory_key：可替换槽位；同一槽位通常只保留一个 active 版本。
    - status：active 是热层，superseded/expired/deleted 会进入冷归档或软删除。
    """

    memory_id: str
    user_id: str
    scope_type: str  # user / case / session
    scope_id: str
    memory_type: str  # fact
    category: str
    content: str
    memory_key: str = ""
    source_type: str = "llm_inferred"
    source_session: str = ""
    source_event_id: str = ""
    source_span: str = ""
    confidence: float = 0.0
    status: str = "active"  # active / superseded / expired / deleted
    supersedes_memory_id: str = ""
    superseded_by_memory_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    allowed_agents: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class MemoryItemRepository(Protocol):
    """Memory Item 热存储接口。

    list_active 只返回 active 记忆；旧版本不参与在线 prompt 和检索。
    replace_user_items 是“替换该用户 active 热层”的批量写入语义。
    """
    def list_active(self, user_id: str) -> list[MemoryItem]: ...
    def replace_user_items(self, user_id: str, items: list[MemoryItem]) -> None: ...
    def delete_user_items(self, user_id: str) -> None: ...


class MemoryItemArchive(Protocol):
    """Memory Item 冷归档接口。

    冷归档用于审计、回放、离线分析；在线 Agent 默认不会读取这里的数据。
    """
    def append(self, user_id: str, items: list[MemoryItem]) -> None: ...


class JsonMemoryItemRepository:
    """离线开发与测试用的 Memory Item 文件存储。

    文件里只保存该用户当前 active items；这让本地行为更接近线上热层，
    同时保持测试环境不依赖 PostgreSQL。
    """

    def __init__(self, memory_dir: str):
        self.memory_dir = Path(memory_dir) / "items"

    def _path(self, user_id: str) -> Path:
        return self.memory_dir / f"{user_id}.items.json"

    def list_active(self, user_id: str) -> list[MemoryItem]:
        """读取指定用户的 active items；文件损坏时返回空，避免启动失败。"""
        path = self._path(user_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [
            MemoryItem(**item)
            for item in data.get("items", [])
            if item.get("user_id") == user_id and item.get("status", "active") == "active"
        ]

    def replace_user_items(self, user_id: str, items: list[MemoryItem]) -> None:
        """原子替换用户 active items。

        先写 tmp 文件再 os.replace，避免进程中断时留下半截 JSON。
        """
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        payload = {
            "user_id": user_id,
            "items": [asdict(item) for item in items if item.user_id == user_id],
        }
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    def delete_user_items(self, user_id: str) -> None:
        self._path(user_id).unlink(missing_ok=True)


class JsonMemoryItemArchive:
    """长期记忆冷归档（JSONL）。

    每一行是一条被替换/移除前的 MemoryItem 快照。JSONL 适合 append-only 写入，
    也方便后续用脚本做离线分析。
    """

    def __init__(self, memory_dir: str):
        self.archive_dir = Path(memory_dir) / "archive" / "memory_items"

    def _path(self, user_id: str) -> Path:
        return self.archive_dir / f"{user_id}.jsonl"

    def append(self, user_id: str, items: list[MemoryItem]) -> None:
        """追加归档项；空列表直接跳过。"""
        if not items:
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        now = datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as f:
            for item in items:
                payload = asdict(item)
                payload["archived_at"] = now
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")


class PostgresMemoryItemRepository:
    """PostgreSQL Memory Item 主存储。

    生产环境推荐走这里：每条记忆独立成行，方便后续添加索引、权限控制、向量字段和
    生命周期任务。旧 snapshot 表仍由 store.py 保留为迁移兼容。
    连接复用进程级共享连接池；建表 DDL 只执行一次。
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        ensure_schema_once(dsn, "agent_memory_items", self._schema_ddl)

    @staticmethod
    def _schema_ddl(cur) -> None:
        """创建/补齐 Memory Item 表结构。

        ADD COLUMN IF NOT EXISTS 用于平滑升级已有开发库，避免每次字段扩展都需要手写迁移。
        正式生产可以再接入 Alembic 等迁移工具。
        """
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory_items (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                memory_key TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'llm_inferred',
                source_session TEXT NOT NULL DEFAULT '',
                source_event_id TEXT NOT NULL DEFAULT '',
                source_span TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                supersedes_memory_id TEXT NOT NULL DEFAULT '',
                superseded_by_memory_id TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ,
                allowed_agents JSONB NOT NULL DEFAULT '[]'::jsonb,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("ALTER TABLE agent_memory_items ADD COLUMN IF NOT EXISTS memory_key TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE agent_memory_items ADD COLUMN IF NOT EXISTS supersedes_memory_id TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE agent_memory_items ADD COLUMN IF NOT EXISTS superseded_by_memory_id TEXT NOT NULL DEFAULT ''")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS agent_memory_items_user_scope_idx "
            "ON agent_memory_items (user_id, scope_type, scope_id, status)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS agent_memory_items_user_expiry_idx "
            "ON agent_memory_items (user_id, expires_at) WHERE status = 'active'"
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

    def list_active(self, user_id: str) -> list[MemoryItem]:
        """读取该用户当前 active 热层记忆。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_id, user_id, scope_type, scope_id, memory_type, category,
                       memory_key, content, source_type, source_session, source_event_id,
                       source_span, confidence, status, supersedes_memory_id,
                       superseded_by_memory_id, created_at::text, updated_at::text,
                       expires_at::text, allowed_agents, metadata
                FROM agent_memory_items
                WHERE user_id = %s AND status = 'active'
                ORDER BY created_at NULLS LAST, memory_id
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        items = []
        for row in rows:
            allowed_agents = row[19]
            metadata = row[20]
            items.append(MemoryItem(
                memory_id=row[0],
                user_id=row[1],
                scope_type=row[2],
                scope_id=row[3],
                memory_type=row[4],
                memory_key=row[5],
                category=row[6],
                content=row[7],
                source_type=row[8],
                source_session=row[9],
                source_event_id=row[10],
                source_span=row[11],
                confidence=float(row[12]),
                status=row[13],
                supersedes_memory_id=row[14],
                superseded_by_memory_id=row[15],
                created_at=row[16] or "",
                updated_at=row[17] or "",
                expires_at=row[18] or "",
                allowed_agents=json.loads(allowed_agents) if isinstance(allowed_agents, str) else allowed_agents,
                metadata=json.loads(metadata) if isinstance(metadata, str) else metadata,
            ))
        return items

    def replace_user_items(self, user_id: str, items: list[MemoryItem]) -> None:
        """替换用户 active 热层。

        这里删除的是 status='active' 的行；历史行如果已被标记成非 active，不会被物理删除。
        Store 会在调用本方法前把将要移除/变更的旧 active items 写入 archive。
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_memory_items WHERE user_id = %s AND status = 'active'",
                (user_id,),
            )
            cur.executemany(
                """
                INSERT INTO agent_memory_items (
                    memory_id, user_id, scope_type, scope_id, memory_type, category,
                    memory_key, content, source_type, source_session, source_event_id,
                    source_span, confidence, status, supersedes_memory_id,
                    superseded_by_memory_id, created_at, updated_at, expires_at,
                    allowed_agents, metadata
                )
                VALUES (
                    %(memory_id)s, %(user_id)s, %(scope_type)s, %(scope_id)s,
                    %(memory_type)s, %(category)s, %(memory_key)s, %(content)s,
                    %(source_type)s, %(source_session)s, %(source_event_id)s,
                    %(source_span)s, %(confidence)s, %(status)s,
                    %(supersedes_memory_id)s, %(superseded_by_memory_id)s,
                    %(created_at)s, %(updated_at)s, %(expires_at)s,
                    %(allowed_agents)s::jsonb, %(metadata)s::jsonb
                )
                ON CONFLICT (memory_id) DO UPDATE SET
                    scope_type = EXCLUDED.scope_type,
                    scope_id = EXCLUDED.scope_id,
                    memory_type = EXCLUDED.memory_type,
                    category = EXCLUDED.category,
                    memory_key = EXCLUDED.memory_key,
                    content = EXCLUDED.content,
                    source_type = EXCLUDED.source_type,
                    source_session = EXCLUDED.source_session,
                    source_event_id = EXCLUDED.source_event_id,
                    source_span = EXCLUDED.source_span,
                    confidence = EXCLUDED.confidence,
                    status = EXCLUDED.status,
                    supersedes_memory_id = EXCLUDED.supersedes_memory_id,
                    superseded_by_memory_id = EXCLUDED.superseded_by_memory_id,
                    updated_at = EXCLUDED.updated_at,
                    expires_at = EXCLUDED.expires_at,
                    allowed_agents = EXCLUDED.allowed_agents,
                    metadata = EXCLUDED.metadata
                """,
                [_serialize_item(item) for item in items if item.user_id == user_id],
            )

    def delete_user_items(self, user_id: str) -> None:
        """软删除用户所有 item，避免误操作时完全丢失审计线索。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_memory_items SET status = 'deleted' WHERE user_id = %s",
                (user_id,),
            )


def _serialize_item(item: MemoryItem) -> dict:
    """把 dataclass 转为 psycopg 可写入的参数字典。"""
    data = asdict(item)
    data["memory_id"] = data["memory_id"] or f"mem-{uuid4()}"
    for key in ("created_at", "updated_at", "expires_at"):
        data[key] = data[key] or None
    data["allowed_agents"] = json.dumps(data["allowed_agents"], ensure_ascii=False)
    data["metadata"] = json.dumps(data["metadata"], ensure_ascii=False)
    return data


def build_memory_item_repository(
    backend: str,
    memory_dir: str,
    postgres_dsn: str = "",
) -> MemoryItemRepository:
    """按配置构建 Memory Item repository。"""
    if backend == "postgres":
        if not postgres_dsn:
            raise RuntimeError("MEMORY_POSTGRES_DSN 未配置，无法启用 PostgreSQL Memory Item 存储")
        return PostgresMemoryItemRepository(postgres_dsn)
    if backend == "json":
        return JsonMemoryItemRepository(memory_dir)
    raise ValueError("memory item backend 仅支持 postgres 或 json")
