"""长期记忆管理器：统一管理跨会话长期事实。

LongTermMemoryManager 是编排器和具体记忆实现之间的 facade：
- 编排器不直接关心长期事实如何存储、如何检索、如何过期。
- Agent 工具不直接访问底层 repository，只通过 manager 获取相关记忆。

当前支持两条读取路径：
1. 自动路径：少量明显需要历史上下文的 query，由编排器先检索并注入 system prompt。
2. 工具路径：Agent 在 ReAct 中根据推理需要调用 recall_user_memory。
"""

from __future__ import annotations

import json
from typing import Optional

from app.session.context_budget import estimate_message_tokens

from openai import OpenAI

from app.memory.long_term import LongTermMemory
from app.memory.query_router import MemoryQueryRouter, MemoryRouteDecision, temporal_query
from app.memory.compaction import build_views, resolve_view
from app.memory.retrieval import time_eligible


class LongTermMemoryManager:
    """长期记忆管理器：管理跨会话的用户事实。

    这层尽量保持薄：配置 LongTermMemory、暴露路由判断、执行会话结束时的巩固。
    复杂的写入治理在 extraction/validator/consolidator/store 中完成。
    """

    def __init__(
        self,
        client: OpenAI | None,
        model: str,
        user_id: str = "default",
        memory_dir: str = "data/sessions/memory",
        memory_enabled: bool = True,
        max_ltm_facts: int = 50,
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
        prompt_tokens: int = 1200,
    ):
        self.client = client
        self.model = model
        self.memory_enabled = memory_enabled
        self.source_policy = None
        self.prompt_tokens = prompt_tokens
        self.query_router = MemoryQueryRouter()
        self._injected_ids: set[str] = set()
        self._context_session_id = ""
        self._context_sources: dict[str, tuple[str, str, str]] = {}
        self._visible_auto_ids: set[str] = set()
        self._visible_user_quotes: list[str] = []
        self._visible_source_quotes: dict[int, list[str]] = {}
        self.derived_views: dict = {"profile": {"refs": []}, "topics": {}}
        from app.config.settings import settings

        # LongTermMemory 是真正的领域对象；manager 只负责把配置从上层传进去。
        self.ltm = LongTermMemory(
            user_id=user_id,
            memory_dir=memory_dir,
            max_facts=max_ltm_facts,
            hot_limit=settings.memory_hot_facts,
            preference_ttl_days=preference_ttl_days,
            retrieval_enabled=retrieval_enabled,
            retrieval_top_k=retrieval_top_k,
            retrieval_min_score=retrieval_min_score,
            retrieval_model_path=retrieval_model_path,
            storage_backend=storage_backend,
            postgres_dsn=postgres_dsn,
            redis_url=redis_url,
            cache_ttl_seconds=cache_ttl_seconds,
            cache_key_prefix=cache_key_prefix,
            semantic_retrieval_top_k=semantic_retrieval_top_k,
        )

        if self.memory_enabled:
            # 初始化时加载用户已有 active 记忆；如果关闭 memory_enabled，则完全不读写记忆层。
            self.ltm.load()

    def should_auto_inject_long_term_memory(self, query: str) -> bool:
        """判断当前 query 是否应自动注入长期记忆。

        只有“明显需要历史上下文”的 query 才返回 True；其它情况留给 Agent 工具路径。
        """
        if not self.memory_enabled:
            return False
        decision = self.query_router.decide(query)
        return decision.should_auto_inject

    def memory_route_decision(self, query: str) -> MemoryRouteDecision:
        """返回长期记忆路由决策，便于调试和测试。"""
        return self.query_router.decide(query)

    def build_memory_prompt_sections(
        self, query: str, *, context: list[dict] | None = None, session_id: str | None = None,
    ) -> list[dict]:
        """按当前用户问题检索并生成记忆相关的 system prompt 消息。"""
        self._context_session_id = session_id if isinstance(session_id, str) else ""
        self._context_sources.clear()
        self.set_injected_ids_from_messages(context or [])
        if not self.memory_enabled:
            return []
        # All candidates are already loaded for this request. Keep their exact
        # version/source association, without adding database reads at each LLM call.
        self._remember_context_sources(self.ltm.facts)

        # 长期事实仅在与当前问题相关时注入，避免画像噪声影响当前决策。
        if not self.should_auto_inject_long_term_memory(query):
            return []

        decision = self.query_router.decide(query)
        facts = self.retrieve_relevant_long_term_memory(
            query, history=decision.history, as_of=decision.as_of, exclude_ids=self.injected_ids,
        )
        self._remember_context_sources(facts)
        facts = [fact for fact in facts if fact.memory_id not in self._injected_ids]
        sections = [self._prompt_message(facts, history=decision.history, as_of=decision.as_of)] if facts else []
        self.set_injected_ids_from_messages([*sections, *(context or [])])
        return sections

    @property
    def injected_ids(self) -> set[str]:
        return set(self._injected_ids)

    def set_injected_ids_from_messages(self, messages: list[dict]) -> None:
        """Called again after final budgeting, so removed memory can be recalled."""
        self._visible_auto_ids = set()
        self._visible_user_quotes = []
        self._visible_source_quotes = {}
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if message.get("role") == "user":
                self._visible_user_quotes.append(content)
                continue
            name = message.get("name")
            if message.get("role") != "system" or name not in {
                "long_term_memory", "short_term_summary", "short_term_recall",
            }:
                continue
            try:
                payload = json.loads(content.split("\n", 1)[1])
            except (IndexError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            items = payload.get("facts" if name == "long_term_memory" else "items", [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if name == "long_term_memory":
                    memory_id = item.get("memory_id")
                    if isinstance(memory_id, str) and memory_id:
                        self._visible_auto_ids.add(memory_id)
                elif (item.get("role") == "user" and type(item.get("sequence")) is int
                      and item["sequence"] > 0 and isinstance(item.get("quote"), str)):
                    self._visible_source_quotes.setdefault(item["sequence"], []).append(item["quote"])
        self._refresh_injected_ids()

    def _remember_context_sources(self, facts) -> None:
        # Use this exact fact version's primary evidence. A matching value or an
        # older version's evidence does not establish that this version is visible.
        for fact in facts:
            if self._allowed(fact) and isinstance(fact.memory_id, str) and fact.memory_id:
                source = (fact.source_session, fact.source_event_id, fact.source_span)
                if all(isinstance(part, str) and part for part in source):
                    self._context_sources[fact.memory_id] = source
        self._refresh_injected_ids()

    def _refresh_injected_ids(self) -> None:
        ids = set(self._visible_auto_ids)
        if self._context_session_id:
            for memory_id, (session_id, event_id, span) in self._context_sources.items():
                if session_id != self._context_session_id:
                    continue
                if any(span in quote for quote in self._visible_user_quotes):
                    ids.add(memory_id)
                    continue
                event_session, separator, sequence = event_id.rpartition(":")
                if separator and event_session == session_id and sequence.isdecimal():
                    if any(span in quote for quote in self._visible_source_quotes.get(int(sequence), [])):
                        ids.add(memory_id)
        self._injected_ids = ids

    @staticmethod
    def fact_record(fact) -> dict:
        return {"memory_id": fact.memory_id, "category": fact.category,
                "version": getattr(fact, "version", 1), "status": fact.status,
                "content": fact.content, "source_event_id": fact.source_event_id,
                "predicate": getattr(fact, "predicate", ""), "value": getattr(fact, "value", None),
                "qualifiers": getattr(fact, "qualifiers", {}), "polarity": getattr(fact, "polarity", "positive"),
                "observed_at": getattr(fact, "observed_at", "") or fact.created_at,
                "valid_from": getattr(fact, "valid_from", ""), "valid_to": getattr(fact, "valid_to", ""),
                "expires_at": fact.expires_at}

    def _prompt_message(self, facts, *, history=False, as_of=None) -> dict:
        from app.prompts.context import HISTORICAL_MEMORY_NOTE, MEMORY_NOTE
        return {"role": "system", "name": "long_term_memory", "content":
                MEMORY_NOTE + (HISTORICAL_MEMORY_NOTE if history or as_of else "") + "\n" +
                json.dumps({"view": "historical" if history or as_of else "current", "as_of": as_of,
                            "facts": [self.fact_record(f) for f in facts]}, ensure_ascii=False, default=str)}

    def _allowed(self, fact) -> bool:
        from app.memory.source_policy import memory_safe
        return (not fact.metadata.get('reset')
                and memory_safe(fact.content) and memory_safe(fact.source_span)
                and (self.source_policy is None or self.source_policy.allows(fact))
                and fact.category in {"identity", "preference", "behavior", "other"}
                and fact.scope in {"user_preference", "user_profile"}
                and fact.source_type == "user_explicit"
                and bool(fact.source_session and fact.source_event_id and fact.source_span)
                and (not fact.allowed_agents or "customer_service" in fact.allowed_agents))

    def build_memory_views(self) -> dict:
        """Derived views never replace the full, searchable active collection."""
        from app.config.settings import settings
        eligible = [fact for fact in self.ltm.facts if self._allowed(fact) and time_eligible(fact)]
        self.derived_views = build_views(eligible, hot_limit=getattr(settings, "memory_hot_facts", 20),
                                        summary_tokens=getattr(settings, "memory_topic_tokens", 300))
        return self.derived_views

    def topic_summary(self, topic: str) -> dict | None:
        if self.source_policy is not None:
            self.ltm.load()
        view = self.derived_views.get("topics", {}).get(topic)
        eligible = [fact for fact in self.ltm.facts if self._allowed(fact)]
        return view if view and resolve_view(view, eligible) else None

    def retrieve_relevant_long_term_memory(self, query: str, *, history=False, as_of=None, exclude_ids=None) -> list:
        """Read bounded user-scope facts; never return task progress or ungrounded legacy facts."""
        if not self.memory_enabled:
            return []
        if self.source_policy is not None:
            # A manager may outlive a correction, reset or another session's
            # preference update. Do not retrieve from its old in-memory list.
            self.ltm.load()
        decision = self.query_router.decide(query)
        if decision.route in {"events", "realtime"}:
            return []
        if not history and as_of is None:
            history, as_of = temporal_query(query)
        historical = bool(history or as_of is not None)
        facts = self.ltm.history_facts() if historical else self.ltm.facts
        eligible = [fact for fact in facts if self._allowed(fact) and fact.memory_id not in (exclude_ids or set())]
        from app.config.settings import settings
        top_k = self.ltm.semantic_retrieval_top_k
        if top_k <= 0:
            return []
        ranked = self.ltm.retriever.retrieve(query, eligible, top_k=max(top_k, settings.memory_hybrid_candidates),
                                             history=historical, as_of=as_of)
        views = self.build_memory_views()
        if not ranked and not historical and any(cue in query for cue in ("我的偏好", "我的画像", "我喜欢什么")):
            ranked = [fact for fact in resolve_view(views["profile"], [f for f in self.ltm.facts if self._allowed(f)])
                      if fact.memory_id not in (exclude_ids or set())]
        # Historical candidates may not be in the active cache. Reuse this
        # retrieval's bounded candidate sources, without extra history reads.
        self._remember_context_sources(ranked)
        selected = []
        for fact in ranked:
            if exclude_ids is not None and fact.memory_id in self._injected_ids:
                continue
            if estimate_message_tokens([self._prompt_message([*selected, fact], history=historical, as_of=as_of)]) <= self.prompt_tokens:
                selected.append(fact)
            if len(selected) >= top_k:
                break
        return selected

    def consolidate_to_long_term(
        self, messages: list[dict], summary: Optional[str], source_session: str = "",
    ) -> None:
        """会话结束时，将本次对话的关键事实巩固到长期记忆。

        记忆不是每个 query 都写入；通常在会话收束/归档时，从完整上下文里提取，
        减少半句话或中间态被误存。
        """
        if not self.memory_enabled:
            return
        self.ltm.extract_and_save(
            self.client, self.model, messages, summary, source_session,
        )

    def prune_expired_long_term_memory(self) -> int:
        """供定时任务调用，清理过期的偏好类长期记忆并持久化。"""
        if not self.memory_enabled:
            return 0
        removed = self.ltm.prune_expired()
        if removed:
            # Reload and prune under the store lock; do not persist this
            # manager's possibly stale snapshot over another chat's new facts.
            self.ltm.merge_and_save([])
        return removed

    def reset_all(self) -> None:
        """重置所有长期记忆。"""
        self.ltm.reset()
