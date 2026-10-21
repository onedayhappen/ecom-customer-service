"""单条长期记忆存储。

Memory Item 是长期记忆的持久化原子单位。相比“一个用户一个 JSON 快照”，它能支持：
- 单条删除/过期/替换
- 按 user/case/session 作用域检索
- 按来源、置信度、权限做治理
- 保存每个版本及其证据，状态与冷热 tier 分开管理

本文件只定义底层 item repository 与 archive。具体哪些事实能写、如何合并替换，
由 validator/consolidator 决定。
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import ContextManager, Protocol
from uuid import uuid4

from app.db import ensure_schema_once, get_connection_pool


@dataclass
class MemoryItem:
    """一条可独立治理的长期记忆。

    字段约定：
    - scope_type/scope_id：这条记忆属于用户、case 还是 session。
    - memory_type：fact 表示结构化事实。
    - memory_key：可替换槽位；同一槽位通常只保留一个 active 版本。
    - status：active/superseded/retracted/expired 描述事实生命周期。
      热/冷层由 metadata.tier 表示；冷层 active 事实仍可检索。
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
    status: str = "active"  # active / superseded / retracted / expired / historical
    supersedes_memory_id: str = ""
    superseded_by_memory_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    allowed_agents: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class MemoryItemRepository(Protocol):
    """Memory Item 版本账本接口。

    list_active 只返回 active 记忆；旧版本不参与在线 prompt 和检索。
    commit_versions 只 upsert 提供的版本，省略的历史不会删除。
    replace_user_items 兼容旧 active 集合替换接口，移出的版本标记 superseded。
    """
    def list_active(self, user_id: str) -> list[MemoryItem]: ...
    def list_versions(self, user_id: str) -> list[MemoryItem]: ...
    def list_evidence(self, user_id: str, memory_id: str) -> list[dict]: ...
    def commit_versions(self, user_id: str, items: list[MemoryItem]) -> None: ...
    def replace_user_items(self, user_id: str, items: list[MemoryItem]) -> None: ...
    def delete_user_items(self, user_id: str) -> None: ...
    def initialized(self, user_id: str) -> bool: ...
    def transaction(self, user_id: str) -> ContextManager: ...
    def batch_processed(self, user_id: str, batch_id: str) -> bool: ...
    def record_batch(self, user_id: str, batch_id: str) -> None: ...
    def source_through(self, user_id: str, session_id: str) -> int: ...
    def record_source(self, user_id: str, session_id: str, through: int) -> None: ...


class MemoryItemArchive(Protocol):
    """Memory Item 冷归档接口。

    冷归档用于审计、回放、离线分析；在线 Agent 默认不会读取这里的数据。
    """
    def append(self, user_id: str, items: list[MemoryItem]) -> None: ...


class JsonMemoryItemRepository:
    """离线开发与测试用的 Memory Item 文件存储。

    versions 保存全部版本，items 是兼容旧工具的 active 镜像。
    版本、证据和处理回执在同一个原子文件事务里提交。
    """

    def __init__(self, memory_dir: str):
        self.memory_dir = Path(memory_dir) / "items"
        self._transaction = ContextVar(f'ltm_json_transaction_{id(self)}', default=None)

    def _path(self, user_id: str) -> Path:
        if not user_id or any(character in user_id for character in ('/', '\\', ':', '\x00')):
            raise ValueError('Invalid memory user path')
        path = self.memory_dir / f"{user_id}.items.json"
        # The file is atomically replaced by another process. Resolve only its
        # stable parent; on Windows resolving the changing file can fall back
        # to an incomplete path while another writer replaces it.
        if path.parent.resolve() != self.memory_dir.resolve() or path.is_reserved() or path.is_symlink():
            raise ValueError('Invalid memory user path')
        return path

    def _read(self, user_id):
        current = self._transaction.get()
        if current is not None:
            if current['user_id'] != user_id:
                raise ValueError('Memory transaction belongs to another user')
            return current['payload']
        path = self._path(user_id)
        if not path.exists():
            return {'user_id': user_id, 'items': [], 'processed_batches': []}
        # Corrupt authoritative data must not be treated as an empty account
        # and silently replaced by the next merge.
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload.get('user_id') != user_id:
            raise ValueError('Memory file owner mismatch')
        return payload

    @contextmanager
    def transaction(self, user_id):
        current = self._transaction.get()
        if current is not None:
            if current['user_id'] != user_id:
                raise ValueError('Memory transaction belongs to another user')
            yield
            return
        from app.memory.file_lock import ProcessFileLock
        path = self._path(user_id)
        with ProcessFileLock(path.with_suffix('.lock')):
            payload = self._read(user_id)
            state = {'user_id': user_id, 'payload': payload, 'dirty': False,
                     'initialized': path.exists()}
            token = self._transaction.set(state)
            try:
                yield
                if state['dirty']:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(path.name + f'.{uuid4().hex}.tmp')
                    try:
                        with temporary.open('w', encoding='utf-8') as stream:
                            json.dump(payload, stream, ensure_ascii=False, indent=2)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
            finally:
                self._transaction.reset(token)

    def initialized(self, user_id):
        current = self._transaction.get()
        return current['initialized'] if current and current['user_id'] == user_id else self._path(user_id).exists()

    def batch_processed(self, user_id, batch_id):
        return batch_id in self._read(user_id).get('processed_batches', [])

    def record_batch(self, user_id, batch_id):
        with self.transaction(user_id):
            payload = self._read(user_id)
            batches = payload.setdefault('processed_batches', [])
            if batch_id not in batches:
                batches.append(batch_id)
                self._transaction.get()['dirty'] = True

    def source_through(self, user_id, session_id):
        return self._read(user_id).get('source_cursors', {}).get(session_id, 0)

    def record_source(self, user_id, session_id, through):
        with self.transaction(user_id):
            cursors = self._read(user_id).setdefault('source_cursors', {})
            cursors[session_id] = max(through, cursors.get(session_id, 0))
            self._transaction.get()['dirty'] = True

    def list_active(self, user_id: str) -> list[MemoryItem]:
        """读取指定用户的 active items；损坏文件抛错，禁止空集覆盖。"""
        return [item for item in self.list_versions(user_id) if item.status == 'active']

    def list_versions(self, user_id: str) -> list[MemoryItem]:
        data = self._read(user_id)
        # Presence, not truthiness, distinguishes an authoritative empty ledger
        # from a legacy file that has never been migrated.
        return [MemoryItem(**item) for item in data.get('versions', data.get('items', []))
                if item.get('user_id') == user_id]

    def list_evidence(self, user_id: str, memory_id: str) -> list[dict]:
        item = next((item for item in self.list_versions(user_id) if item.memory_id == memory_id), None)
        if item is None:
            return []
        indexed = self._read(user_id).get('evidence', {}).get(memory_id, [])
        return _merge_evidence(indexed, _item_evidence(item))

    def commit_versions(self, user_id: str, items: list[MemoryItem]) -> None:
        _validate_items(user_id, items)
        with self.transaction(user_id):
            payload = self._read(user_id)
            versions = {item.memory_id: item for item in self.list_versions(user_id)}
            evidence_index = payload.setdefault('evidence', {})
            for item in items:
                evidence = _merge_evidence(evidence_index.get(item.memory_id, []),
                                           _item_evidence(versions[item.memory_id]) if item.memory_id in versions else [],
                                           _item_evidence(item))
                versions[item.memory_id] = _with_evidence(item, evidence)
                evidence_index[item.memory_id] = evidence
            payload['versions'] = [asdict(item) for item in versions.values()]
            payload['items'] = [asdict(item) for item in versions.values() if item.status == 'active']
            payload['schema_version'] = 2
            self._transaction.get().update(dirty=True, initialized=True)

    def replace_user_items(self, user_id: str, items: list[MemoryItem]) -> None:
        """替换 active 集合；省略的 active 版本保留为 superseded。"""
        _validate_items(user_id, items)
        with self.transaction(user_id):
            incoming = {item.memory_id for item in items}
            removed = [replace(item, status='superseded') for item in self.list_active(user_id)
                       if item.memory_id not in incoming]
            self.commit_versions(user_id, removed + items)

    def delete_user_items(self, user_id: str) -> None:
        # Preserve the empty active set, history, evidence and replay receipts.
        with self.transaction(user_id):
            self.commit_versions(user_id, [replace(item,
                status='retracted' if item.status == 'active' else item.status,
                metadata={**item.metadata, 'reset': True}) for item in self.list_versions(user_id)])


class JsonMemoryItemArchive:
    """长期记忆冷归档（JSONL）。

    每一行是一条被替换/移除前的 MemoryItem 快照。JSONL 适合 append-only 写入，
    也方便后续用脚本做离线分析。
    """

    def __init__(self, memory_dir: str):
        self.archive_dir = Path(memory_dir) / "archive" / "memory_items"

    def _path(self, user_id: str) -> Path:
        path = self.archive_dir / f"{user_id}.jsonl"
        if path.resolve().parent != self.archive_dir.resolve():
            raise ValueError('Invalid memory user path')
        return path

    def append(self, user_id: str, items: list[MemoryItem]) -> None:
        """追加归档项；空列表直接跳过。"""
        if not items:
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as f:
            for item in items:
                payload = asdict(item)
                payload["archived_at"] = now
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_versions(self, user_id: str) -> list[MemoryItem]:
        """显式迁移旧 JSONL；归档状态不明确的记录永不恢复为 active。"""
        path = self._path(user_id)
        if not path.exists():
            return []
        versions = {}
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get('user_id') != user_id:
                continue
            archived_at = payload.pop('archived_at', '')
            original_id = payload['memory_id']
            digest = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
            payload['memory_id'] = f'{original_id}:archive:{digest}'
            payload['metadata'] = {**payload.get('metadata', {}),
                                   'archived_legacy': True, 'legacy_memory_id': original_id,
                                   'archived_at': archived_at}
            payload['status'] = 'historical'
            versions[payload['memory_id']] = MemoryItem(**payload)
        return list(versions.values())


class PostgresMemoryItemRepository:
    """PostgreSQL Memory Item 主存储。

    生产环境推荐走这里：每条记忆独立成行，方便后续添加索引、权限控制、向量字段和
    生命周期任务。旧 snapshot 表仍由 store.py 保留为迁移兼容。
    连接复用进程级共享连接池；建表 DDL 只执行一次。
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._transaction = ContextVar(f'ltm_pg_transaction_{id(self)}', default=None)
        ensure_schema_once(dsn, "agent_memory_items_v3", self._schema_ddl)

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
        cur.execute('CREATE TABLE IF NOT EXISTS agent_memory_item_users (user_id TEXT PRIMARY KEY)')
        cur.execute('''CREATE TABLE IF NOT EXISTS agent_memory_batches (
            user_id TEXT NOT NULL, batch_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(user_id,batch_id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS agent_memory_source_cursors (
            user_id TEXT NOT NULL, session_id TEXT NOT NULL, through_sequence BIGINT NOT NULL,
            PRIMARY KEY(user_id,session_id))''')
        cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS agent_memory_items_owner_id_idx
            ON agent_memory_items(user_id,memory_id)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS memory_evidence (
            user_id TEXT NOT NULL, memory_id TEXT NOT NULL, source_event_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL, payload JSONB NOT NULL,
            PRIMARY KEY(user_id,memory_id,source_event_id,evidence_id),
            FOREIGN KEY(user_id,memory_id) REFERENCES agent_memory_items(user_id,memory_id)
        )''')

    def _raw_connect(self):
        pool = get_connection_pool(self.dsn)
        if pool is not None:
            return pool.connection()
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("未安装 psycopg，请执行 pip install 'psycopg[binary]'") from error
        return psycopg.connect(self.dsn)

    @contextmanager
    def _connect(self):
        current = self._transaction.get()
        if current:
            yield current[1]
        else:
            with self._raw_connect() as connection:
                yield connection

    @contextmanager
    def transaction(self, user_id):
        current = self._transaction.get()
        if current:
            if current[0] != user_id:
                raise ValueError('Memory transaction belongs to another user')
            yield
            return
        with self._raw_connect() as connection:
            lock = int.from_bytes(sha256(('ltm:' + user_id).encode()).digest()[:8], 'big', signed=True)
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
            token = self._transaction.set((user_id, connection))
            try:
                yield
            finally:
                self._transaction.reset(token)

    def initialized(self, user_id):
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute('''SELECT 1 FROM agent_memory_item_users WHERE user_id=%s
                UNION ALL SELECT 1 FROM agent_memory_items WHERE user_id=%s LIMIT 1''', (user_id, user_id))
            return cur.fetchone() is not None

    def batch_processed(self, user_id, batch_id):
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute('SELECT 1 FROM agent_memory_batches WHERE user_id=%s AND batch_id=%s', (user_id, batch_id))
            return cur.fetchone() is not None

    def record_batch(self, user_id, batch_id):
        with self.transaction(user_id), self._connect() as connection, connection.cursor() as cur:
            cur.execute('INSERT INTO agent_memory_batches(user_id,batch_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                        (user_id, batch_id))

    def source_through(self, user_id, session_id):
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute('SELECT through_sequence FROM agent_memory_source_cursors WHERE user_id=%s AND session_id=%s',
                        (user_id, session_id))
            row = cur.fetchone()
            return row[0] if row else 0

    def record_source(self, user_id, session_id, through):
        with self.transaction(user_id), self._connect() as connection, connection.cursor() as cur:
            cur.execute('''INSERT INTO agent_memory_source_cursors VALUES (%s,%s,%s)
                ON CONFLICT(user_id,session_id) DO UPDATE
                SET through_sequence=GREATEST(agent_memory_source_cursors.through_sequence,EXCLUDED.through_sequence)''',
                        (user_id, session_id, through))

    def list_active(self, user_id: str) -> list[MemoryItem]:
        """读取所有 active 事实，包含 metadata.tier=cold。"""
        return self._list_items(user_id, active_only=True)

    def list_versions(self, user_id: str) -> list[MemoryItem]:
        return self._list_items(user_id)

    def _list_items(self, user_id: str, *, active_only: bool = False,
                    memory_id: str | None = None) -> list[MemoryItem]:
        condition = " AND status = 'active'" if active_only else ''
        params = (user_id,)
        if memory_id is not None:
            condition += ' AND memory_id = %s'
            params += (memory_id,)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT memory_id, user_id, scope_type, scope_id, memory_type, category,
                       memory_key, content, source_type, source_session, source_event_id,
                       source_span, confidence, status, supersedes_memory_id,
                       superseded_by_memory_id, created_at::text, updated_at::text,
                       expires_at::text, allowed_agents, metadata
                FROM agent_memory_items
                WHERE user_id = %s {condition}
                ORDER BY created_at NULLS LAST, memory_id
                """,
                params,
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
                memory_key=row[6],
                category=row[5],
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

    def list_evidence(self, user_id: str, memory_id: str) -> list[dict]:
        items = self._list_items(user_id, memory_id=memory_id)
        if not items:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT payload FROM memory_evidence
                WHERE user_id=%s AND memory_id=%s ORDER BY source_event_id,evidence_id''',
                        (user_id, memory_id))
            evidence = [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in cur.fetchall()]
        return _merge_evidence(evidence, _item_evidence(items[0]))

    def commit_versions(self, user_id: str, items: list[MemoryItem]) -> None:
        """同用户事务内 upsert 版本和证据；不删除遗漏的历史。"""
        _validate_items(user_id, items)
        with self.transaction(user_id), self._connect() as conn, conn.cursor() as cur:
            prepared = []
            for item in items:
                cur.execute('''SELECT user_id,metadata,source_session,source_event_id,source_span,updated_at::text
                    FROM agent_memory_items WHERE memory_id=%s''', (item.memory_id,))
                existing = cur.fetchone()
                if existing and existing[0] != user_id:
                    raise ValueError('Memory ID belongs to another user')
                prior_evidence = []
                if existing:
                    metadata = json.loads(existing[1]) if isinstance(existing[1], str) else existing[1]
                    prior_evidence = metadata.get('evidence', [])
                    if existing[3] and not any(
                        entry.get('source_event_id') == existing[3]
                        and entry.get('source_span', '') == existing[4] for entry in prior_evidence
                    ):
                        prior_evidence = _merge_evidence(prior_evidence, [{
                            'source_session': existing[2], 'source_event_id': existing[3],
                            'source_span': existing[4], 'observed_at': existing[5] or '',
                        }])
                prepared.append(_with_evidence(item, _merge_evidence(prior_evidence, _item_evidence(item))))
            cur.execute('INSERT INTO agent_memory_item_users(user_id) VALUES (%s) ON CONFLICT DO NOTHING', (user_id,))
            if not prepared:
                return
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
                WHERE agent_memory_items.user_id = EXCLUDED.user_id
                """,
                [_serialize_item(item) for item in prepared],
            )
            if cur.rowcount != len(prepared):
                # A conflicting ID may appear after the ownership precheck.
                # Roll back the entire replacement instead of losing an item.
                raise ValueError('Memory ID ownership conflict')
            evidence_rows = [dict(user_id=user_id, memory_id=item.memory_id,
                                  source_event_id=evidence['source_event_id'],
                                  evidence_id=_evidence_id(evidence),
                                  payload=json.dumps(evidence, ensure_ascii=False))
                             for item in prepared for evidence in _item_evidence(item)]
            if evidence_rows:
                cur.executemany('''INSERT INTO memory_evidence(user_id,memory_id,source_event_id,evidence_id,payload)
                    VALUES (%(user_id)s,%(memory_id)s,%(source_event_id)s,%(evidence_id)s,%(payload)s::jsonb)
                    ON CONFLICT(user_id,memory_id,source_event_id,evidence_id) DO UPDATE
                    SET payload=EXCLUDED.payload''', evidence_rows)

    def replace_user_items(self, user_id: str, items: list[MemoryItem]) -> None:
        """兼容 active 集合替换；移出的版本只标记 superseded。"""
        _validate_items(user_id, items)
        with self.transaction(user_id):
            # Validate and write incoming IDs before touching the active set.
            self.commit_versions(user_id, items)
            incoming = {item.memory_id for item in items}
            removed = [replace(item, status='superseded') for item in self.list_active(user_id)
                       if item.memory_id not in incoming]
            self.commit_versions(user_id, removed)

    def delete_user_items(self, user_id: str) -> None:
        """软删除用户所有 item，避免误操作时完全丢失审计线索。"""
        with self.transaction(user_id), self._connect() as conn, conn.cursor() as cur:
            cur.execute('INSERT INTO agent_memory_item_users(user_id) VALUES (%s) ON CONFLICT DO NOTHING', (user_id,))
            cur.execute(
                """UPDATE agent_memory_items SET
                    status = CASE WHEN status='active' THEN 'retracted' ELSE status END,
                    metadata = metadata || '{"reset":true}'::jsonb
                    WHERE user_id = %s""",
                (user_id,),
            )


def _validate_items(user_id: str, items: list[MemoryItem]) -> None:
    if any(item.user_id != user_id for item in items):
        raise ValueError('Memory item owner mismatch')
    identifiers = [item.memory_id for item in items]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError('Memory versions require distinct nonempty IDs')


def _evidence_id(evidence: dict) -> str:
    """One fact may cite multiple spans of the same source event."""
    identity = {'source_span': evidence.get('source_span', ''), 'offset': _evidence_offset(evidence)}
    return sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _evidence_offset(evidence: dict):
    if evidence.get('offset') is not None:
        return evidence['offset']
    if evidence.get('start_offset') is not None or evidence.get('end_offset') is not None:
        return [evidence.get('start_offset'), evidence.get('end_offset')]
    return None


def _merge_evidence(*groups: list[dict]) -> list[dict]:
    merged = {}
    for group in groups:
        for evidence in group:
            if not isinstance(evidence, dict) or not evidence.get('source_event_id'):
                continue
            key = (str(evidence['source_event_id']), _evidence_id(evidence))
            # Retain all captured attributes when a duplicate adds details.
            merged[key] = {**merged.get(key, {}), **evidence}
    positioned = {(entry.get('source_event_id'), entry.get('source_span', ''))
                  for entry in merged.values() if _evidence_offset(entry) is not None}
    # The legacy top-level source has no offset. When that quote now has exact
    # offsets, it is the same evidence with better provenance, not a new quote.
    return [entry for entry in merged.values() if _evidence_offset(entry) is not None
            or (entry.get('source_event_id'), entry.get('source_span', '')) not in positioned]


def _item_evidence(item: MemoryItem) -> list[dict]:
    evidence = _merge_evidence(item.metadata.get('evidence', []))
    if item.source_event_id and not any(
        entry.get('source_event_id') == item.source_event_id
        and entry.get('source_span', '') == item.source_span for entry in evidence
    ):
        evidence = _merge_evidence(evidence, [{
            'source_event_id': item.source_event_id, 'source_session': item.source_session,
            'source_span': item.source_span,
            'observed_at': item.metadata.get('observed_at') or item.updated_at or item.created_at,
        }])
    return evidence


def _with_evidence(item: MemoryItem, evidence: list[dict]) -> MemoryItem:
    return replace(item, metadata={**item.metadata, 'evidence': evidence})


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
