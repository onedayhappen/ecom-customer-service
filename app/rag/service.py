"""Internal knowledge retrieval service shared by HTTP retrieval and evidence checks."""
from pathlib import Path
from typing import Optional
import threading

from app.config.settings import settings
from app.rag.backends import create_backend
from app.rag.embedder import Embedder
from app.rag.retriever import KnowledgeRetriever
from app.reliability.failure_policy import FailureCategory

_retriever: Optional[KnowledgeRetriever] = None
_retriever_lock = threading.RLock()
_policy_retriever = None


def _create_backend_from_settings():
    """根据 settings 创建 Chroma 后端实例。"""
    name = settings.rag_backend.lower()
    if name == "chroma":
        return create_backend(
            "chroma",
            persist_dir=Path(settings.chroma_persist_dir),
            collection_name=settings.chroma_collection,
        )
    raise ValueError(
        f"未知的 RAG 后端: {settings.rag_backend}（仅支持 chroma）"
    )


def _get_retriever() -> KnowledgeRetriever:
    with _retriever_lock:
        return _initialize_retriever()


def _initialize_retriever() -> KnowledgeRetriever:
    """初始化和获取检索器"""
    global _retriever
    if _retriever is None:
        embedder = Embedder(
            api_key=settings.embedding_api_key or settings.openai_api_key,
            base_url=settings.embedding_base_url or settings.openai_base_url,
            model=settings.embedding_model,
            provider=settings.embedding_provider,
            local_model_path=settings.local_embedding_model_path,
        )
        backend = _create_backend_from_settings()
        _retriever = KnowledgeRetriever(embedder=embedder, backend=backend)
        _retriever.load()
    return _retriever


def reset_retriever() -> None:
    """清空单例缓存（测试或切换后端时使用）。"""
    global _retriever, _policy_retriever
    _retriever = None
    _policy_retriever = None


def search_knowledge(query: str, top_k: int = 3) -> dict:
    from app.rag.task_policy import current_policy_context
    from app.tasks.runtime import current_task
    if current_policy_context() is not None:
        return search_policy(query, top_k, current_policy_context())
    if current_task() is not None:
        from app.rag.policy import failure
        return failure('policy_context_missing')
    if settings.retrieval_service_url:
        from app.rag.remote import search
        try:
            if not query or len(query) > 4000 or not 1 <= int(top_k) <= 5:
                raise ValueError('Invalid retrieval input')
            return search(query, int(top_k))
        except Exception:
            return {'success': False, 'backend': 'remote', 'query': query, 'results': [],
                    'error': '远程检索暂时不可用', 'error_code': FailureCategory.TRANSIENT_FAILURE.value}
    return search_knowledge_local(query, top_k)


def _get_policy_retriever():
    global _policy_retriever
    with _retriever_lock:
        if _policy_retriever is None:
            from app.rag.releases import PolicyIndex
            from app.rag.retriever import PolicyRetriever
            embedder = Embedder(api_key=settings.embedding_api_key or settings.openai_api_key,
                base_url=settings.embedding_base_url or settings.openai_base_url,
                model=settings.embedding_model, provider=settings.embedding_provider,
                local_model_path=settings.local_embedding_model_path)
            _policy_retriever = PolicyRetriever(embedder, PolicyIndex(settings.chroma_persist_dir, settings.chroma_collection))
        return _policy_retriever


def search_policy(query, top_k, context, *, local=False):
    from app.rag.policy import PolicyError, PolicyContext, failure
    try:
        context = PolicyContext.model_validate(context)
        if settings.retrieval_service_url and not local:
            from app.rag.remote import policy_search
            return policy_search(query, top_k, context)
        return _get_policy_retriever().search(query, context, top_k)
    except PolicyError as error:
        return failure(error.code, error.fields)
    except Exception:
        return failure('policy_retrieval_unavailable')


def policy_result_current(result, expected_context=None, *, local=False):
    try:
        if settings.retrieval_service_url and not local:
            from app.rag.remote import policy_validate
            return policy_validate(result, expected_context)
        return _get_policy_retriever().current(result, expected_context)
    except Exception:
        return False


def search_knowledge_local(query: str, top_k: int = 3) -> dict:
    """检索退换货政策、配送说明、会员权益、FAQ 等知识库内容。

    Returns:
        {
          "success": bool,
          "backend": "chroma",
          "query": str,
          "results": [
            {"doc": "...", "section": "...", "score": 0.83, "text": "..."},
            ...
          ],
          "error": "..."  # 仅失败时存在
        }
    """
    if not query or not query.strip():
        return {
            "success": False,
            "error": "query 不能为空",
            "error_code": FailureCategory.VALIDATION_ERROR.value,
            "query": query,
            "results": [],
        }

    try:
        retriever = _get_retriever()
    except FileNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"知识库初始化失败: {e}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }

    try:
        top_k = max(1, min(int(top_k or 3), 5))
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": "top_k 必须是整数",
            "error_code": FailureCategory.VALIDATION_ERROR.value,
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }
    try:
        hits = retriever.search(query, top_k=top_k)
    except FileNotFoundError as e:
        reset_retriever()
        return {
            "success": False,
            "error": str(e),
            "error_code": FailureCategory.EVIDENCE_UNAVAILABLE.value,
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }
    except Exception as e:
        # Embedding、Chroma 查询或索引损坏都必须 fail-closed，不能把异常
        # 泄漏到 ReAct 循环，更不能让模型把检索失败当成“没有政策限制”。
        return {
            "success": False,
            "error": f"知识库检索失败: {type(e).__name__}",
            "error_code": FailureCategory.TRANSIENT_FAILURE.value,
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }

    return {
        "success": True,
        "backend": settings.rag_backend,
        "query": query,
        "results": [
            {
                "doc": h.chunk.doc,
                "section": h.chunk.section,
                "score": round(h.score, 4),
                "text": h.chunk.text,
            }
            for h in hits
        ],
    }
