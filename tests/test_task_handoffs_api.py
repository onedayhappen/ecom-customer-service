from uuid import uuid4

from app.config.settings import settings
from app.support.runtime import get_store
from app.tasks.repository import TaskRepository
from tests.test_human_support_api import api
from tests.test_task_candidates import task


def test_authenticated_handover_update_and_explicit_customer_resume(api, monkeypatch):
    client, headers = api
    monkeypatch.setattr(settings, 'task_intake_enabled', True)
    sid = str(uuid4())
    store = get_store()
    store.ensure(sid, 'customer')
    record, _ = task(store, sid, user='customer')
    tid = client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers()).json()['ticket']['ticket_id']
    endpoint = f'/api/v1/support/tickets/{tid}/tasks/{record.task_id}'
    body = dict(request_id=str(uuid4()), expected_version=1, details={'reason': '左耳没有声音'})
    assert client.post(endpoint, json=body).status_code == 401
    assert client.post(endpoint, headers=headers(), json=body).status_code == 403
    assert client.post(endpoint, headers=headers('agent'), json=body).status_code == 403
    assert client.post(f'/api/v1/support/tickets/{tid}/transition', headers=headers('agent'),
        json={'action': 'claim', 'version': 1}).status_code == 200
    detail = client.get(f'/api/v1/support/tickets/{tid}', headers=headers('agent')).json()
    assert detail['task_handovers'][0]['handover']['task']['task_id'] == record.task_id
    assert client.post(endpoint, headers=headers('agent'), json={**body, 'user_id': 'other'}).status_code == 422
    assert client.post(endpoint, headers=headers('agent'), json={**body, 'details': {'authorized': 'yes'}}).status_code == 422
    saved = client.post(endpoint, headers=headers('agent'), json=body)
    assert saved.status_code == 200 and saved.json()['task']['details']['reason'] == '左耳没有声音'
    assert client.post(endpoint, headers=headers('agent'), json=body).json() == saved.json()
    assert client.post(f'/api/v1/support/tickets/{tid}/messages', headers=headers('agent'),
        json={'client_message_id': str(uuid4()), 'content': '内部备注不可外传', 'internal': True}).status_code == 200
    assert '内部备注不可外传' not in client.get(f'/api/v1/sessions/{sid}/transcript', headers=headers()).text
    assert client.post(f'/api/v1/support/tickets/{tid}/transition', headers=headers('agent'),
        json={'action': 'resolved', 'version': detail['ticket']['version']}).status_code == 200
    assert client.get(f'/api/v1/sessions/{sid}/support-state', headers=headers()).json()['mode'] == 'human'
    assert client.post(f'/api/v1/sessions/{sid}/resume-bot', headers=headers('agent')).status_code == 404
    assert client.post(f'/api/v1/sessions/{sid}/resume-bot', headers=headers()).json()['mode'] == 'bot'
    current = TaskRepository(store, 'customer').get(record.task_id)
    assert current.version == saved.json()['task']['version'] + 1
    assert client.post(endpoint, headers=headers('agent'), json=body).status_code in {403, 409}
