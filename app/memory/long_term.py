# 文件说明：组织跨会话用户事实的内存模型、更新、过期和检索操作。
"""长期记忆：跨会话持久化可复用的用户事实。

LongTermMemory 是记忆系统的领域对象，负责把几条链路串起来：

- load/save：从 Memory Item 主存储读取 active 记忆；旧 JSON snapshot 作为迁移兼容。
- extract_and_save：会话结束时调用 LLM 提取候选，再通过准入与合并写入。
- retrieve_relevant：按 query 召回相关 facts，而不是全量注入 prompt。
- prune_expired：根据 TTL 清理会过时的偏好记忆。

长期记忆只保留 MemoryFact：用户画像、稳定偏好、可复用事实。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

from app.memory.consolidator import MemoryConsolidator, MemoryQuota
from app.memory.extraction import extract_long_term_facts
from app.memory.items import MemoryItem
from app.memory.retrieval import MemoryRetriever
from app.memory.store import LongTermMemoryStore, build_memory_store


@dataclass
class MemoryFact:
    """一条长期记忆事实。

    memory_key 表示可替换槽位，例如 preference:electronics_color；
    memory_id 表示这条事实版本本身。这样“喜欢白色耳机”变成“喜欢黑色耳机”时，
    可以替换同槽位旧版本，而不是无限追加。
    """
    content: str
    category: str  # identity / preference / behavior / issue / other
    created_at: str
    memory_id: str = ""
    memory_key: str = ""
    source_session: str = ""
    updated_at: str = ""
    expires_at: str = ""
    # 记忆必须可追溯；默认视为模型推断，不能把它当作用户明确陈述。
    source_type: str = "llm_inferred"  # user_explicit / tool_verified / llm_inferred
    source_event_id: str = ""
    source_span: str = ""
    confidence: float = 0.0
    scope: str = "user_preference"
    status: str = "active"
    supersedes_memory_id: str = ""
    superseded_by_memory_id: str = ""
    allowed_agents: list[str] = field(default_factory=list)
    schema_version: int = 1
    predicate: str = ""
    value: Any = None
    qualifiers: dict = field(default_factory=dict)
    polarity: str = "positive"
    operation: str = "ASSERT"
    observed_at: str = ""
    valid_from: str = ""
    valid_to: str = ""
    recorded_at: str = ""
    superseded_at: str = ""
    version: int = 1
    evidence: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


ATOMIC_FIELDS = ('schema_version', 'predicate', 'value', 'qualifiers', 'polarity', 'operation',
                 'observed_at', 'valid_from', 'valid_to', 'recorded_at', 'superseded_at', 'version', 'evidence')


class LongTermMemory:
    """跨会话长期记忆：持久化用户知识。

    ``facts`` 保留当前有效事实（包括冷层），历史由版本账本保存。
    生产 v2 写入使用 merge_and_save；旧导入调用保留兼容入口。
    """

    def __init__(
        self,
        user_id: str = "default",
        memory_dir: str = "data/sessions/memory",
        max_facts: int = 50,
        preference_ttl_days: int = 90,
        retrieval_enabled: bool = True,
        retrieval_top_k: int = 4,
        retrieval_min_score: float = 0.35,
        retrieval_model_path: str | None = None,
        storage_backend: str = "json",
        postgres_dsn: str = "",
        redis_url: str = "",
        cache_ttl_seconds: int = 1800,
        cache_key_prefix: str = "ecom:ltm",
        semantic_retrieval_top_k: int | None = None,
        hot_limit: int = 20,
    ):
        self.user_id = user_id
        self.memory_dir = Path(memory_dir)
        self.max_facts = max_facts
        self.hot_limit = hot_limit
        self.last_operations = []
        self.preference_ttl_days = preference_ttl_days
        # 保留 retrieval_top_k 兼容旧配置。
        self.semantic_retrieval_top_k = semantic_retrieval_top_k or retrieval_top_k
        self.retriever = MemoryRetriever(
            model_path=retrieval_model_path,
            enabled=retrieval_enabled,
            min_score=retrieval_min_score,
        )
        self.consolidator = MemoryConsolidator(MemoryQuota(max_active_facts=max_facts))
        self.store: LongTermMemoryStore = build_memory_store(
            backend=storage_backend,
            memory_dir=memory_dir,
            postgres_dsn=postgres_dsn,
            redis_url=redis_url,
            cache_ttl_seconds=cache_ttl_seconds,
            cache_key_prefix=cache_key_prefix,
        )
        self.version = 0
        # facts 包含全部当前有效事实，冷热 tier 不决定事实是否有效。
        self.facts: list[MemoryFact] = []
        self.source_filter = None

    @property
    def memory_path(self) -> Path:
        return self.memory_dir / f"{self.user_id}.json"

    def load(self) -> None:
        """优先读取 Memory Item；旧 JSONB snapshot 仅作为兼容回退。"""
        # 新模型：Memory Item 是主路径，能支持单条治理、过期、归档和未来权限控制。
        self.facts = []
        items = self.store.list_versions(self.user_id)
        if items or self.store.items_initialized(self.user_id):
            self.version = 0
            self._load_from_items(items)
            if any(f.schema_version >= 2 for f in self.facts):
                from app.memory.reconciliation import reconcile
                self.facts, _ = reconcile(self.facts, [], user_id=self.user_id, hot_limit=self.hot_limit)
            self.facts = [f for f in self.facts if f.status == 'active' and not f.metadata.get('reset')]
            # Reads filter locally. The next atomic merge archives removed
            # records; online retrieval never replaces a possibly stale set.
            self.prune_expired()
            return

        # 旧模型：一个用户一个 JSON snapshot。保留读取能力，避免历史数据无法迁移。
        data, version = self.store.load(self.user_id)
        if data is None:
            return

        self.version = version
        self.facts = []
        for item in data.get("facts", []):
            if str(item.get("scope", "")).startswith("case:"):
                continue
            self.facts.append(MemoryFact(
                content=item["content"],
                category=item.get("category", "other"),
                created_at=item.get("created_at", ""),
                memory_id=item.get("memory_id", ""),
                memory_key=item.get("memory_key", ""),
                source_session=item.get("source_session", ""),
                updated_at=item.get("updated_at", item.get("created_at", "")),
                expires_at=item.get("expires_at", ""),
                source_type=item.get("source_type", "llm_inferred"),
                source_event_id=item.get("source_event_id", ""),
                source_span=item.get("source_span", ""),
                confidence=float(item.get("confidence", 0.0)),
                scope=item.get("scope", "user_preference"),
                status=item.get("status", "active"),
                supersedes_memory_id=item.get("supersedes_memory_id", ""),
                superseded_by_memory_id=item.get("superseded_by_memory_id", ""),
                allowed_agents=item.get("allowed_agents", []),
            ))
        # 旧 episodic_memories 不再加载；事项进度由 app.tasks 管理。
        self.prune_expired()

    def history_facts(self) -> list[MemoryFact]:
        """All user-owned versions, with current time windows recomputed in memory."""
        saved = self.facts
        try:
            self._load_from_items(self.store.list_versions(self.user_id))
            ledger = self.facts
        finally:
            self.facts = saved
        if any(f.schema_version >= 2 for f in ledger):
            from app.memory.reconciliation import reconcile
            ledger, _ = reconcile(ledger, [], user_id=self.user_id, hot_limit=self.hot_limit)
        return ledger

    def merge_and_save(self, facts: list[MemoryFact], source_batch_id: str = '') -> bool:
        """Prepare vectors outside the lock, then atomically reload/update the ledger."""
        prepared = deepcopy(facts)
        for fact in prepared:
            self._apply_lifecycle(fact, datetime.now(timezone.utc))
        # Encoding does not hold a user/store transaction. If there is no local
        # model, lexical retrieval remains available and no download is started.
        if hasattr(self.retriever, 'prepare_embeddings'):
            from app.memory.retrieval import content_hash
            cached = {content_hash(f): f.metadata['embedding'] for f in self.facts
                      if isinstance(f.metadata.get('embedding'), dict)}
            for fact in prepared:
                embedding = cached.get(content_hash(fact))
                if embedding is not None:
                    fact.metadata['embedding'] = deepcopy(embedding)
            prepared = self.retriever.prepare_embeddings(prepared)
        with self.store.transaction(self.user_id):
            if source_batch_id and self.store.batch_processed(self.user_id, source_batch_id):
                self.load()
                self.last_operations = [{'operation': 'NOOP', 'reason': 'duplicate_batch'}]
                return False
            ledger = self.history_facts()
            if not ledger and not self.store.items_initialized(self.user_id):
                self.load()
                ledger = deepcopy(self.facts)
            self._namespace_ids(ledger)
            candidates = []
            events = {fact.source_event_id for fact in prepared if fact.source_event_id}
            seen_events = {event for event in events
                           if self.store.batch_processed(self.user_id, 'event:' + event)}
            for fact in prepared:
                if self.source_filter is not None and not self.source_filter(fact):
                    continue
                event = fact.source_event_id
                if event in seen_events:
                    continue
                if event:
                    session, separator, sequence = event.rpartition(':')
                    if separator and sequence.isdigit() and int(sequence) <= self.store.source_through(self.user_id, session):
                        continue
                candidates.append(fact)
            atomic = [f for f in candidates if f.schema_version >= 2]
            legacy = [f for f in candidates if f.schema_version < 2]
            # Keep the legacy import/API contract, without using it to merge
            # structured facts or evict the atomic ledger's cold valid facts.
            old_legacy = [f for f in ledger if f.schema_version < 2 and f.status == 'active']
            if legacy or not any(f.schema_version >= 2 for f in ledger + candidates):
                self.facts = deepcopy(old_legacy)
                self.add_facts(legacy)
                self.prune_expired()
                current_ids = {f.memory_id for f in self.facts}
                for fact in ledger:
                    if fact.schema_version < 2 and fact.status == 'active' and fact.memory_id not in current_ids:
                        fact.status = 'expired' if self._is_expired(fact, datetime.now(timezone.utc)) else 'superseded'
                by_id = {f.memory_id: f for f in ledger}
                by_id.update({f.memory_id: f for f in self.facts})
                ledger = list(by_id.values())
            from app.memory.reconciliation import reconcile
            ledger, self.last_operations = reconcile(ledger, atomic, user_id=self.user_id, hot_limit=self.hot_limit)
            self.store.commit_versions(self.user_id, [self._fact_to_item(f) for f in ledger])
            if source_batch_id:
                self.store.record_batch(self.user_id, source_batch_id)
                parts = source_batch_id.rsplit(':', 2)
                if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                    self.store.record_source(self.user_id, parts[0], int(parts[2]))
            for event in events:
                self.store.record_batch(self.user_id, 'event:' + event)
            self.facts = [f for f in ledger if f.status == 'active' and not f.metadata.get('reset')]
            self.prune_expired()
        return True

    def save(self) -> None:
        """写入 Memory Item 主存储；旧 snapshot 由迁移期读取兼容。"""
        items = self._to_items()
        if self.store.item_repository is not None:
            # replace_items 会在覆盖 active 热层前归档被替换/移除的旧 item。
            self.store.replace_items(self.user_id, items)
            return

        # 兼容路径：只有在没有 item repository 时，才继续写旧 snapshot。
        payload = {
            "version": self.version,
            "user_id": self.user_id,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "facts": [asdict(f) for f in self.facts],
        }

        self.version = self.store.save(self.user_id, payload, self.version)

    def _load_from_items(self, items: list[MemoryItem]) -> None:
        """把底层 MemoryItem 转回领域对象。

        支持完整版本账本；当前/历史过滤由调用者执行。
        """
        self.facts = []
        for item in items:
            if item.user_id != self.user_id:
                continue
            if item.scope_type == 'user':
                if item.scope_id != self.user_id:
                    continue
                scope = item.metadata.get('scope', 'user_preference')
                if scope not in {'user_profile', 'user_preference'}:
                    continue
            elif item.scope_type == 'session' and item.scope_id:
                # Structured ownership/scope is authoritative. Legacy metadata
                # cannot promote a session record into a user profile; retired scopes are skipped.
                scope = self._fact_scope_from_item(item)
            else:
                continue
            if item.memory_type == "fact":
                self.facts.append(MemoryFact(
                    content=item.content,
                    category=item.category,
                    created_at=item.created_at,
                    memory_id=item.memory_id,
                    memory_key=item.memory_key,
                    source_session=item.source_session,
                    updated_at=item.updated_at,
                    expires_at=item.expires_at,
                    source_type=item.source_type,
                    source_event_id=item.source_event_id,
                    source_span=item.source_span,
                    confidence=item.confidence,
                    scope=scope,
                    status=item.status,
                    supersedes_memory_id=item.supersedes_memory_id,
                    superseded_by_memory_id=item.superseded_by_memory_id,
                    allowed_agents=item.allowed_agents,
                    schema_version=item.metadata.get('schema_version', 1),
                    predicate=item.metadata.get('predicate', ''), value=item.metadata.get('value'),
                    qualifiers=item.metadata.get('qualifiers', {}), polarity=item.metadata.get('polarity', 'positive'),
                    operation=item.metadata.get('operation', 'ASSERT'),
                    observed_at=item.metadata.get('observed_at', item.created_at),
                    valid_from=item.metadata.get('valid_from', ''), valid_to=item.metadata.get('valid_to', ''),
                    recorded_at=item.metadata.get('recorded_at', ''), superseded_at=item.metadata.get('superseded_at', ''),
                    version=item.metadata.get('version', 1), evidence=item.metadata.get('evidence', []),
                    metadata={key: deepcopy(value) for key, value in item.metadata.items()
                              if key not in {*ATOMIC_FIELDS, 'scope'}},
                ))

    def _to_items(self) -> list[MemoryItem]:
        """把内存态领域对象统一转成可持久化的 MemoryItem。"""
        self._namespace_ids(self.facts)
        return [self._fact_to_item(fact) for fact in self.facts]

    def _namespace_ids(self, facts):
        prefix = 'memu-' + sha256(self.user_id.encode('utf-8')).hexdigest()[:16] + '-'

        def scoped(value):
            return value if not value or value.startswith(prefix) else prefix + sha256(value.encode('utf-8')).hexdigest()[:24]

        for fact in facts:
            fact.memory_key = fact.memory_key or self.consolidator.derive_memory_key(fact)
            value = fact.memory_id or self.consolidator._stable_memory_id(fact)
            fact.memory_id = scoped(value)
            fact.supersedes_memory_id = scoped(fact.supersedes_memory_id)
            fact.superseded_by_memory_id = scoped(fact.superseded_by_memory_id)

    def _fact_to_item(self, fact: MemoryFact) -> MemoryItem:
        """结构化事实 → MemoryItem。

        scope_type/scope_id 区分用户与会话记录。事项进度只保存在 app.tasks，不写入用户画像。
        """
        self._namespace_ids([fact])
        scope_type, scope_id = self._scope_for_fact(fact)
        memory_key = fact.memory_key or self.consolidator.derive_memory_key(fact)
        memory_id = fact.memory_id or f"mem-{sha256(f'{memory_key}|{fact.content}'.encode('utf-8')).hexdigest()[:16]}"
        item = MemoryItem(
            memory_id=memory_id,
            user_id=self.user_id,
            scope_type=scope_type,
            scope_id=scope_id,
            memory_type="fact",
            memory_key=memory_key,
            category=fact.category,
            content=fact.content,
            source_type=fact.source_type,
            source_session=fact.source_session,
            source_event_id=fact.source_event_id,
            source_span=fact.source_span,
            confidence=fact.confidence,
            status=fact.status,
            supersedes_memory_id=fact.supersedes_memory_id,
            superseded_by_memory_id=fact.superseded_by_memory_id,
            created_at=fact.created_at,
            updated_at=fact.updated_at,
            expires_at=fact.expires_at,
            allowed_agents=fact.allowed_agents,
            metadata={**fact.metadata, "scope": fact.scope,
                      **{key: deepcopy(getattr(fact, key)) for key in ATOMIC_FIELDS}},
        )

        from app.memory.items import _item_evidence
        fact.evidence = _item_evidence(item)
        item.metadata['evidence'] = deepcopy(fact.evidence)
        return item

    def _scope_for_fact(self, fact: MemoryFact) -> tuple[str, str]:
        """从 fact.scope 解析 MemoryItem 的作用域。

        scope 使用字符串是为了兼容 LLM/旧 snapshot 输出；落库时转为结构化字段。
        """
        if fact.scope.startswith("case:"):
            raise ValueError("Retired Case scope cannot be written to user memory")
        if fact.scope.startswith("session:"):
            return "session", fact.scope.split(":", 1)[1]
        return "user", self.user_id

    def _fact_scope_from_item(self, item: MemoryItem) -> str:
        """把 MemoryItem 的结构化 scope 还原成 MemoryFact.scope。"""
        if item.scope_type == "session":
            return f"{item.scope_type}:{item.scope_id}"
        return "user_preference"

    def add_facts(self, new_facts: list[MemoryFact]) -> None:
        """旧 v1 导入的内存合并；v2 原子事实必须走 merge_and_save。"""
        now = datetime.now(timezone.utc)
        for fact in new_facts:
            self._apply_lifecycle(fact, now)
        self._namespace_ids(self.facts)
        self._namespace_ids(new_facts)
        self.facts = self.consolidator.consolidate(self.facts, new_facts, now=now)

    def prune_expired(self, now: Optional[datetime] = None) -> int:
        """移除已过期的偏好记忆，返回被清理的数量。"""
        now = now or datetime.now(timezone.utc)
        active = []
        for fact in self.facts:
            if (fact.category == "preference" or fact.expires_at) and self._is_expired(fact, now):
                continue
            active.append(fact)
        removed = len(self.facts) - len(active)
        self.facts = active
        return removed

    def _apply_lifecycle(self, fact: MemoryFact, now: datetime) -> None:
        """给新 fact 写入更新时间和默认 TTL。

        目前只有 preference 默认过期；身份类/经过验证的事实默认不过期。
        """
        if fact.source_event_id:
            try:
                now = datetime.fromisoformat(fact.updated_at or fact.created_at)
            except (TypeError, ValueError):
                raise ValueError('Event-backed memory requires a valid source timestamp')
        else:
            # Preserve the old offset-free format for manual/import callers,
            # but consistently interpret/generate those legacy values as UTC.
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
        timestamp = now.isoformat(timespec="microseconds" if fact.source_event_id else "seconds")
        fact.updated_at = timestamp
        if fact.category == "preference":
            ttl_start = now
            if fact.schema_version >= 2 and fact.valid_from:
                from app.memory.reconciliation import timestamp as parse_time
                valid_start = parse_time(fact.valid_from)
                source_time = parse_time(now.isoformat())
                if valid_start and valid_start > source_time:
                    ttl_start = valid_start
            fact.expires_at = (ttl_start + timedelta(days=self.preference_ttl_days)).isoformat(
                timespec="seconds"
            )

    def _is_expired(self, fact: MemoryFact, now: datetime) -> bool:
        """判断 fact 是否过期。

        对旧数据缺失 expires_at 的情况按 created/updated + TTL 兼容推导。
        """
        expires_at = fact.expires_at
        if not expires_at:
            base_time = fact.updated_at or fact.created_at
            try:
                expires_at = (
                    datetime.fromisoformat(base_time)
                    + timedelta(days=self.preference_ttl_days)
                ).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                # 时间格式异常的旧数据不自动删除，留给人工排查。
                return False
        try:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            return expiry <= now
        except (TypeError, ValueError):
            return False

    def extract_and_save(
        self,
        client: OpenAI,
        model: str,
        messages: list[dict],
        summary: Optional[str],
        source_session: str = "",
    ) -> None:
        """旧会话巩固兼容入口；生产事件的 v2 提取由 runtime 处理。"""
        if not messages and not summary:
            return

        new_facts = extract_long_term_facts(
            client, model, messages, summary, self.facts,
        )

        for fact in new_facts:
            fact.source_session = fact.source_session or source_session
        self.merge_and_save(new_facts)

    def retrieve_relevant(self, query: str) -> list[MemoryFact]:
        """根据 Query 召回相关长期事实。"""
        self.prune_expired()
        return self.retriever.retrieve(
            query,
            self.facts,
            top_k=self.semantic_retrieval_top_k,
        )

    def build_prompt_section(self, query: str) -> str | None:
        """只将与当前 Query 相关的长期记忆注入 system prompt。"""
        facts = self.retrieve_relevant(query)
        if not facts:
            return None

        facts_text = "\n".join(f"- [{f.category}] {f.content}" for f in facts)
        return f"与当前问题相关的用户历史记忆：\n{facts_text}"

    def reset(self) -> None:
        """清空该用户的长期记忆主存储与缓存。"""
        self.facts = []
        self.store.delete(self.user_id)
        self.version = 0
