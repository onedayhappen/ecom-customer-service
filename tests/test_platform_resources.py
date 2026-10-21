"""Resource isolation and remote retrieval contracts."""
from tests.intake_fixtures import receive_message
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.runtime.budgets import Buckets, RateExceeded, admit_request, model_budget
from app.runtime.notifications import NotificationHub, NotificationPublisher
from app.support.store import SupportStore


def test_hierarchical_limit_does_not_spend_other_buckets_on_rejection():
    limits = Buckets('', 'test')
    limits.consume([('global', 2, 1), ('user-a', 1, 1)])
    with pytest.raises(RateExceeded): limits.consume([('global', 2, 1), ('user-a', 1, 1)])
    limits.consume([('global', 2, 1), ('user-b', 1, 1)])


def test_control_channel_has_separate_budget_and_tenant_is_server_bound(monkeypatch):
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')
    monkeypatch.setattr(settings, 'api_chat_queue_prefix', str(uuid4()))
    monkeypatch.setattr(settings, 'api_user_rpm', 1)
    monkeypatch.setattr(settings, 'api_tenant_rpm', 1)
    monkeypatch.setattr(settings, 'api_user_tenants', {'a': 'shop', 'b': 'shop', 'c': 'other'})
    admit_request('a')
    with pytest.raises(RateExceeded): admit_request('b')
    admit_request('c')
    admit_request('a', 'control')


def test_model_tokens_are_reserved_before_provider_call(monkeypatch):
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')
    monkeypatch.setattr(settings, 'api_chat_queue_prefix', str(uuid4()))
    monkeypatch.setattr(settings, 'model_token_budget_per_minute', 600)
    with pytest.raises(RateExceeded):
        with model_budget('fixture', {'messages': [{'content': 'large' * 200}], 'max_tokens': 100}):
            pytest.fail('provider must not run')


def test_redis_failure_preserves_bounded_durable_admission(monkeypatch):
    from app.runtime.budgets import buckets
    from app.runtime.concurrency import CoordinationUnavailable
    prefix = str(uuid4())
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', 'redis://offline:6379/0')
    monkeypatch.setattr(settings, 'api_chat_queue_prefix', prefix)
    limiter = buckets(settings.api_concurrency_redis_url, prefix)
    def unavailable(specs): raise CoordinationUnavailable('offline')
    monkeypatch.setattr(limiter, 'consume', unavailable)
    for _ in range(10): admit_request('customer')
    with pytest.raises(RateExceeded): admit_request('customer')
    admit_request('customer', 'control')


def test_notification_hub_coalesces_and_releases_connections():
    async def run():
        hub = NotificationHub('', 'test', max_connections=1)
        async with hub.subscribe('s') as event:
            for _ in range(100): hub.wake('s')
            await asyncio.wait_for(event.wait(), 1)
            assert hub.connections == 1
            from app.support.store import SupportError
            with pytest.raises(SupportError):
                async with hub.subscribe('other'): pass
        assert hub.connections == 0
    asyncio.run(run())


def test_notification_outbox_does_not_drop_update_during_publish(tmp_path):
    store = SupportStore(f'sqlite:///{tmp_path / "notify.db"}')
    store.ensure('s', 'u')
    receive_message(store, 's', 'u', 'message', 'first')
    publisher = NotificationPublisher(store, '', 'test')
    class Client:
        def publish(self, channel, payload):
            store.handoff('s', 'u')
    publisher.client = Client()
    assert publisher.publish_one()
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM runtime_notifications')['n'] == 1
    publisher.client = SimpleNamespace(publish=lambda *args: None)
    assert publisher.publish_one()
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM runtime_notifications')['n'] == 0


def test_retrieval_service_auth_and_remote_result(monkeypatch):
    from app.services.retrieval import app
    from app.tools import knowledge
    from app.rag import remote
    token = 'test-retrieval-internal-token-32chars'
    monkeypatch.setattr(settings, 'retrieval_service_token', token)
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')
    monkeypatch.setattr(knowledge, 'search_knowledge_local', lambda query, top_k: {
        'success': True, 'backend': 'fixture', 'query': query,
        'results': [{'doc': 'policy', 'section': 'returns', 'text': '政策依据', 'score': .9}]})
    client = TestClient(app)  # Exercise routes without loading heavyweight fixture models.
    try:
        assert client.post('/v1/search', json={'query': '退货'}).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + token
        assert client.post('/v1/search', json={'query': '退货', 'top_k': 8}).status_code == 422
        monkeypatch.setattr(settings, 'retrieval_service_url', 'http://retrieval.fixture')
        monkeypatch.setattr(remote, 'client', lambda *args: client)
        result = knowledge.search_knowledge('退货', 3)
        assert result['success'] and result['results'][0]['text'] == '政策依据'
    finally:
        client.close()


def test_sse_cancel_during_acquisition_returns_late_slot(monkeypatch):
    import threading
    from app.runtime import streaming
    started, proceed = threading.Event(), threading.Event()
    released = []
    def acquire():
        started.set()
        assert proceed.wait(3)
        return 'late-token'
    resource = SimpleNamespace(acquire=acquire, release=released.append)
    async def run():
        task = asyncio.create_task(streaming.acquire_connection(resource))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Nested ASGI cancellation must not skip cleanup either.
        proceed.set()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert released == ['late-token']


def test_sse_slow_send_and_header_disconnect_always_cleanup(monkeypatch):
    from app.runtime.streaming import BoundedStreamingResponse
    from starlette.background import BackgroundTask
    monkeypatch.setattr(settings, 'api_sse_send_timeout_seconds', .02)
    async def run():
        closed, cleanup = [], []
        async def body():
            try:
                yield 'first'
            finally:
                closed.append(True)
        async def receive():
            await asyncio.sleep(10)
        async def slow(message):
            if message['type'] == 'http.response.body': await asyncio.sleep(10)
        async def finish(): cleanup.append(True)
        scope = {'type': 'http', 'asgi': {'spec_version': '2.4'}}
        response = BoundedStreamingResponse(body(), background=BackgroundTask(finish))
        await asyncio.wait_for(response(scope, receive, slow), 1)
        assert closed == [True] and cleanup == [True]
        async def disconnected(message): raise OSError('client disconnected before headers')
        response = BoundedStreamingResponse(body(), background=BackgroundTask(finish))
        from starlette.requests import ClientDisconnect
        with pytest.raises(ClientDisconnect): await response(scope, receive, disconnected)
        assert cleanup == [True, True]
    asyncio.run(run())


def test_local_live_lease_survives_identity_cache_churn():
    from app.runtime.budgets import lease
    key = str(uuid4())
    first = lease('', key, 1, 60)
    token = first.acquire()
    try:
        for _ in range(600): lease('', str(uuid4()), 1, 60)
        second = lease('', key, 1, 60)
        assert second is first
        assert second.acquire() is None
    finally:
        first.release(token)


def test_remote_retrieval_stops_reading_oversized_response(monkeypatch):
    import httpx
    from app.rag import remote
    consumed, closed = [], []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                consumed.append(True)
                yield b'x' * 256000
        def close(self): closed.append(True)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))
    with httpx.Client(transport=transport, base_url='http://fixture') as http:
        monkeypatch.setattr(remote, 'client', lambda *args: http)
        monkeypatch.setattr(settings, 'retrieval_service_token', 'fixture')
        with pytest.raises(ValueError, match='exceeds allowed size'): remote.search('test', 3)
    assert len(consumed) == 3 and closed == [True]


def test_async_model_cancellation_keeps_sync_provider_slot_until_return(monkeypatch):
    import threading
    from app.observability.llm_client import LlmClient
    from app.runtime.budgets import lease
    from hashlib import sha256
    started, proceed = threading.Event(), threading.Event()
    prefix = str(uuid4())
    monkeypatch.setattr(settings, 'api_chat_queue_prefix', prefix)
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')
    monkeypatch.setattr(settings, 'model_max_concurrency', 1)
    resource = lease('', prefix + ':model:' + sha256(b'fixture').hexdigest(), 1, settings.api_distributed_lease_seconds)
    def create(**kwargs):
        started.set()
        assert proceed.wait(3)
        return 'done'
    llm = LlmClient(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    async def run():
        task = asyncio.create_task(llm._raw_acreate(model='fixture', messages=[], max_tokens=1))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert resource.acquire() is None
        proceed.set()
    asyncio.run(run())  # Drains the executor: the provider has actually returned.
    token = resource.acquire()
    assert token
    resource.release(token)


def test_true_async_model_cancel_during_admission_does_not_leak(monkeypatch):
    from contextlib import contextmanager
    import threading
    from app.runtime import budgets
    from app.observability.llm_client import LlmClient
    started, proceed = threading.Event(), threading.Event()
    released, calls = [], []
    @contextmanager
    def budget(*args):
        started.set()
        assert proceed.wait(3)
        try: yield
        finally: released.append(True)
    async def create(**kwargs): calls.append(True)
    monkeypatch.setattr(budgets, 'model_budget', budget)
    llm = LlmClient(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    async def run():
        task = asyncio.create_task(llm._raw_acreate(model='fixture', messages=[]))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert released == [True] and calls == []


def test_lost_agent_lease_blocks_following_model_or_business_steps():
    import threading
    from app.runtime.concurrency import DistributedLease, execution_lease_guard, CoordinationUnavailable
    from app.support.runtime import check_bot
    resource = DistributedLease('', 'lost-fixture', 1, lease_seconds=.03)
    resource._redis = SimpleNamespace(eval=lambda *args: 1 if args[0] == resource._ACQUIRE else 0,
                                      zrem=lambda *args: None)
    lost = threading.Event()
    with pytest.raises(CoordinationUnavailable):
        with execution_lease_guard(lost), resource.slot(lost_event=lost) as acquired:
            assert acquired and lost.wait(1)
            with pytest.raises(CoordinationUnavailable): check_bot()


def test_embedding_budget_is_separate_and_blocks_before_network(monkeypatch):
    from app.rag.embedder import Embedder
    from app.runtime.budgets import embedding_budget
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')
    monkeypatch.setattr(settings, 'api_chat_queue_prefix', str(uuid4()))
    monkeypatch.setattr(settings, 'embedding_token_budget_per_minute', 520)
    calls = []
    embedder = Embedder('fixture', 'http://fixture', provider='openai')
    embedder._client = SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kw: calls.append(kw)))
    with pytest.raises(RateExceeded): embedder.encode(['oversized-input'])
    assert not calls
    with model_budget('fixture', {'max_tokens': 1}): pass
