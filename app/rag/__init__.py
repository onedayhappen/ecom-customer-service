# 文件说明：声明知识检索包，组织文档切分、向量化、索引发布与证据校验。
"""RAG 模块：知识库切分、向量化、检索。

第 5 期：让 Agent 能基于 FAQ、退换货政策、配送说明、会员权益等
非结构化知识回答问题，而不只依赖工具返回的结构化数据。
"""

from app.rag.backends import RetrievedChunk, VectorBackend, create_backend
from app.rag.chunker import Chunk, chunk_markdown_dir
from app.rag.embedder import Embedder
from app.rag.retriever import KnowledgeRetriever

__all__ = [
    "Chunk",
    "chunk_markdown_dir",
    "Embedder",
    "KnowledgeRetriever",
    "RetrievedChunk",
    "VectorBackend",
    "create_backend",
]
