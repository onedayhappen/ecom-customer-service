"""知识库检索器：query → 向量化 → 委托后端检索。

设计上 retriever 只负责"问句怎么变向量""结果怎么聚合"，
存储和打分都交给 ChromaBackend：嵌入式向量数据库 + HNSW。

校验逻辑：加载后比对 backend 持久化的 embedding_model 与当前 Embedder.model，
不一致直接报错——避免"换了 embedding 但还在用老索引"这种隐蔽问题。

并发说明：本地 SentenceTransformer 编码是 CPU 密集操作，Chroma 客户端内部
也有锁；高并发下同时编码/查询反而互相争抢。因此用信号量把检索并发限制在
``settings.api_rag_search_concurrency``（默认 2），避免线程池被打满。
"""

from __future__ import annotations

import threading

from app.config.settings import settings
from app.rag.backends.base import RetrievedChunk, VectorBackend
from app.rag.embedder import Embedder

__all__ = ["KnowledgeRetriever", "RetrievedChunk"]

# 检索并发上限：保护 embedding 模型与 Chroma 内部锁，避免线程争抢。
_retrieval_semaphore = threading.BoundedSemaphore(settings.api_rag_search_concurrency)


class KnowledgeRetriever:
    """对上层暴露统一接口，对下委托给具体 backend。"""

    def __init__(self, embedder: Embedder, backend: VectorBackend):
        self._embedder = embedder
        self._backend = backend
        self._loaded = False

    @property
    def backend(self) -> VectorBackend:
        return self._backend

    @property
    def size(self) -> int:
        return self._backend.size()

    def load(self) -> None:
        if self._loaded:
            return
        self._backend.load()

        # 索引与查询向量必须来自同一 embedding 模型，否则相似度没有可比性。
        expected = self._backend.expected_embedding_model()
        if expected and expected != self._embedder.model:
            raise ValueError(
                f"索引模型({expected}) 与当前 Embedder 模型"
                f"({self._embedder.model}) 不一致，请重建索引。"
            )
        self._loaded = True

    def search(self, query: str, top_k: int = 3) -> list[RetrievedChunk]:
        if not self._loaded:
            self.load()
        # 并发上限内执行「编码 + 检索」整段，避免 embedding 模型被多线程同时调用。
        if not _retrieval_semaphore.acquire(timeout=settings.retrieval_local_wait_seconds):
            raise TimeoutError('Retrieval instance is busy')
        try:
            # Retriever 只负责“文本转向量”和调用时机，具体召回算法由后端实现。
            q_vec = self._embedder.encode_one(query)
            return self._backend.search(q_vec, top_k=top_k)
        finally:
            _retrieval_semaphore.release()
