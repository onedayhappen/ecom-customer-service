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
from app.session import repository
from app.support.runtime import get_store


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "support_database_url", f"sqlite:///{tmp_path / 'support.db'}")
    monkeypatch.setattr(settings, "api_session_storage_backend", "json")
    monkeypatch.setattr(repository, "JSON_SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(settings, "api_chat_execution_mode", "sync")
    monkeypatch.setattr(settings, "api_jwt_secret", "isolated-test-key-at-least-32-characters")
    from app.runtime.concurrency import DistributedLease
    monkeypatch.setattr(main, "_llm_lease", DistributedLease("", "test", 4))
    get_store().seed_staff("agent", "agent")
    get_store().seed_staff("admin", "admin")
    client = TestClient(main.app)
    def headers(user="customer"):
        return {"Authorization": "Bearer " + jwt.encode({"sub": user, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, settings.api_jwt_secret, algorithm="HS256")}
    yield client, headers
    client.close()


def test_full_human_lifecycle_and_private_note(api, monkeypatch):
    client, headers = api
    monkeypatch.setattr(main, "_handle_chat", lambda *a: pytest.fail("Human message must not call AI"))
    sid = str(uuid4())
    response = client.post('/api/v1/chat', headers=headers(), json={"message": "我要人工客服", "session_id": sid, "request_id": str(uuid4())})
    assert response.status_code == 200
    tid = response.json()["support"]["ticket"]["ticket_id"]
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


def test_takeover_suppresses_inflight_ai_response(api, monkeypatch):
    client, headers = api
    started, finish = threading.Event(), threading.Event()
    sid = str(uuid4())
    def slow(request, user):
        started.set(); assert finish.wait(5)
        return main.ChatResponse(session_id=sid, reply="stale AI", intent="greetings", confidence=1, requires_human=False, follow_up_question=None)
    monkeypatch.setattr(main, "_handle_chat", slow)
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(client.post, '/api/v1/chat', headers=headers(), json={"message": "hello", "session_id": sid})
        assert started.wait(5)
        assert client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers()).status_code == 200
        finish.set()
        response = task.result()
    assert response.status_code == 200
    assert response.json()["suppressed"] is True
    assert "stale AI" not in client.get(f'/api/v1/sessions/{sid}/messages', headers=headers()).text


def test_bot_publication_and_request_replay(api, monkeypatch):
    client, headers = api
    calls = []
    def answer(request, user):
        calls.append(user)
        return main.ChatResponse(session_id=request.session_id, reply="hello", intent="greetings", confidence=1, requires_human=False, follow_up_question=None)
    monkeypatch.setattr(main, "_handle_chat", answer)
    body = {"message": "hi", "session_id": str(uuid4()), "request_id": str(uuid4())}
    a = client.post('/api/v1/chat', headers=headers(), json=body)
    b = client.post('/api/v1/chat', headers=headers(), json=body)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert len(calls) == 1
    changed = client.post('/api/v1/chat', headers=headers(), json={**body, "message": "changed"})
    assert changed.status_code == 409


def test_automatic_escalation_creates_actual_ticket(api, monkeypatch):
    client, headers = api
    monkeypatch.setattr(main, "_handle_chat", lambda r, u: main.ChatResponse(session_id=r.session_id, reply="needs help", intent="complaint", confidence=1, requires_human=True, follow_up_question=None))
    r = client.post('/api/v1/chat', headers=headers(), json={"message": "something broken", "session_id": str(uuid4())})
    assert r.status_code == 200
    assert r.json()["support"]["ticket"]["status"] == "queued"


def test_existing_session_cannot_be_claimed_by_another_user(api):
    client, headers = api
    sid = str(uuid4())
    repository.save_session(sid, [{"role": "user", "content": "private"}], None, {"owner_user_id": "owner"}, "json", "", expected_state_version=0)
    assert client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers("intruder")).status_code == 404
    assert client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers("owner")).status_code == 200


def test_reset_preserves_ticket_history_and_roles_are_not_client_controlled(api):
    client, headers = api
    sid = str(uuid4())
    client.post(f'/api/v1/sessions/{sid}/handoff', headers=headers())
    assert client.post(f'/api/v1/sessions/{sid}/reset', headers=headers()).status_code == 409
    assert client.post('/api/v1/support/staff/customer', headers=headers(), json={"role": "admin"}).status_code == 403


def test_old_confirmation_binding_invalidated_by_takeover(api, tmp_path):
    from app.memory.case_manager import CaseManager
    from app.reliability.mutations import MutationGuard
    from app.support.runtime import bot_scope
    sid = str(uuid4()); store = get_store(); store.ensure(sid, "customer")
    guard = MutationGuard(CaseManager("customer", str(tmp_path / "cases")), sid)
    with bot_scope(store, sid, "customer", 0):
        token = guard.request_confirmation("after_sale_request", "apply_refund", {"order_id": "O1", "reason": "test"})
        assert guard.peek_confirmation(token)
    store.handoff(sid, "customer"); state = store.resume(sid, "customer")
    with bot_scope(store, sid, "customer", state["control_version"]):
        assert guard.peek_confirmation(token) is None


def test_concurrent_same_request_does_not_run_twice(api, monkeypatch):
    client, headers = api
    started, finish = threading.Event(), threading.Event()
    calls = []
    def slow(request, user):
        calls.append(user); started.set(); assert finish.wait(5)
        return main.ChatResponse(session_id=request.session_id, reply="done", intent="greetings", confidence=1, requires_human=False, follow_up_question=None)
    monkeypatch.setattr(main, "_handle_chat", slow)
    body = {"message": "hello", "session_id": str(uuid4()), "request_id": str(uuid4())}
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.post, '/api/v1/chat', headers=headers(), json=body)
        assert started.wait(5)
        assert client.post('/api/v1/chat', headers=headers(), json=body).status_code == 409
        # The rejected replay must not clear the original execution reservation.
        assert client.post('/api/v1/chat', headers=headers(), json={**body, "request_id": str(uuid4())}).status_code == 409
        finish.set()
        assert future.result().status_code == 200
    assert len(calls) == 1


def test_legacy_queued_job_cannot_execute_after_takeover(api, monkeypatch):
    from app.runtime.chat_queue import ChatJob
    sid = str(uuid4())
    get_store().ensure(sid, "customer")
    get_store().handoff(sid, "customer")
    get_store().resume(sid, "customer")
    monkeypatch.setattr(main, "_handle_chat", lambda *a: pytest.fail("stale legacy job must not run"))
    result = main._run_chat_job(ChatJob("legacy-job", "customer", sid, "stale"))
    assert result["suppressed"] is True


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
