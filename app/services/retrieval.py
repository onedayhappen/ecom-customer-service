"""Independent retrieval HTTP service: bounded work, authenticated access."""
import asyncio
import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from app.config.settings import settings
from app.rag.policy import PolicyContext


@asynccontextmanager
async def lifespan(app):
    if len(settings.retrieval_service_token) < 32:
        raise RuntimeError('RETRIEVAL_SERVICE_TOKEN must contain at least 32 characters')
    if settings.retrieval_service_url:
        raise RuntimeError('Retrieval service must use local backend, not recursively call itself')
    from app.tools.knowledge import _get_retriever, _get_policy_retriever
    if settings.task_intake_enabled:
        retriever = await asyncio.to_thread(_get_policy_retriever)
        head = await asyncio.to_thread(retriever.index.head)
        if head['model'] != retriever.embedder.index_identity:
            raise RuntimeError('Published policy index uses a different embedding model')
    else:
        await asyncio.to_thread(_get_retriever)
    yield


app = FastAPI(title='Internal retrieval service', lifespan=lifespan)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=3, ge=1, le=5)


class PolicySearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=3, ge=1, le=5, strict=True)
    context: PolicyContext


class PolicyValidationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    result: dict
    expected_context: PolicyContext | None = None


def authorize(authorization):
    if not settings.retrieval_service_token or not hmac.compare_digest(authorization or '', 'Bearer ' + settings.retrieval_service_token):
        raise HTTPException(401, 'Service authentication required')


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/metrics')
def metrics(authorization: str | None = Header(default=None)):
    authorize(authorization)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post('/v1/search')
def search(request: SearchRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    from app.runtime.budgets import lease
    from app.tools.knowledge import search_knowledge_local
    resource = lease(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix + ':retrieval-global',
                     settings.retrieval_global_concurrency, settings.api_distributed_lease_seconds)
    try:
        with resource.slot(0) as acquired:
            if not acquired:
                raise HTTPException(429, 'Retrieval capacity exhausted', headers={'Retry-After': '1'})
            return search_knowledge_local(request.query, request.top_k)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Retrieval temporarily unavailable') from None


@app.post('/v1/policies/search')
def search_policies(request: PolicySearchRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    from app.runtime.budgets import lease
    from app.tools.knowledge import search_policy
    resource = lease(settings.api_concurrency_redis_url, settings.api_chat_queue_prefix + ':retrieval-global',
                     settings.retrieval_global_concurrency, settings.api_distributed_lease_seconds)
    try:
        with resource.slot(0) as acquired:
            if not acquired:
                raise HTTPException(429, 'Retrieval capacity exhausted', headers={'Retry-After': '1'})
            return search_policy(request.query, request.top_k, request.context, local=True)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Retrieval temporarily unavailable') from None


@app.post('/v1/policies/validate')
def validate_policy(request: PolicyValidationRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    import json
    if len(json.dumps(request.result, ensure_ascii=False).encode()) > 512000:
        raise HTTPException(413, 'Policy result exceeds allowed size')
    from app.tools.knowledge import policy_result_current
    return {'valid': policy_result_current(request.result, request.expected_context, local=True)}
