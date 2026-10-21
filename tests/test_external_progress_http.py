"""Actual maintenance process, business database, two APIs and SSE replay."""
import asyncio
import json
import os
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from app.tasks.operations import TaskOperations
from app.tasks.repository import TaskRepository
from tests.test_platform_http import platform, ROOT
from tests.test_task_operations_mysql import business_db, DSN
from tests.test_task_operations import flags
from tests.test_task_candidates import task

pytestmark = pytest.mark.skipif(os.environ.get('ECOM_INTEGRATION') != '1', reason='isolated PostgreSQL/Redis/MySQL required')


def test_maintenance_restart_and_sse_replay_preserve_one_progress_event(platform, business_db):
    _, order = business_db
    sid = str(uuid4())
    store, client, urls = platform['store'], platform['client'], platform['urls']
    store.ensure(sid, 'alice')
    record, _ = task(store, sid, goal='跟进退款到账', details=dict(order_id=order, reason='不想要了'))
    operations = TaskOperations(store, 'alice')
    operations.prepare(sid, record.task_id, record.version, 'apply_refund')
    card = operations.list(sid)[0]
    assert operations.confirm(sid, card['operation_id'], card['confirmation_token'])['status'] == 'succeeded'
    headers = platform['headers']('alice')
    before = client.get(urls[0] + f'/api/v1/sessions/{sid}/transcript', headers=headers).json()['cursor']
    env = {**platform['env'], 'TASK_EXTERNAL_PROGRESS_ENABLED': 'true', 'ORDER_DB_DSN': DSN,
           'SHORT_TERM_MEMORY_ENABLED': 'false', 'MEMORY_ENABLED': 'false'}
    def maintenance():
        completed = subprocess.run([sys.executable, '-m', 'scripts.platform_maintenance', '--once'],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=45,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert completed.returncode == 0, completed.stderr[-3000:]
    maintenance()
    async def replay():
        async with httpx.AsyncClient(trust_env=False, timeout=15) as connection:
            async with connection.stream('GET', urls[1] + f'/api/v1/sessions/{sid}/support-events',
                headers={**headers, 'Last-Event-ID': str(before)}) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        return json.loads(line[6:])
    recovered = asyncio.run(replay())
    progress = [e for e in recovered['events'] if e['payload'].get('external_event_id')]
    assert len(progress) == 1
    assert '仍待确认' in progress[0]['payload']['content']
    current = TaskRepository(store, 'alice').get(record.task_id)
    assert current.status == 'waiting_external'
    # No callback receiver exists in this project. A client cannot manufacture
    # a terminal observation through an invented business progress endpoint.
    assert client.post(urls[0] + '/api/v1/external-progress', headers=headers,
        json=dict(task_id=record.task_id, phase='funds_settled')).status_code == 404
    with store.transaction() as tx:
        tx.execute('UPDATE task_external_waits SET next_check=0 WHERE task_id=?', (record.task_id,))
    maintenance()
    assert TaskRepository(store, 'alice').get(record.task_id).version == current.version
    refreshed = client.get(urls[0] + f'/api/v1/sessions/{sid}/transcript', headers=headers).json()
    assert len([e for e in refreshed['events'] if e['payload'].get('external_event_id')]) == 1
    assert client.get(urls[1] + f'/api/v1/sessions/{sid}/transcript', headers=platform['headers']('foreign')).status_code == 404
