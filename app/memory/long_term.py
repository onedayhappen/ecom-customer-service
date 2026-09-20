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
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Optional

from openai import OpenAI

from app.memory.consolidator import MemoryConsolidator
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


class LongTermMemory:
    """跨会话长期记忆：持久化用户知识。

    这层保留内存态 ``facts``，每次 save 再转成 MemoryItem。
    这么做兼顾了领域对象的易用性和底层 item 存储的治理能力。
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
    ):
        self.user_id = user_id
        self.memory_dir = Path(memory_dir)
        self.max_facts = max_facts
        self.preference_ttl_days = preference_ttl_days
        # 保留 retrieval_top_k 兼容旧配置。
        self.semantic_retrieval_top_k = semantic_retrieval_top_k or retrieval_top_k
        self.retriever = MemoryRetriever(
            model_path=retrieval_model_path,
            enabled=retrieval_enabled,
            min_score=retrieval_min_score,
        )
        self.consolidator = MemoryConsolidator()
        self.store: LongTermMemoryStore = build_memory_store(
            backend=storage_backend,
            memory_dir=memory_dir,
            postgres_dsn=postgres_dsn,
            redis_url=redis_url,
            cache_ttl_seconds=cache_ttl_seconds,
            cache_key_prefix=cache_key_prefix,
        )
        self.version = 0
        # facts 是当前用户的 active 热层；被替换/过期的旧版本由 Store 归档。
        self.facts: list[MemoryFact] = []

    @property
    def memory_path(self) -> Path:
        return self.memory_dir / f"{self.user_id}.json"

    def load(self) -> None:
        """优先读取 Memory Item；旧 JSONB snapshot 仅作为兼容回退。"""
        # 新模型：Memory Item 是主路径，能支持单条治理、过期、归档和未来权限控制。
        items = self.store.list_items(self.user_id)
        if items:
            self.version = 0
            self._load_from_items(items)
            # 旧 episode 不再参与在线记忆；下次加载时将其从活跃层移除并归档。
            had_episodes = any(item.memory_type == "episode" for item in items)
            if had_episodes or self.prune_expired():
                self.save()
            return

        # 旧模型：一个用户一个 JSON snapshot。保留读取能力，避免历史数据无法迁移。
        data, version = self.store.load(self.user_id)
        if data is None:
            return

        self.version = version
        self.facts = []
        for item in data.get("facts", []):
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
        # 旧 episodic_memories 不再加载；业务情境由 Case 管理。
        had_episodes = bool(data.get("episodic_memories") or data.get("interaction_summaries"))
        if had_episodes or self.prune_expired():
            self.save()

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
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "facts": [asdict(f) for f in self.facts],
        }

        self.version = self.store.save(self.user_id, payload, self.version)

    def _load_from_items(self, items: list[MemoryItem]) -> None:
        """把底层 MemoryItem 转回领域对象。

        Store 只返回 active items，因此这里不会加载 superseded/expired/deleted 的冷数据。
        """
        self.facts = []
        for item in items:
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
                    scope=item.metadata.get("scope", self._fact_scope_from_item(item)),
                    status=item.status,
                    supersedes_memory_id=item.supersedes_memory_id,
                    superseded_by_memory_id=item.superseded_by_memory_id,
                    allowed_agents=item.allowed_agents,
                ))

    def _to_items(self) -> list[MemoryItem]:
        """把内存态领域对象统一转成可持久化的 MemoryItem。"""
        return [self._fact_to_item(fact) for fact in self.facts]

    def _fact_to_item(self, fact: MemoryFact) -> MemoryItem:
        """结构化事实 → MemoryItem。

        scope_type/scope_id 决定这条事实属于用户、case 还是 session；当前主要使用 user
        和 case，为后续更细粒度权限/检索预留。
        """
        scope_type, scope_id = self._scope_for_fact(fact)
        memory_key = fact.memory_key or self.consolidator.derive_memory_key(fact)
        memory_id = fact.memory_id or f"mem-{sha256(f'{memory_key}|{fact.content}'.encode('utf-8')).hexdigest()[:16]}"
        return MemoryItem(
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
            metadata={"scope": fact.scope},
        )

    def _scope_for_fact(self, fact: MemoryFact) -> tuple[str, str]:
        """从 fact.scope 解析 MemoryItem 的作用域。

        scope 使用字符串是为了兼容 LLM/旧 snapshot 输出；落库时转为结构化字段。
        """
        if fact.scope.startswith("case:"):
            return "case", fact.scope.split(":", 1)[1]
        if fact.scope.startswith("session:"):
            return "session", fact.scope.split(":", 1)[1]
        return "user", self.user_id

    def _fact_scope_from_item(self, item: MemoryItem) -> str:
        """把 MemoryItem 的结构化 scope 还原成 MemoryFact.scope。"""
        if item.scope_type in {"case", "session"}:
            return f"{item.scope_type}:{item.scope_id}"
        return "user_preference"

    def add_facts(self, new_facts: list[MemoryFact]) -> None:
        """添加新事实，按 memory_key 去重、替换并裁剪到 max_facts。"""
        now = datetime.now()
        for fact in new_facts:
            self._apply_lifecycle(fact, now)
        self.facts = self.consolidator.consolidate(self.facts, new_facts, now=now)

    def prune_expired(self, now: Optional[datetime] = None) -> int:
        """移除已过期的偏好记忆，返回被清理的数量。"""
        now = now or datetime.now()
        active = []
        for fact in self.facts:
            if fact.category == "preference" and self._is_expired(fact, now):
                continue
            active.append(fact)
        removed = len(self.facts) - len(active)
        self.facts = active
        return removed

    def _apply_lifecycle(self, fact: MemoryFact, now: datetime) -> None:
        """给新 fact 写入更新时间和默认 TTL。

        目前只有 preference 默认过期；身份类/经过验证的事实默认不过期。
        """
        timestamp = now.isoformat(timespec="seconds")
        fact.updated_at = timestamp
        if fact.category == "preference":
            fact.expires_at = (now + timedelta(days=self.preference_ttl_days)).isoformat(
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
            return datetime.fromisoformat(expires_at) <= now
        except ValueError:
            return False

    def extract_and_save(
        self,
        client: OpenAI,
        model: str,
        messages: list[dict],
        summary: Optional[str],
        source_session: str = "",
    ) -> None:
        """从会话消息中提取长期记忆事实并保存。"""
        if not messages and not summary:
            return

        new_facts = extract_long_term_facts(
            client, model, messages, summary, self.facts,
        )

        if new_facts:
            self.add_facts(new_facts)
        self.save()

    def retrieve_relevant(self, query: str) -> list[MemoryFact]:
        """根据 Query 召回相关长期事实。"""
        if self.prune_expired():
            self.save()
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
