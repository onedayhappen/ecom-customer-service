"""Actual API/worker processes, database controls, restart and continued input."""
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from tests.test_platform_http import platform, ROOT, wait_until, stop_role

pytestmark = pytest.mark.skipif(os.getenv('ECOM_INTEGRATION') != '1', reason='isolated PostgreSQL/Redis required')


def send(platform, sid, text, *, user='customer', rid=None, peer=0):
    body = dict(session_id=sid, request_id=rid or str(uuid4()), message=text)
    response = platform['client'].post(platform['urls'][peer] + '/api/v1/task-messages',
                                      headers=platform['headers'](user), json=body)
    assert response.status_code == 202, response.text
    return body, response.json()


def assistant(platform, sid, user='customer'):
    return [m for m in platform['store'].public_history(sid, user) if m['role'] == 'assistant']


def command(platform, mode):
    process = subprocess.run([sys.executable, '-m', 'scripts.pipeline_rollout', 'set', mode,
                              '--operator', 'isolated-operator', '--reason', 'HTTP rollback drill'],
        cwd=ROOT, env=platform['env'], capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert process.returncode == 0, process.stderr[-2000:]


def workers(platform):
    for _ in range(2):
        platform['start']('worker', TASK_WORKER_ENABLED='true', TASK_INTAKE_ENABLED='true')


def test_01_committed_input_survives_no_worker_duplicate_http_and_parallel_consumers(platform):
    sid = str(uuid4())
    first, receipt = send(platform, sid, '查询 ORD-A 当前状态')
    _, replay = send(platform, sid, first['message'], rid=first['request_id'], peer=1)
    assert receipt == replay
    send(platform, sid, '也查询 ORD-B 当前状态', peer=1)
    assert assistant(platform, sid) == []
    workers(platform)
    platform['start']('dispatcher')
    wait_until(lambda: all(order in str(assistant(platform, sid)) for order in ('ORD-A', 'ORD-B')), seconds=35)
    with platform['store'].transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM conversation_tasks WHERE session_id=? AND storage_mode='durable'", (sid,))['n'] == 2
        assert tx.one("SELECT COUNT(*) AS n FROM support_events WHERE session_id=? AND kind='task_tool_result'", (sid,))['n'] == 2
        assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE kind='conversation'")['n'] == 0
    legacy = platform['client'].post(platform['urls'][0] + '/api/v1/chat',
                                     headers=platform['headers'](), json=first)
    assert legacy.status_code == 410


def test_cli_stop_preserves_reads_human_and_cross_session_input_then_resumes(platform):
    if not any(p.args[2] == 'worker' and p.poll() is None for p in platform['processes']):
        workers(platform)
    command(platform, 'stopped')
    sids = [str(uuid4()), str(uuid4())]
    for sid, order in zip(sids, ['ORD-C', 'ORD-D']):
        send(platform, sid, f'查询 {order}', user='rollout-user')
    for url in platform['urls']:
        assert platform['client'].get(url + '/ready').json()['automation'] == 'stopped'
        progress = platform['client'].get(url + f'/api/v1/task-messages/{sids[0]}', headers=platform['headers']('rollout-user'))
        assert progress.status_code == 200 and progress.json()['automatic_processing'] is False
    with platform['store'].transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE user_id='rollout-user'")['n'] == 0
    human_sid = str(uuid4())
    response = platform['client'].post(platform['urls'][1] + f'/api/v1/sessions/{human_sid}/handoff', headers=platform['headers']('human-user'))
    assert response.status_code == 200 and response.json()['ticket']
    command(platform, 'all')
    for sid, order in zip(sids, ['ORD-C', 'ORD-D']):
        wait_until(lambda: order in str(assistant(platform, sid, 'rollout-user')), seconds=35)


def test_worker_loss_during_association_reclaims_original_messages(platform):
    if not any(p.args[2] == 'worker' and p.poll() is None for p in platform['processes']):
        workers(platform)
    sid, user = str(uuid4()), 'crash-user'
    send(platform, sid, 'SLOW 查询 ORD-E', user=user)
    def running():
        with platform['store'].transaction(read_only=True) as tx:
            return tx.one("SELECT job_id FROM runtime_jobs WHERE session_id=? AND kind='association' AND status='running'", (sid,))
    wait_until(running)
    stop_role(platform, 'worker')
    with platform['store'].transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET run_deadline=0 WHERE session_id=? AND status='running'", (sid,))
    workers(platform)
    wait_until(lambda: 'ORD-E' in str(assistant(platform, sid, user)), seconds=35)
    with platform['store'].transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM message_intake_items WHERE session_id=?', (sid,))['n'] == 1
        assert tx.one('SELECT COUNT(*) AS n FROM conversation_tasks WHERE session_id=?', (sid,))['n'] == 1
        assert tx.one('SELECT COUNT(*) AS n FROM support_operations WHERE session_id=?', (sid,))['n'] == 0


def test_handoff_while_model_is_running_is_fast_and_fences_late_reply(platform):
    if not any(p.args[2] == 'worker' and p.poll() is None for p in platform['processes']):
        workers(platform)
    sid, user = str(uuid4()), 'handoff-user'
    send(platform, sid, 'SLOW 查询 ORD-F', user=user)
    def running():
        with platform['store'].transaction(read_only=True) as tx:
            return tx.one("SELECT job_id FROM runtime_jobs WHERE session_id=? AND kind='task_execution' AND status='running'", (sid,))
    wait_until(running, seconds=30)
    started = time.monotonic()
    response = platform['client'].post(platform['urls'][1] + f'/api/v1/sessions/{sid}/handoff', headers=platform['headers'](user))
    assert response.status_code == 200 and time.monotonic() - started < 2
    # Wait for the detached query/model result to arrive before inspecting.
    time.sleep(4)
    assert not any('ORD-F' in m['content'] for m in assistant(platform, sid, user))
    assert platform['store'].state(sid, user)['mode'] == 'handoff_pending'
