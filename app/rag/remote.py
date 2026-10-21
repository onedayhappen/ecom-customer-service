"""Bounded HTTP client for independently deployed retrieval replicas."""
from functools import lru_cache
import time
from weakref import WeakSet

import httpx
from pydantic import BaseModel, Field


class Hit(BaseModel):
    doc: str = Field(max_length=1000)
    section: str = Field(max_length=1000)
    text: str = Field(max_length=20000)
    score: float


class SearchResult(BaseModel):
    success: bool
    backend: str = Field(max_length=100)
    query: str = Field(max_length=4000)
    results: list[Hit] = Field(max_length=5)
    error: str | None = Field(default=None, max_length=1000)
    error_code: str | None = Field(default=None, max_length=100)


_clients = WeakSet()


@lru_cache(maxsize=8)
def client(url, timeout, token):
    value = httpx.Client(base_url=url.rstrip('/'), timeout=httpx.Timeout(timeout, connect=2),
                        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                        headers={'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'},
                        follow_redirects=False, trust_env=False)
    _clients.add(value)
    return value


def close_clients():
    for value in list(_clients):
        value.close()
    _clients.clear()
    client.cache_clear()


def search(query, top_k):
    content = _post('/v1/search', {'query': query, 'top_k': top_k})
    return SearchResult.model_validate_json(content).model_dump(exclude_none=True)


def _post(path, payload):
    from app.config.settings import settings
    import json
    if not settings.retrieval_service_token:
        raise ValueError('Remote retrieval requires service authentication')
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 512000:
        raise ValueError('Retrieval request exceeds allowed size')
    deadline = time.monotonic() + settings.retrieval_timeout_seconds
    with client(settings.retrieval_service_url, settings.retrieval_timeout_seconds,
                settings.retrieval_service_token).stream('POST', path, json=payload) as response:
        response.raise_for_status()
        if response.headers.get('content-encoding', 'identity') != 'identity':
            raise ValueError('Retrieval service must return an uncompressed response')
        content = bytearray()
        # Bound the bytes while downloading, before JSON parsing or allocation
        # of an arbitrarily large response body.
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline:
                raise TimeoutError('Retrieval response deadline exceeded')
            if len(content) + len(chunk) > 512000:
                raise ValueError('Retrieval response exceeds allowed size')
            content.extend(chunk)
    return content


def policy_search(query, top_k, context):
    import json
    from app.rag.policy import PolicyContext
    context = PolicyContext.model_validate(context)
    result = json.loads(_post('/v1/policies/search', dict(query=query, top_k=top_k,
                         context=context.model_dump(mode='json'))))
    if result.get('success') and not policy_validate(result, context):
        raise ValueError('Invalid policy evidence from retrieval service')
    return result


def policy_validate(result, context=None):
    import json
    from app.rag.policy import PolicyContext
    expected = PolicyContext.model_validate(context).model_dump(mode='json') if context is not None else None
    reply = json.loads(_post('/v1/policies/validate', dict(result=result, expected_context=expected)))
    return reply == {'valid': True}
