from uuid import uuid4

from app.config.settings import settings
from app.support.runtime import get_store
from app.tasks.intake import MessageIntake
from tests.test_multiturn_tasks_api import api


def test_intake_is_off_by_default(api, monkeypatch):
    client, headers, sid, _, _, effects = api
    monkeypatch.setattr(settings, "task_intake_enabled", False)
    response = client.post("/api/v1/task-messages", headers=headers(), json={
        "session_id": sid, "request_id": str(uuid4()), "message": "退货"})
    assert response.status_code == 404
    assert effects == []


def test_authenticated_api_accepts_while_processing_without_running_tools(api, monkeypatch):
    client, headers, sid, old_send, _, effects = api
    monkeypatch.setattr(settings, "task_intake_enabled", True)
    body = {"session_id": sid, "request_id": str(uuid4()), "message": "退款"}
    assert client.post("/api/v1/task-messages", json=body).status_code == 401
    first = client.post("/api/v1/task-messages", headers=headers(), json=body)
    assert first.status_code == 202
    intake = MessageIntake(get_store())
    batch = intake.claim_next()
    second = client.post("/api/v1/task-messages", headers=headers(), json={
        **body, "request_id": str(uuid4()), "message": "左耳没声音"})
    assert second.status_code == 202 and batch is not None
    assert client.post("/api/v1/task-messages", headers=headers(), json=body).json() == first.json()
    assert client.post("/api/v1/task-messages", headers=headers(), json={**body, "user_id": "other"}).status_code == 422
    assert client.get(f"/api/v1/task-messages/{sid}", headers=headers("other")).status_code == 404
    state = client.get(f"/api/v1/task-messages/{sid}", headers=headers()).json()
    assert state["associated_sequence"] == 0 and len(state["messages"]) == 2
    assert old_send("测试旧入口").status_code == 409
    assert effects == []


def test_database_failure_does_not_return_accepted(api, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.main import app
    client, headers, sid, _, _, _ = api
    monkeypatch.setattr(settings, "task_intake_enabled", True)
    store = get_store()
    store.ensure(sid, "user")
    def fail(*args, **kwargs):
        raise RuntimeError("database write unavailable")
    monkeypatch.setattr(store, "_event", fail)
    failing_client = TestClient(app, raise_server_exceptions=False)
    try:
        response = failing_client.post("/api/v1/task-messages", headers=headers(), json={
            "session_id": sid, "request_id": str(uuid4()), "message": "测试"})
        assert response.status_code == 500
    finally:
        failing_client.close()
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM message_intake_items")["n"] == 0
