# 文件说明：测试图片上传、发送、预览、重试与撤回接口及权限限制。
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.support.runtime import get_store
from tests.api_fixtures import api
from tests.test_image_context import png


@pytest.fixture(autouse=True)
def image_config(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'media_storage_dir', str(tmp_path / 'media'))
    monkeypatch.setattr(settings, 'api_auth_backend', 'env')


def test_upload_send_preview_retry_and_revoke(api):
    client, headers, sid, *_ = api
    data = dict(session_id=sid, upload_request_id=str(uuid4()))
    upload = client.post('/api/v1/attachments', headers=headers(), data=data,
                         files={'file': ('wrong.exe', png(), 'application/octet-stream')})
    assert upload.status_code == 201, upload.text
    aid = upload.json()['attachment_id']
    path = f'/api/v1/attachments/{aid}'
    assert client.get(path + '/content').status_code == 401
    assert client.get(path, headers=headers('other')).status_code == 404
    preview = client.get(path + '/content', headers=headers())
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/jpeg'
    assert preview.headers['cache-control'] == 'private, no-store'
    assert client.post('/api/v1/attachments', headers=headers(), data=data,
        files={'file': ('image.png', png(), 'image/png')}).json()['attachment_id'] == aid
    sent = client.post('/api/v1/task-messages', headers=headers(), json={
        'session_id': sid, 'request_id': str(uuid4()), 'attachment_ids': [aid]})
    assert sent.status_code == 202, sent.text
    retry = dict(request_id=str(uuid4()), session_id=sid, source_sequence=sent.json()['sequence'])
    assert client.post(path + '/analysis-retries', headers=headers(), json=retry).status_code == 202
    assert client.post(path + '/analysis-retries', headers=headers('other'), json=retry).status_code == 404
    assert client.delete(path, headers=headers()).status_code == 204
    assert client.get(path + '/content', headers=headers()).status_code == 410
    assert client.post(path + '/analysis-retries', headers=headers(), json={**retry, 'request_id': str(uuid4())}).status_code in {404, 410}


def test_empty_forged_and_oversize_inputs_are_rejected(api, monkeypatch):
    client, headers, sid, *_ = api
    assert client.post('/api/v1/task-messages', headers=headers(), json={
        'session_id': sid, 'request_id': str(uuid4()), 'message': '  '}).status_code == 422
    data = dict(session_id=sid, upload_request_id=str(uuid4()))
    assert client.post('/api/v1/attachments', headers=headers(), data=data,
        files={'file': ('image.png', b'<script>bad</script>', 'image/png')}).status_code == 422
    monkeypatch.setattr(settings, 'media_max_bytes', 20)
    assert client.post('/api/v1/attachments', headers=headers(), data=data,
        files={'file': ('image.png', png(), 'image/png')}).status_code == 413


def test_staff_access_requires_ticket_and_sent_image(api):
    client, headers, sid, *_ = api
    store = get_store()
    store.ensure(sid, 'user')
    with store.transaction() as tx:
        tx.execute("INSERT INTO support_staff VALUES ('agent','agent',1)")
    upload = client.post('/api/v1/attachments', headers=headers(),
        data=dict(session_id=sid, upload_request_id=str(uuid4())), files={'file': ('a.png', png(), 'image/png')})
    aid = upload.json()['attachment_id']
    handoff = client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers())
    assert handoff.status_code == 200, handoff.text
    with store.transaction(read_only=True) as tx:
        ticket = tx.one('SELECT ticket_id FROM support_conversations WHERE session_id=?', (sid,))['ticket_id']
    store.change(ticket, 'agent', 1, 'claim')
    path = f'/api/v1/attachments/{aid}/content?ticket_id={ticket}'
    assert client.get(path, headers=headers('other')).status_code in {403, 404}
    assert client.get(path, headers=headers('agent')).status_code == 404  # uploaded, not sent
    assert client.post('/api/v1/task-messages', headers=headers(), json={
        'session_id': sid, 'request_id': str(uuid4()), 'attachment_ids': [aid]}).status_code == 202
    assert client.get(path, headers=headers('agent')).status_code == 200
