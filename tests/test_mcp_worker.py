# 文件说明：通过真实 MCP HTTP 测试默认 Worker 分流、身份范围和超时关闭。
"""Default Worker dispatch over real Streamable HTTP and scoped identities."""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.config.settings import settings
from app.mcp.auth import issue_mcp_user_context, verify_mcp_claims
from app.mcp.client import MCPClient, unavailable
from app.services.commerce.common import ok, ref
from app.tasks.candidates import BusinessScope
from app.tasks.conversation_worker import ConversationWorker
from app.tasks.scheduling import TaskSchedule
from app.tools.catalog import BY_NAME
from app.tools.request_context import get_current_user_id, get_remote_tool_context
from tests.test_task_candidates import store, task
from tests.test_task_execution import Client, engine, local_budgets, request, response
from tests.test_task_scheduling import drain
from tests.test_task_storage import empty_store, message
from tests.test_core_tools import database, svc, standard_provider
from tests.test_core_task_tools import business


@pytest.fixture
def mcp_settings(monkeypatch):
    monkeypatch.setattr(settings, 'mcp_enabled', True)
    monkeypatch.setattr(settings, 'mcp_service_token', 'test-service-token-no-business-access')
    monkeypatch.setattr(settings, 'mcp_context_jwt_secret', 'test-mcp-context-secret-at-least-32-characters')
    monkeypatch.setattr(settings, 'mcp_context_token_ttl_seconds', 60)
    monkeypatch.setattr(settings, 'order_platform_codes', 'shop')


@pytest.fixture
def http_server(monkeypatch, mcp_settings):
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from app.mcp.server import MCPAuthenticationMiddleware, list_business_tools, call_business_tool
    calls = []
    class Commerce:
        def __init__(self, _, user, *, scope, **kwargs):
            self.user, self.scope = user, scope
        def read(self, name, args):
            remote = get_remote_tool_context()
            assert remote is not None  # A local bypass must fail this test.
            assert self.user == get_current_user_id()
            calls.append((self.user, self.scope, name, args, remote))
            return ok({'user': self.user, 'platform_code': self.scope.platform_code,
                       'store_id': self.scope.store_id}, sources=[{'kind': 'mysql'}])
    monkeypatch.setattr(importlib.import_module('app.services.commerce.service'), 'CommerceService', Commerce)
    mcp = FastMCP('test-worker-tools', stateless_http=True, json_response=True)
    mcp._mcp_server.list_tools()(list_business_tools)
    mcp._mcp_server.call_tool()(call_business_tool)
    app = MCPAuthenticationMiddleware(mcp.streamable_http_app())
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}/mcp'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on', timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    monkeypatch.setattr(settings, 'mcp_server_url', url)
    try:
        yield url, calls
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def test_default_conversation_worker_uses_remote_read_and_closes_transport(store, http_server):
    _, calls = http_server
    current, source = task(store, 's', goal='查询订单', details={'order_id': 'paid', 'platform_code': 'shop', 'store_id': 'm'})
    jid = TaskSchedule(store, 'alice', scope=BusinessScope('shop', 'm')).enqueue(request(current, source))
    model = Client(response(calls=[('get_order', {'order_ref': ref('order', 'shop', 'paid')})]), response('查询完成。'))
    worker = ConversationWorker(store, engine(model))  # Same default assembly as scripts/task_worker.py.
    try:
        assert drain(worker.executions, [jid])[0]['result']['state'] == 'ready'
    finally:
        worker.close()
    assert len(calls) == 1 and calls[0][:3] == ('alice', BusinessScope('shop', 'm'), 'get_order')
    assert 'ask_user' not in calls[0][4]['allowed_tools']
    with store.transaction(read_only=True) as tx:
        evidence = json.loads(tx.one("SELECT payload FROM support_events WHERE kind='task_tool_result'")['payload'])
    assert evidence['result']['data']['store_id'] == 'm'
    assert not any(t.name == 'mcp-client' for t in threading.enumerate())
    serialized = json.dumps(model.requests)
    assert settings.mcp_service_token not in serialized and settings.mcp_context_jwt_secret not in serialized


def test_real_http_separates_users_scopes_and_tool_grants(http_server):
    url, calls = http_server
    def read(user):
        token = issue_mcp_user_context(user, business_scope={'platform_code': user, 'store_id': 'shop-' + user},
                                      allowed_tools={'get_my_account'})
        client = MCPClient(url, settings.mcp_service_token, token)
        try:
            assert {t['function']['name'] for t in client.connect()} == {'get_my_account'}
            assert not json.loads(client.call_tool('get_order', {'order_ref': ref('order', 'x', 'y')}))['success']
            assert not json.loads(client.call_tool('ask_user', {'question': 'hi'}))['success']
            return json.loads(client.call_tool('get_my_account', {}))['data']
        finally:
            client.close(); client.close()
    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(read, ['alice', 'bob']))
    assert rows == [dict(user=u, platform_code=u, store_id='shop-' + u) for u in ['alice', 'bob']]
    assert len(calls) == 2
    assert get_current_user_id() is None and get_remote_tool_context() is None
    assert not any(t.name == 'mcp-client' for t in threading.enumerate())


def test_real_http_rejects_expired_and_untrusted_context(http_server, monkeypatch):
    url, calls = http_server
    monkeypatch.setattr(settings, 'mcp_context_token_ttl_seconds', -1)
    expired = issue_mcp_user_context('alice')
    for service, context in [(settings.mcp_service_token, expired), ('wrong', expired), (settings.mcp_service_token, 'forged')]:
        result = httpx.post(url, headers={'Authorization': 'Bearer ' + service, 'X-MCP-User-Context': context},
                            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, timeout=3, trust_env=False)
        assert result.status_code == 401
    assert not calls


def test_real_remote_call_timeout_returns_failure_and_closes_client(http_server, monkeypatch):
    url, calls = http_server
    started, release = threading.Event(), threading.Event()
    commerce = importlib.import_module('app.services.commerce.service').CommerceService
    original = commerce.read
    def slow(self, name, args):
        started.set(); release.wait(3)
        return original(self, name, args)
    monkeypatch.setattr(commerce, 'read', slow)
    client = MCPClient(url, settings.mcp_service_token, issue_mcp_user_context('alice'))
    try:
        client.connect(); client.timeout = .15
        result = json.loads(client.call_tool('get_my_account', {}))
        assert started.is_set() and not result['success'] and result['error'] == 'mcp_call_failed_or_unknown'
    finally:
        client.close(); release.set()
    assert client._thread is None


class Remote:
    instances = []
    def __init__(self, url, service, token):
        self.claims, self.closed = verify_mcp_claims(token), False
        self.calls = []
        self.instances.append(self)
    def connect(self): return [BY_NAME['get_order'].definition(), BY_NAME['ask_user'].definition()]
    def call_tool(self, name, args):
        self.calls.append(name)
        return json.dumps(ok({'status': 'paid'}))
    def close(self): self.closed = True


def test_disabled_mcp_uses_local_without_constructing_client(store, monkeypatch):
    monkeypatch.setattr(settings, 'mcp_enabled', False)
    monkeypatch.setattr('app.mcp.MCPClient', lambda *a: pytest.fail('MCP must stay off'))
    calls = []
    monkeypatch.setattr('app.tools.task_dispatch.local_execute_tool', lambda n, a: calls.append(n) or json.dumps(ok({})))
    current, source = task(store, 's', goal='查询订单')
    result = engine(Client(response(calls=[('get_order', {'order_ref': 'order'})]), response('查询完成。'))).execute_task(
        store, 'alice', request(current, source))
    assert result['state'] == 'ready' and calls == ['get_order']


def test_discovery_failure_falls_back_once_without_leaking_secrets(store, monkeypatch, mcp_settings, caplog):
    Remote.instances = []
    class Failed(Remote):
        def connect(self): raise OSError('secret-should-not-appear')
    monkeypatch.setattr('app.mcp.MCPClient', Failed)
    calls = []
    monkeypatch.setattr('app.tools.manager.local_execute_tool', lambda n, a: calls.append(n) or json.dumps(ok({})))
    current, source = task(store, 's', goal='查询订单')
    result = engine(Client(response(calls=[('get_order', {'order_ref': 'order'})]), response('查询完成。'))).execute_task(
        store, 'alice', request(current, source))
    assert result['state'] == 'ready' and calls == ['get_order']
    assert Remote.instances[0].closed and 'secret-should-not-appear' not in caplog.text


def test_sent_remote_read_never_replays_locally_when_retry_discovery_fails(store, monkeypatch, mcp_settings):
    Remote.instances = []
    class Failed(Remote):
        def connect(self):
            if len(self.instances) > 1: raise OSError('offline during retry')
            return super().connect()
        def call_tool(self, name, args):
            self.calls.append(name)
            return unavailable('mcp_call_failed_or_unknown')
    monkeypatch.setattr('app.mcp.MCPClient', Failed)
    monkeypatch.setattr('app.tools.manager.local_execute_tool', lambda *a: pytest.fail('unknown remote read replayed locally'))
    current, source = task(store, 's', goal='查询订单')
    model = Client(response(calls=[('get_order', {'order_ref': 'order'})]), response('暂时无法核实。'))
    result = engine(model).execute_task(store, 'alice', request(current, source))
    assert result['tool_calls'] == 2 and len(Remote.instances) == 2
    assert sum(len(c.calls) for c in Remote.instances) == 1
    assert all(c.closed for c in Remote.instances)


def test_task_only_flow_stays_local_with_mcp_enabled(store, monkeypatch, mcp_settings):
    monkeypatch.setattr('app.mcp.MCPClient', lambda *a: pytest.fail('flow must not open MCP'))
    current, source = task(store, 's', goal='查询订单')
    result = engine(Client(response(calls=[('ask_user', {'question': '需要查询哪一笔订单？'})]))).execute_task(
        store, 'alice', request(current, source))
    assert result['state'] == 'waiting_user'


def test_confirmation_preparation_stays_local_with_mcp_enabled(store, business, standard_provider, monkeypatch, mcp_settings):
    from app.tasks.operations import TaskOperations
    monkeypatch.setattr('app.mcp.MCPClient', lambda *a: pytest.fail('preparation must not open MCP'))
    current, source = task(store, 's', goal='申请退款', details={'order_id': 'paid', 'platform_code': 'shop'})
    args = {'kind': 'refund_only', 'items': [{'order_item_id': 'i-paid', 'quantity': 1}], 'reason': '不需要了'}
    result = engine(Client(response(calls=[('prepare_after_sale', args)]))).execute_task(
        store, 'alice', request(current, source))
    assert result['state'] == 'needs_confirmation'
    with business.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM after_sale_requests')['n'] == 0
    operations = TaskOperations(store, 'alice')
    card = operations.list('s')[0]
    confirmed = operations.confirm('s', card['operation_id'], card['confirmation_token'])
    assert confirmed['status'] == 'succeeded'
    assert operations.confirm('s', card['operation_id'], card['confirmation_token']) == confirmed


def test_real_mcp_business_reads_enforce_user_and_channel_scope(http_server, svc, monkeypatch):
    from app.services.commerce import CommerceService
    url, _ = http_server
    monkeypatch.setattr(importlib.import_module('app.services.commerce.service'), 'CommerceService', CommerceService)
    monkeypatch.setattr(settings, 'order_db_dsn', svc.store.dsn)
    monkeypatch.setattr(settings, 'business_data_keys', json.dumps(svc.cipher.keys))
    def read(user, scope, order):
        client = MCPClient(url, settings.mcp_service_token,
            issue_mcp_user_context(user, business_scope=scope, allowed_tools={'get_order'}))
        try:
            client.connect()
            return json.loads(client.call_tool('get_order', {'order_ref': ref('order', 'shop', order)}))
        finally:
            client.close()
    assert read('alice', {'platform_code': 'shop', 'store_id': 'm'}, 'paid')['data']['order_id'] == 'paid'
    assert not read('bob', {'platform_code': 'shop'}, 'paid')['success']
    assert not read('alice', {'platform_code': 'shop', 'store_id': 'another'}, 'paid')['success']
    assert read('bob', {'platform_code': 'shop'}, 'other')['data']['order_id'] == 'other'


def test_discovery_timeout_cancels_and_closes_background_loop(monkeypatch):
    @asynccontextmanager
    async def hanging(*args, **kwargs):
        await asyncio.Event().wait()
        yield None
    monkeypatch.setattr('app.mcp.client.streamable_http_client', hanging)
    client = MCPClient('http://unused')
    client.timeout = .05
    with pytest.raises(TimeoutError): client.connect()
    client.close()
    assert client._thread is None
    assert not any(t.name == 'mcp-client' for t in threading.enumerate())


def test_timeout_during_discovery_never_sends_late_tool_call(store, monkeypatch, mcp_settings):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Slow(Remote):
        def connect(self):
            started.set(); assert release.wait(5)
            return super().connect()
        def call_tool(self, *args): pytest.fail('late remote call was sent')
        def close(self): closed.set()
    monkeypatch.setattr('app.mcp.MCPClient', Slow)
    current, source = task(store, 's', goal='查询订单')
    model = Client(response(calls=[('get_order', {'order_ref': 'order'})]))
    try:
        result = engine(model).execute_task(store, 'alice', request(current, source, call_seconds=.2))
        assert started.is_set() and result['state'] != 'ready'
    finally:
        release.set()
    assert closed.wait(3)
