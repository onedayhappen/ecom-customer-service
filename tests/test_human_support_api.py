# 文件说明：测试人工服务接口鉴权、控制权流转及客户与客服数据隔离。
"""End-to-end API authorization and bot/human routing with isolated storage."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import threading
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.config.settings import settings
from app.support.runtime import get_store


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "support_database_url", f"sqlite:///{tmp_path / 'support.db'}")
    monkeypatch.setattr(settings, "api_jwt_secret", "isolated-test-key-at-least-32-characters")
    from app.runtime.concurrency import DistributedLease
    get_store().seed_staff("agent", "agent")
    get_store().seed_staff("admin", "admin")
    client = TestClient(main.app)
    def headers(user="customer"):
        return {"Authorization": "Bearer " + jwt.encode({"sub": user, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, settings.api_jwt_secret, algorithm="HS256")}
    yield client, headers
    client.close()


def test_full_human_lifecycle_and_private_note(api, monkeypatch):
    client, headers = api
    sid = str(uuid4())
    response = client.post('/api/v1/task-messages', headers=headers(), json={"message": "我要人工客服", "session_id": sid, "request_id": str(uuid4())})
    assert response.status_code == 202
    tid = get_store().state(sid, "customer")["ticket"]["ticket_id"]
    assert client.get('/api/v1/support/tickets', headers=headers()).status_code == 403
    assert client.get(f'/api/v1/support/tickets/{tid}', headers=headers("agent")).status_code == 403
    assert client.post(f'/api/v1/support/tickets/{tid}/transition', headers=headers("agent"), json={"action": "claim", "version": 1}).status_code == 200
    for private, text in ((True, "内部核实说明"), (False, "您好，我来为您处理")):
        r = client.post(f'/api/v1/support/tickets/{tid}/messages', headers=headers("agent"), json={"content": text, "internal": private, "client_message_id": str(uuid4())})
        assert r.status_code == 200
    public = client.get(f'/api/v1/sessions/{sid}/messages', headers=headers())
    assert "内部核实说明" not in public.text
    assert "您好，我来为您处理" in public.text
    assert client.get(f'/api/v1/sessions/{sid}/messages?staff=true', headers=headers()).status_code == 403
    assert client.get(f'/api/v1/sessions/{sid}/messages', headers=headers("intruder")).status_code == 404
    assert client.post(f'/api/v1/sessions/{sid}/resume-bot', headers=headers()).json()["mode"] == "bot"
    assert client.post(f'/api/v1/support/tickets/{tid}/messages', headers=headers("agent"), json={"content": "late", "client_message_id": str(uuid4())}).status_code in {403, 409}


def test_existing_session_cannot_be_claimed_by_another_user(api):
    client, headers = api
    sid = str(uuid4())
    get_store().ensure(sid, "owner", [{"role": "user", "content": "private"}])
    assert client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers("intruder")).status_code == 404
    assert client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers("owner")).status_code == 200


def test_reset_preserves_ticket_history_and_roles_are_not_client_controlled(api):
    client, headers = api
    sid = str(uuid4())
    client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers())
    assert client.post(f'/api/v1/sessions/{sid}/reset', headers=headers()).status_code == 409
    assert client.post('/api/v1/support/staff/customer', headers=headers(), json={"role": "admin"}).status_code == 403


def test_unauthorized_support_response_has_cors_headers(api):
    client, _ = api
    response = client.get('/api/v1/support/me', headers={'Origin': 'http://localhost:5173'})
    assert response.status_code == 401
    assert response.headers['access-control-allow-origin'] == 'http://localhost:5173'
    reconnect = client.options('/api/v1/sessions/unused/support-events', headers={
        'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'Last-Event-ID',
    })
    assert reconnect.status_code == 200
