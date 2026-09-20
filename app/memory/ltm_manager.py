"""长期记忆管理器：统一管理跨会话长期事实。

LongTermMemoryManager 是编排器和具体记忆实现之间的 facade：
- 编排器不直接关心长期事实如何存储、如何检索、如何过期。
- Agent 工具不直接访问底层 repository，只通过 manager 获取相关记忆。

当前支持两条读取路径：
1. 自动路径：少量明显需要历史上下文的 query，由编排器先检索并注入 system prompt。
2. 工具路径：Agent 在 ReAct 中根据推理需要调用 recall_user_memory。
"""

from __future__ import annotations

from typing import Optional

from openai import OpenAI

from app.memory.long_term import LongTermMemory
from app.memory.query_router import MemoryQueryRouter, MemoryRouteDecision


class LongTermMemoryManager:
    """长期记忆管理器：管理跨会话的用户事实。

    这层尽量保持薄：配置 LongTermMemory、暴露路由判断、执行会话结束时的巩固。
    复杂的写入治理在 extraction/validator/consolidator/store 中完成。
    """

    def __init__(
        self,
        client: OpenAI,
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
    ):
        self.client = client
        self.model = model
        self.memory_enabled = memory_enabled
        self.query_router = MemoryQueryRouter()

        # LongTermMemory 是真正的领域对象；manager 只负责把配置从上层传进去。
        self.ltm = LongTermMemory(
            user_id=user_id,
            memory_dir=memory_dir,
            max_facts=max_ltm_facts,
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

    def build_memory_prompt_sections(self, query: str) -> list[dict]:
        """按当前用户问题检索并生成记忆相关的 system prompt 消息。"""
        if not self.memory_enabled:
            return []

        # 长期事实仅在与当前问题相关时注入，避免画像噪声影响当前决策。
        if not self.should_auto_inject_long_term_memory(query):
            return []

        sections = []
        ltm_section = self.ltm.build_prompt_section(query)
        if ltm_section:
            sections.append({"role": "system", "content": ltm_section})
        return sections

    def retrieve_relevant_long_term_memory(self, query: str) -> list:
        """供工具调用按 Query 返回相关长期事实，而非全量历史。

        这是 recall_user_memory 的核心读取入口：Agent 自己判断何时需要查。
        """
        if not self.memory_enabled:
            return []
        return self.ltm.retrieve_relevant(query)

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
            self.ltm.save()
        return removed

    def reset_all(self) -> None:
        """重置所有长期记忆。"""
        self.ltm.reset()
