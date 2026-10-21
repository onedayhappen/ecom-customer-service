"""Real HTTP, PostgreSQL, Redis and multiple OS processes; synthetic business data."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest

from app.support.store import SupportStore
from tests.test_concurrency_integration import DSN, REDIS

pytestmark = pytest.mark.skipif(os.getenv('ECOM_INTEGRATION') != '1', reason='isolated real infrastructure required')
ROOT = Path(__file__).resolve().parents[1]
SECRET = 'isolated-platform-http-jwt-32-characters'
SERVICE_TOKEN = 'isolated-retrieval-service-32-characters'


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_until(check, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('condition did not become true')


@pytest.fixture(scope='module')
def platform(tmp_path_factory):
    import psycopg
    from psycopg import sql
    root = tmp_path_factory.mktemp('platform-http')
    schema = 'http_' + uuid4().hex
    with psycopg.connect(DSN) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == 'ecom_concurrency_test'
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    dsn = DSN + '?options=-csearch_path%3D' + schema
    store = SupportStore(dsn)
    store.initialize()
    store.seed_staff('staff', 'agent')
    with store.transaction() as tx:
        tx.execute('CREATE TABLE integration_effects(id SERIAL PRIMARY KEY,order_id TEXT NOT NULL)')
    model_port = port()
    prefix = 'http:' + uuid4().hex
    env = {**os.environ, 'ECOM_INTEGRATION': '1', 'SUPPORT_DATABASE_URL': dsn,
         'API_SESSION_POSTGRES_DSN': dsn,
        'MEMORY_STORAGE_BACKEND': 'json', 'MEMORY_POSTGRES_DSN': '', 'MEMORY_ENABLED': 'false',
        'MEMORY_REDIS_URL': '', 'ORDER_DB_DSN': '', 'ORDER_CACHE_REDIS_URL': '',
        'API_CONCURRENCY_REDIS_URL': REDIS, 'API_CHAT_QUEUE_PREFIX': prefix,
        'API_CHAT_WORKER_CONCURRENCY': '2',
        'API_JWT_SECRET': SECRET, 'MCP_ENABLED': 'false',
        'OBSERVABILITY_ENABLED': 'false', 'OBSERVABILITY_TRACE_ENABLED': 'false', 'AUDIT_ENABLED': 'false',
        'DEEPSEEK_API_KEY': 'fixture-provider-key', 'OPENAI_API_KEY': '', 'DASHSCOPE_API_KEY': '', 'ROUTER_API_KEY': '',
        'DEEPSEEK_BASE_URL': f'http://127.0.0.1:{model_port}/v1', 'DEEPSEEK_MODEL_NAME': 'fixture',
        'ECOM_TEST_ARTIFACT_DIR': str(root), 'RETRIEVAL_SERVICE_TOKEN': SERVICE_TOKEN, 'RETRIEVAL_SERVICE_URL': '',
        'API_SSE_CATCHUP_SECONDS': '5', 'RETRIEVAL_GLOBAL_CONCURRENCY': '1',
        'RUNTIME_METRICS_PORT': '0', 'API_QUEUE_WAIT_SECONDS': '2',
        'API_GLOBAL_RPM': '10000', 'API_TENANT_RPM': '10000', 'API_USER_RPM': '1000'}
    processes, logs = [], []
    client = httpx.Client(timeout=15, trust_env=False)
    def start(role, **overrides):
        p = port() if role != 'model' else model_port
        if role in {'worker', 'dispatcher'}:
            overrides = {'RUNTIME_METRICS_PORT': str(p), **overrides}
        log = (root / f'{role}-{p}.log').open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, 'tests/platform_process.py', role, str(p)], cwd=ROOT,
            env={**env, **overrides}, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        processes.append(process); logs.append(log)
        if role in {'api', 'model', 'retrieval', 'worker', 'dispatcher'}:
            def ready():
                if process.poll() is not None:
                    log.flush()
                    raise AssertionError((root / f'{role}-{p}.log').read_text(encoding='utf-8'))
                path = '/metrics' if role in {'worker', 'dispatcher'} else '/health'
                try:
                    response = client.get(f'http://127.0.0.1:{p}' + path)
                    if response.status_code != 200:
                        return False
                    if role in {'worker', 'dispatcher'}:
                        metric_role = 'notifications' if role == 'dispatcher' else role
                        return f'platform_runtime_last_success_unixtime{{role="{metric_role}"}}' in response.text
                    return True
                except httpx.TransportError: return False
            wait_until(ready)
        return process, f'http://127.0.0.1:{p}'
    def headers(user='customer'):
        return {'Authorization': 'Bearer ' + jwt.encode({'sub': user, 'exp': datetime.now(timezone.utc) + timedelta(minutes=30)}, SECRET, algorithm='HS256')}
    try:
        start('model')
        api1, url1 = start('api')
        api2, url2 = start('api')
        yield dict(store=store, start=start, client=client, urls=[url1, url2], headers=headers, processes=processes,
                   api_processes=[api1, api2], root=root, env=env)
    finally:
        client.close()
        for process in reversed(processes):
            if process.poll() is None: process.terminate()
        for process in reversed(processes):
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        for log in logs: log.close()


@pytest.fixture(autouse=True)
def running_runtime(platform, request):
    for role in ('dispatcher', 'worker'):
        live = sum(process.args[2] == role and process.poll() is None for process in platform['processes'])
        for _ in range(max(0, 2 - live)):
            platform['start'](role)


def test_04_sse_cross_instance_notification_and_cursor(platform):
    sid = str(uuid4())
    platform['store'].ensure(sid, 'customer')
    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            async with client.stream('GET', platform['urls'][1] + f'/api/v1/sessions/{sid}/support-events', headers=platform['headers']()) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                await anext(lines)  # Subscription is established before the write.
                started = time.monotonic()
                await client.post(platform['urls'][0] + f'/api/v1/sessions/{sid}/handoff', headers=platform['headers']())
                async for line in lines:
                    if line.startswith('data: '):
                        data = json.loads(line[6:])
                        assert data['state']['mode'] == 'handoff_pending'
                        assert time.monotonic() - started < 2  # Shorter than the 5-second catch-up interval.
                        return data['cursor']
    cursor = asyncio.run(run())
    response = platform['client'].get(platform['urls'][0] + f'/api/v1/sessions/{sid}/messages?after={cursor}', headers=platform['headers']())
    assert response.json()['events'] == []


def test_05_retrieval_replicas_share_resource_limit(platform):
    _, first = platform['start']('retrieval')
    _, second = platform['start']('retrieval')
    headers = {'Authorization': 'Bearer ' + SERVICE_TOKEN}
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda url: platform['client'].post(url + '/v1/search', headers=headers,
            json={'query': 'SLOW 政策', 'top_k': 3}), [first, second]))
    assert sorted(r.status_code for r in responses) == [200, 429]
    result = platform['client'].post(second + '/v1/search', headers=headers, json={'query': '政策'}).json()
    assert result['success'] and result['backend'] == 'chroma'
    assert result['results'][0]['doc'] == 'fixture-policy'


def stop_role(platform, role):
    for process in platform['processes']:
        if process.args[2] == role and process.poll() is None:
            process.terminate()
            process.wait(timeout=8)


def test_06_lost_notification_is_recovered_by_cursor(platform):
    stop_role(platform, 'dispatcher')
    sid = str(uuid4())
    platform['store'].ensure(sid, 'customer')
    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=12) as client:
            async with client.stream('GET', platform['urls'][1] + f'/api/v1/sessions/{sid}/support-events', headers=platform['headers']()) as response:
                lines = response.aiter_lines()
                async for line in lines:
                    if line == ': heartbeat': break
                await client.post(platform['urls'][0] + f'/api/v1/sessions/{sid}/handoff', headers=platform['headers']())
                async for line in lines:
                    if line.startswith('data: '):
                        assert json.loads(line[6:])['state']['mode'] == 'handoff_pending'
                        return
    try:
        asyncio.run(run())
    finally:
        for _ in range(2): platform['start']('dispatcher')


def test_09_api_broker_partition_keeps_admission_and_handoff_available(platform):
    stop_role(platform, 'dispatcher')
    _, url = platform['start']('api', API_CONCURRENCY_REDIS_URL=f'redis://127.0.0.1:{port()}/0')
    try:
        assert platform['client'].get(url + '/ready').status_code == 200
        sid, rid = str(uuid4()), str(uuid4())
        response = platform['client'].post(url + '/api/v1/task-messages', headers=platform['headers'](),
            json={'message': '查物流 ORD-A', 'session_id': sid, 'request_id': rid})
        assert response.status_code == 202, response.text
        assert response.json()['status'] == 'accepted'
        handoff = platform['client'].post(url + f'/api/v1/sessions/{sid}/handoff', headers=platform['headers']())
        assert handoff.status_code == 200, handoff.text
    finally:
        for _ in range(2): platform['start']('dispatcher')


def test_10_sse_user_quota_is_shared_and_disconnect_releases_it(platform):
    _, first = platform['start']('api', API_SSE_PER_USER='1')
    _, second = platform['start']('api', API_SSE_PER_USER='1')
    sid, user = str(uuid4()), 'sse-quota-user'
    platform['store'].ensure(sid, user)
    path = f'/api/v1/sessions/{sid}/support-events'
    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            async with client.stream('GET', first + path, headers=platform['headers'](user)) as one:
                assert one.status_code == 200
                assert (await client.get(second + path, headers=platform['headers'](user))).status_code == 429
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                async with client.stream('GET', second + path, headers=platform['headers'](user)) as two:
                    if two.status_code == 200:
                        return
                    assert two.status_code == 429
                await asyncio.sleep(.05)
            pytest.fail('Disconnected SSE lease was not released')
    asyncio.run(run())


def test_11_sse_closes_after_role_revocation_and_token_expiry(platform):
    store = platform['store']
    sid = str(uuid4())
    store.ensure(sid, 'revocation-customer')
    store.seed_staff('revocation-admin', 'admin')
    store.seed_staff('revocation-agent', 'agent')
    ticket = store.handoff(sid, 'revocation-customer')['ticket']
    store.change(ticket['ticket_id'], 'revocation-agent', ticket['version'], 'claim')
    path = f'/api/v1/sessions/{sid}/support-events'
    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            async with client.stream('GET', platform['urls'][0] + path + '?staff=true&ticket_id=' + ticket['ticket_id'],
                                     headers=platform['headers']('revocation-agent')) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                await anext(lines)
                store.update_staff('revocation-admin', 'revocation-agent', 'agent', False)
                async for line in lines:
                    if line == 'event: revoked': break
                else: pytest.fail('Disabled agent stream did not report revocation')
            token = jwt.encode({'sub': 'revocation-customer', 'exp': datetime.now(timezone.utc) + timedelta(seconds=3)}, SECRET, algorithm='HS256')
            async with client.stream('GET', platform['urls'][1] + path, headers={'Authorization': 'Bearer ' + token}) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line == 'event: revoked': return
                pytest.fail('Expired token did not close the stream')
    asyncio.run(run())
