"""Handover lifecycle through real API processes and isolated PostgreSQL."""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.tasks.association import AssociationService
from app.tasks.intake import MessageIntake
from app.tasks.repository import TaskRepository
from tests.test_platform_http import platform
from tests.test_message_association import new, proposal

pytestmark = pytest.mark.skipif(os.getenv('ECOM_INTEGRATION') != '1', reason='isolated real infrastructure required')


def test_handover_update_and_resume_across_api_instances(platform):
    _, url = platform['start']('api', TASK_INTAKE_ENABLED='true')
    sid = str(uuid4())
    client, headers, store = platform['client'], platform['headers'], platform['store']
    accepted = client.post(url + '/api/v1/task-messages', headers=headers(), json={
        'session_id': sid, 'request_id': str(uuid4()), 'message': 'ORD-A 耳机左耳没有声音，申请退货'})
    assert accepted.status_code == 202
    batch = MessageIntake(store).claim(sid, 'customer')
    AssociationService(store, 'customer').process(batch, SimpleNamespace(associate=lambda _: proposal(
        new(batch, goal='耳机退货', details={'order_id': 'ORD-A'}))))
    record = TaskRepository(store, 'customer').list(session_id=sid)[0]
    tid = client.post(url + f'/api/v1/sessions/{sid}/handoff', headers=headers()).json()['ticket']['ticket_id']
    peer = platform['urls'][1]
    assert client.post(peer + f'/api/v1/support/tickets/{tid}/transition', headers=headers('staff'),
        json={'action': 'claim', 'version': 1}).status_code == 200
    endpoint = url + f'/api/v1/support/tickets/{tid}/tasks/{record.task_id}'
    body = {'request_id': str(uuid4()), 'expected_version': record.version, 'details': {'reason': '左耳没有声音'}}
    first = client.post(endpoint, headers=headers('staff'), json=body)
    assert first.status_code == 200, first.text
    again = client.post(peer + f'/api/v1/support/tickets/{tid}/tasks/{record.task_id}', headers=headers('staff'), json=body)
    assert again.json() == first.json()
    private = client.post(peer + f'/api/v1/support/tickets/{tid}/messages', headers=headers('staff'), json={
        'client_message_id': str(uuid4()), 'content': '内部备注不可交给机器人', 'internal': True})
    assert private.status_code == 200
    assert '内部备注不可交给机器人' not in client.get(url + f'/api/v1/sessions/{sid}/transcript', headers=headers()).text
    ticket = client.get(peer + f'/api/v1/support/tickets/{tid}', headers=headers('staff')).json()['ticket']
    assert client.post(peer + f'/api/v1/support/tickets/{tid}/transition', headers=headers('staff'),
        json={'action': 'resolved', 'version': ticket['version']}).status_code == 200
    assert client.get(url + f'/api/v1/sessions/{sid}/support-state', headers=headers()).json()['mode'] == 'human'
    assert client.post(url + f'/api/v1/sessions/{sid}/resume-bot', headers=headers()).json()['mode'] == 'bot'
    current = TaskRepository(store, 'customer').get(record.task_id)
    assert current.version == first.json()['task']['version'] + 1 and current.details['reason'] == '左耳没有声音'
    archived = client.get(peer + f'/api/v1/support/tickets/{tid}', headers=headers('staff')).json()
    assert archived['task_handovers'][0]['active'] is False
