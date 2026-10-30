# 文件说明：提供向量后端创建入口，组织抽象契约和 Chroma 实现。
"""向量后端：Chroma 向量数据库实现。"""

from app.rag.backends.base import RetrievedChunk, VectorBackend
__all__ = ["RetrievedChunk", "VectorBackend", "create_backend"]


def create_backend(name: str, **kwargs) -> VectorBackend:
    """工厂方法：根据名称创建对应后端。

    name: "chroma"
    kwargs: Chroma 持久化目录与 collection 名。
    """
    name = (name or "chroma").lower()
    if name == "chroma":
        from app.rag.backends.chroma_backend import ChromaBackend

        return ChromaBackend(
            persist_dir=kwargs["persist_dir"],
            collection_name=kwargs.get("collection_name", "ecom_kb"),
        )
    raise ValueError(f"未知的 RAG 后端: {name}（仅支持 chroma）")
