"""Actual chat routing with real workflows, isolated order/LLM substitutes."""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.config.settings import settings
from app.support.runtime import get_store


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.session import repository
    from app.runtime.concurrency import DistributedLease
    from app.agents.engine import AgentEngine
    from app.intent.query_rewrite import RewriteOutcome
    from app.intent import query_rewrite
    from app.tools import registry
    from app.data_plan.enums import Intent
    import app.intent

    monkeypatch.setattr(settings, 'support_database_url', f"sqlite:///{tmp_path / 'tasks.db'}")
    monkeypatch.setattr(settings, 'api_session_storage_backend', 'json')
    monkeypatch.setattr(settings, 'case_dir', str(tmp_path / 'cases'))
    monkeypatch.setattr(settings, 'api_chat_execution_mode', 'sync')
    monkeypatch.setattr(settings, 'api_jwt_secret', 'task-tests-isolated-secret-at-least-32bytes')
    monkeypatch.setattr(repository, 'JSON_SESSION_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(main, '_llm_lease', DistributedLease('', 'tasks-test', 4))
    def model(**kw):
        latest = next(m['content'] for m in reversed(kw['messages']) if m['role'] == 'user')
        order = re.search(r'ORD-[A-Z0-9-]+', latest)
        if not order:
            if kw['messages'][-1]['role'] != 'tool':
                name, args = 'list_user_orders', {}
            else:
                name, args = 'ask_user', {'prompt': '请提供退款订单号', 'options': ['ORD-A', 'ORD-B']}
        elif kw['messages'][-1]['role'] == 'tool':
            name, args = 'apply_refund', {'order_id': order.group(), 'reason': '用户申请退款'}
        else:
            name, args = 'query_order', {'order_id': order.group()}
        call = SimpleNamespace(id='tool-call', function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))])
    client_model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=model)))
    monkeypatch.setattr(main, '_get_openai_client', lambda: client_model)
    monkeypatch.setattr(main, '_order_locate_execute', lambda user: None)
    monkeypatch.setattr(AgentEngine, '_workflow_reply_generator', lambda *a: None)
    monkeypatch.setattr(query_rewrite, 'rewrite_query', lambda message, **kw: RewriteOutcome(message=message))
    monkeypatch.setattr(app.intent, 'recognize', lambda *a, **kw: Intent.GREETINGS)
    monkeypatch.setitem(registry._TOOL_MAP, 'query_order', lambda order_id: {'success': True, 'order': {
        'order_id': order_id, 'status': 'pending', 'items': [], 'tracking_number': None,
    }})
    monkeypatch.setitem(registry._TOOL_MAP, 'list_user_orders', lambda **kw: {'success': True, 'orders': [
        {'order_id': 'ORD-A', 'items_summary': '耳机', 'status': 'pending'},
        {'order_id': 'ORD-B', 'items_summary': '运动鞋', 'status': 'pending'},
    ]})
    effects = []
    def refund(**kw):
        effects.append(kw)
        return {'success': True, 'message': '退款已提交'}
    monkeypatch.setitem(registry._TOOL_MAP, 'apply_refund', refund)
    client = TestClient(main.app)
    def headers(user='user'):
        token = jwt.encode({'sub': user, 'exp': datetime.now(timezone.utc) + timedelta(minutes=10)}, settings.api_jwt_secret, algorithm='HS256')
        return {'Authorization': 'Bearer ' + token}
    sid = str(uuid4())
    def send(message, **extra):
        return client.post('/api/v1/chat', headers=headers(), json={'message': message, 'session_id': sid, 'request_id': str(uuid4()), **extra})
    def tasks():
        return client.get(f'/api/v1/sessions/{sid}/tasks', headers=headers()).json()['tasks']
    yield client, headers, sid, send, tasks, effects
    client.close()


def test_refund_clarification_resumes_and_requires_explicit_confirmation(api):
    client, headers, sid, send, tasks, effects = api
    first = send('我要退款')
    assert first.status_code == 200
    task = tasks()[0]
    assert task['status'] == 'waiting_input'
    answer = send('ORD-A', task_action='answer', task_id=task['task_id'], task_version=task['version'], question_id=task['question']['question_id'])
    assert answer.status_code == 200, answer.text
    assert answer.json()['confirmation_token']
    assert effects == []
    assert tasks()[0]['task_id'] == task['task_id']
    assert tasks()[0]['status'] == 'waiting_confirmation'
    result = send('确认', confirmation_token=answer.json()['confirmation_token'])
    assert result.status_code == 200, result.text
    assert len(effects) == 1 and effects[0]['order_id'] == 'ORD-A'
    assert tasks()[0]['status'] == 'completed'


def test_order_candidates_and_change_of_mind_do_not_get_stuck(api):
    _, _, _, send, tasks, _ = api
    assert send('查物流').status_code == 200
    task = tasks()[0]
    assert task['question']['options'][0]['value'] == 'ORD-A'
    assert send('不查物流了，改为退款').status_code == 200
    current = tasks()
    assert current[0]['status'] == 'cancelled'
    assert current[1]['intent'] == 'after_sale_request'
    assert send('ORD-B').status_code == 200
    assert tasks()[1]['status'] == 'waiting_confirmation'


def test_old_confirmation_token_cannot_execute_new_task(api):
    _, _, _, send, tasks, effects = api
    token = send('我要退款 ORD-A').json()['confirmation_token']
    assert send('不退款了，查物流 ORD-B').status_code == 200
    response = send('确认', confirmation_token=token)
    assert response.status_code == 409
    assert effects == []


def test_multitask_retention_and_resume_survive_client_reload(api):
    client, headers, sid, send, tasks, _ = api
    assert send('我要退款，查物流，还要开发票').status_code == 200
    assert len(tasks()) == 3
    task = tasks()[1]
    response = send('继续物流', task_action='resume', task_id=task['task_id'], task_version=task['version'])
    assert response.status_code == 200, response.text
    reloaded = client.get(f'/api/v1/sessions/{sid}/tasks', headers=headers()).json()['tasks']
    assert [t['status'] for t in reloaded] == ['paused', 'waiting_input', 'queued']
    assert reloaded[0]['question']


def test_cancel_during_llm_drops_late_reply(api, monkeypatch):
    client, headers, sid, send, tasks, _ = api
    entered, released = threading.Event(), threading.Event()
    def slow(request, user):
        entered.set(); assert released.wait(5)
        return main.ChatResponse(session_id=sid, reply='late task answer', intent='logistics_inquiry', confidence=1, requires_human=False, follow_up_question=None)
    monkeypatch.setattr(main, '_handle_chat', slow)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(send, '查物流 ORD-A')
        assert entered.wait(5)
        assert send('取消').status_code == 200
        released.set()
        assert pending.result().json()['suppressed']
    assert tasks()[0]['status'] == 'cancelled'
    assert 'late task answer' not in client.get(f'/api/v1/sessions/{sid}/messages', headers=headers()).text


def test_api_rejects_stale_question_and_foreign_task(api):
    client, headers, sid, send, tasks, _ = api
    send('我要退款')
    task = tasks()[0]
    payload = dict(task_action='answer', task_id=task['task_id'], task_version=task['version'], question_id=task['question']['question_id'])
    assert send('ORD-A', **payload).status_code == 200
    assert send('ORD-B', **payload).status_code == 409
    assert client.get(f'/api/v1/sessions/{sid}/tasks', headers=headers('other')).status_code == 404


def test_classifier_adopts_business_task_and_react_asks_user(api, monkeypatch):
    from app.data_plan.enums import Intent
    import app.intent
    _, _, _, send, tasks, _ = api
    monkeypatch.setattr(app.intent, 'recognize', lambda *a, **kw: Intent.ISSUE_REPORT)
    tool_call = SimpleNamespace(id='ask', function=SimpleNamespace(name='ask_user', arguments=json.dumps({'prompt': '具体是哪种问题？', 'options': ['无法开机', '声音异常', 'ORD-INVENTED']})))
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))])
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    monkeypatch.setattr(main, '_get_openai_client', lambda: fake_client)
    result = send('耳机坏了')
    assert result.status_code == 200, result.text
    task = tasks()[0]
    assert task['intent'] == 'issue_report' and task['status'] == 'waiting_input', (result.json(), task)
    assert task['question']['options'][0]['label'] == '无法开机'
    assert len(task['question']['options']) == 2  # Unqueried order IDs cannot become selectable evidence.


def test_changed_question_request_id_replay_is_rejected(api):
    client, headers, sid, send, tasks, _ = api
    send('我要退款')
    task = tasks()[0]
    payload = {'message': 'ORD-A', 'session_id': sid, 'request_id': str(uuid4()), 'task_action': 'answer',
        'task_id': task['task_id'], 'task_version': task['version'], 'question_id': task['question']['question_id']}
    first = client.post('/api/v1/chat', headers=headers(), json=payload)
    replay = client.post('/api/v1/chat', headers=headers(), json=payload)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert client.post('/api/v1/chat', headers=headers(), json={**payload, 'task_version': task['version'] + 1}).status_code == 409
