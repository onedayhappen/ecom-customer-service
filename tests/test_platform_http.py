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
        'API_SESSION_STORAGE_BACKEND': 'postgres', 'API_SESSION_POSTGRES_DSN': dsn,
        'MEMORY_STORAGE_BACKEND': 'json', 'MEMORY_POSTGRES_DSN': '', 'MEMORY_ENABLED': 'false',
        'MEMORY_REDIS_URL': '', 'ORDER_DB_DSN': '', 'ORDER_CACHE_REDIS_URL': '',
        'API_CONCURRENCY_REDIS_URL': REDIS, 'API_CHAT_QUEUE_PREFIX': prefix, 'API_CHAT_EXECUTION_MODE': 'async',
        'API_CHAT_WORKER_ENABLED': 'false', 'API_CHAT_WORKER_CONCURRENCY': '2',
        'API_JWT_SECRET': SECRET, 'INTENT_MODEL_ENABLED': 'false', 'MCP_ENABLED': 'false',
        'OBSERVABILITY_ENABLED': 'false', 'OBSERVABILITY_TRACE_ENABLED': 'false', 'AUDIT_ENABLED': 'false',
        'DEEPSEEK_API_KEY': 'fixture-provider-key', 'OPENAI_API_KEY': '', 'DASHSCOPE_API_KEY': '', 'ROUTER_API_KEY': '',
        'DEEPSEEK_BASE_URL': f'http://127.0.0.1:{model_port}/v1', 'DEEPSEEK_MODEL_NAME': 'fixture',
        'CASE_DIR': str(root / 'cases'), 'CASE_STORAGE_BACKEND': 'postgres', 'RETRIEVAL_SERVICE_TOKEN': SERVICE_TOKEN, 'RETRIEVAL_SERVICE_URL': '',
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
                        return f'platform_runtime_last_success_unixtime{{role="{role}"}}' in response.text
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


def submit(platform, message, sid=None, **extra):
    sid = sid or str(uuid4())
    response = platform['client'].post(platform['urls'][0] + '/api/v1/chat', headers=platform['headers'](),
        json={'session_id': sid, 'request_id': str(uuid4()), 'message': message, **extra})
    assert response.status_code in {200, 202}, response.text
    return sid, response.json()


@pytest.fixture(autouse=True)
def running_runtime(platform, request):
    # test_01 deliberately verifies durable admission before any consumer
    # exists. Other scenarios must also work when selected on their own.
    if request.node.name.startswith('test_01_'):
        return
    for role in ('dispatcher', 'worker'):
        live = sum(process.args[2] == role and process.poll() is None for process in platform['processes'])
        for _ in range(max(0, 2 - live)):
            platform['start'](role)


def completed(platform, job_id):
    def result():
        response = platform['client'].get(platform['urls'][1] + '/api/v1/chat/' + job_id, headers=platform['headers']())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['status'] not in {'failed', 'expired', 'interrupted'}, data
        return data if data['status'] == 'completed' else None
    return wait_until(result)


def test_01_durable_admission_before_dispatchers_and_workers_exist(platform):
    sid, accepted = submit(platform, '查物流 ORD-A')
    assert platform['store'].jobs.get(accepted['job_id'], 'customer')['status'] == 'pending'
    for _ in range(2): platform['start']('dispatcher')
    for _ in range(2): platform['start']('worker')
    assert completed(platform, accepted['job_id'])['result']['intent'] == 'logistics_inquiry'


def test_02_question_answer_confirmation_across_instances(platform):
    sid, accepted = submit(platform, '我要退款')
    completed(platform, accepted['job_id'])
    tasks = platform['store'].tasks.list(sid, 'customer')['tasks']
    task = tasks[0]
    assert task['status'] == 'waiting_input'
    _, accepted = submit(platform, 'ORD-A', sid, task_action='answer', task_id=task['task_id'],
        task_version=task['version'], question_id=task['question']['question_id'])
    token = completed(platform, accepted['job_id'])['result']['confirmation_token']
    with platform['store'].transaction(read_only=True) as tx:
        before = tx.one('SELECT COUNT(*) AS n FROM integration_effects')['n']
    request = {'session_id': sid, 'request_id': str(uuid4()), 'message': '确认', 'confirmation_token': token}
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda url: platform['client'].post(url + '/api/v1/chat', headers=platform['headers'](), json=request), platform['urls']))
    assert all(r.status_code in {200, 202, 409} for r in replies)
    queued = next(r.json() for r in replies if r.status_code == 202)
    completed(platform, queued['job_id'])
    with platform['store'].transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM integration_effects')['n'] == before + 1


def test_03_cancel_slow_model_keeps_control_responsive(platform):
    sid, accepted = submit(platform, '我要退款 ORD-A SLOW')
    wait_until(lambda: platform['store'].jobs.get(accepted['job_id'], 'customer')['status'] == 'running')
    start = time.monotonic()
    submit(platform, '取消退款', sid)
    assert time.monotonic() - start < 2
    assert platform['store'].jobs.get(accepted['job_id'], 'customer')['status'] == 'superseded'
    time.sleep(3.5)
    assert platform['store'].tasks.list(sid, 'customer')['tasks'][0]['status'] == 'cancelled'


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


def test_07_worker_crash_after_business_commit_is_not_replayed(platform):
    sid, accepted = submit(platform, '我要退款 ORD-CRASH')
    token = completed(platform, accepted['job_id'])['result']['confirmation_token']
    request_id = str(uuid4())
    _, accepted = submit(platform, '确认', sid, confirmation_token=token, request_id=request_id)
    def committed():
        with platform['store'].transaction(read_only=True) as tx:
            return tx.one("SELECT COUNT(*) AS n FROM integration_effects WHERE order_id='ORD-CRASH'")['n'] == 1
    wait_until(committed)
    stop_role(platform, 'worker')
    with platform['store'].transaction() as tx:
        tx.execute('UPDATE runtime_jobs SET run_deadline=0 WHERE job_id=?', (accepted['job_id'],))
    wait_until(lambda: platform['store'].jobs.get(accepted['job_id'], 'customer')['status'] == 'interrupted')
    assert platform['store'].state(sid, 'customer')['mode'] == 'handoff_pending'
    for _ in range(2): platform['start']('worker')
    submit(platform, '确认', sid, confirmation_token=token, request_id=request_id)
    assert committed()
    response = platform['client'].post(platform['urls'][1] + f'/api/v1/sessions/{sid}/resume-bot', headers=platform['headers']())
    assert response.status_code == 409


def test_08_bounded_load_and_metrics(platform):
    import math
    import platform as operating_system
    count = 24
    started = time.monotonic()
    def roundtrip(index):
        begin = time.monotonic()
        _, response = submit(platform, f'查物流 ORD-LOAD-{index}')
        admitted = time.monotonic() - begin
        completed(platform, response['job_id'])
        return admitted, time.monotonic() - begin
    with ThreadPoolExecutor(6) as pool:
        observations = list(pool.map(roundtrip, range(count)))
    elapsed = time.monotonic() - started
    def percentile(values, q): return sorted(values)[max(0, math.ceil(len(values) * q) - 1)]
    report = {'scenario': 'synthetic logistics; real PostgreSQL/Redis/HTTP; 2 API, 2 dispatcher, 2 Worker',
        'samples': count, 'clients': 6, 'elapsed_seconds': elapsed, 'completed_per_second': count / elapsed,
        'arrival_model': 'closed loop, 6 clients, one outstanding request per client',
        'observed_request_starts_per_second': count / elapsed,
        'admission_p95_seconds': percentile([o[0] for o in observations], .95),
        'admission_p99_seconds': percentile([o[0] for o in observations], .99),
        'completion_p95_seconds': percentile([o[1] for o in observations], .95),
        'completion_p99_seconds': percentile([o[1] for o in observations], .99), 'errors': 0,
        'os': operating_system.platform(), 'cpu_count': os.cpu_count(),
        'limitation': 'Small reproducible regression load, not production capacity or real model throughput.'}
    (ROOT / '.codex' / 'concurrency-load-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    metrics = platform['client'].get(platform['urls'][0] + '/metrics')
    assert metrics.status_code == 200 and 'platform_oldest_waiting_job_seconds' in metrics.text
    for process in platform['processes']:
        if process.args[2] == 'worker' and process.poll() is None:
            response = platform['client'].get('http://127.0.0.1:' + process.args[3] + '/metrics')
            assert response.status_code == 200 and 'platform_job_execution_seconds' in response.text


def test_09_api_broker_partition_keeps_admission_and_handoff_available(platform):
    stop_role(platform, 'dispatcher')
    _, url = platform['start']('api', API_CONCURRENCY_REDIS_URL=f'redis://127.0.0.1:{port()}/0')
    try:
        assert platform['client'].get(url + '/ready').status_code == 200
        sid, rid = str(uuid4()), str(uuid4())
        response = platform['client'].post(url + '/api/v1/chat', headers=platform['headers'](),
            json={'message': '查物流 ORD-A', 'session_id': sid, 'request_id': rid})
        assert response.status_code == 202, response.text
        assert platform['store'].jobs.get(response.json()['job_id'], 'customer')['status'] == 'pending'
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


def test_12_job_event_stream_reports_cancellation_and_terminates(platform):
    sid, accepted = submit(platform, '我要退款 ORD-A SLOW')
    wait_until(lambda: platform['store'].jobs.get(accepted['job_id'], 'customer')['status'] == 'running')
    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            async with client.stream('GET', platform['urls'][1] + accepted['events_url'], headers=platform['headers']()) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                await anext(lines)
                cancelled = await client.post(platform['urls'][0] + '/api/v1/chat', headers=platform['headers'](),
                    json={'message': '取消退款', 'session_id': sid, 'request_id': str(uuid4())})
                assert cancelled.status_code == 200
                statuses = []
                async for line in lines:
                    if line.startswith('data: '): statuses.append(json.loads(line[6:])['status'])
                assert statuses[-1] == 'superseded'
    asyncio.run(run())
