"""长期事实检索。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from app.memory.long_term import MemoryFact

_MODELS: dict[str, object] = {}
T = TypeVar("T")


class MemoryRetriever:
    """按当前问题召回用户长期事实。

    当配置了本地 embedding 模型路径时，优先用向量相似度；否则回退到轻量词面匹配，
    保证开发环境和单元测试不依赖大模型/外部服务。
    """

    def __init__(self, model_path: str | None, enabled: bool = True, min_score: float = 0.35):
        self.enabled = enabled
        self.min_score = min_score
        self.model_path = Path(model_path).resolve() if model_path else None

    def retrieve(
        self,
        query: str,
        facts: list[MemoryFact],
        top_k: int,
    ) -> list[MemoryFact]:
        """按 query 召回相关事实。"""
        if not query.strip():
            return []
        return self._rank(
            query,
            facts,
            top_k,
            text_of=lambda fact: fact.content,
            boost=lambda fact: 0.12 if fact.category in self._preferred_categories(query) else 0.0,
        )

    def _rank(self, query: str, items: list[T], top_k: int, text_of, boost) -> list[T]:
        """通用排序逻辑。

        text_of 把不同记忆对象转成可匹配文本，boost 则注入业务规则加权。
        """
        if not items or top_k <= 0:
            return []
        scores = self._scores(query, [text_of(item) for item in items])
        ranked = []
        for item, score in zip(items, scores):
            adjusted_score = score + boost(item)
            if adjusted_score >= self.min_score:
                ranked.append((adjusted_score, item))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in ranked[:top_k]]


    def _scores(self, query: str, texts: list[str]) -> list[float]:
        """返回 query 与每段文本的相关性分数。

        sentence-transformers 失败时不抛出：记忆检索是增强能力，不能让缺少本地模型导致
        主业务流程不可用。
        """
        if self.enabled and self.model_path and self.model_path.exists():
            try:
                from sentence_transformers import SentenceTransformer

                key = str(self.model_path)
                model = _MODELS.get(key)
                if model is None:
                    model = SentenceTransformer(key)
                    _MODELS[key] = model
                vectors = model.encode([query, *texts], normalize_embeddings=True, show_progress_bar=False)
                query_vector = vectors[0]
                return [float(sum(a * b for a, b in zip(query_vector, vector))) for vector in vectors[1:]]
            except (ImportError, OSError, ValueError):
                pass
        return [self._lexical_score(query, text) for text in texts]

    @staticmethod
    def _lexical_score(query: str, text: str) -> float:
        """最小可用的中文词面相似度回退。

        这里按字符集合交集计算，粗糙但稳定；适合在没有 embedding 模型时做兜底。
        """
        query_chars = {char for char in query if char.strip()}
        text_chars = {char for char in text if char.strip()}
        return len(query_chars & text_chars) / len(query_chars) if query_chars else 0.0

    @staticmethod
    def _preferred_categories(query: str) -> set[str]:
        """根据 query 意图给结构化事实分类加权。"""
        if any(word in query for word in ("推荐", "喜欢", "买", "适合", "预算", "颜色", "尺码")):
            return {"preference", "behavior", "identity"}
        if any(word in query for word in ("退款", "退货", "订单", "物流", "售后", "投诉")):
            return {"issue"}
        if any(word in query for word in ("记得", "会员", "我是谁")):
            return {"identity", "preference", "behavior"}
        return set()
