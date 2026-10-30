# 文件说明：测试操作接口的鉴权、固定参数、回执重读及取消控制。
from app.config.settings import settings
from app.support.runtime import get_store
from app.tasks.operations import TaskOperations
from tests.api_fixtures import api
from tests.test_task_candidates import task
from tests.test_task_operations import Business


def setup(api, monkeypatch):
    client, headers, sid, _, _, _ = api
    monkeypatch.setattr(settings, "task_intake_enabled", True)
    monkeypatch.setattr(settings, "task_operations_enabled", True)
    business = Business()
    monkeypatch.setattr("app.tasks.operations.backend", lambda: business)
    store = get_store()
    store.ensure(sid, "user")
    record, _ = task(store, sid, user="user", details=dict(order_id="ORD-A", reason="不想要了"))
    TaskOperations(store, "user").prepare(sid, record.task_id, 1, "apply_refund")
    response = client.get(f"/api/v1/task-operations/{sid}", headers=headers())
    assert response.headers["Cache-Control"] == "no-store"
    card = response.json()[0]
    return client, headers, sid, card, business


def test_authenticated_fixed_parameters_and_receipt_replay(api, monkeypatch):
    client, headers, sid, card, business = setup(api, monkeypatch)
    path = f'/api/v1/task-operations/{card["operation_id"]}/confirm'
    body = dict(session_id=sid, confirmation_token=card["confirmation_token"])
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers=headers("other"), json=body).status_code == 404
    assert client.post(path, headers=headers(), json={**body, "order_id": "ORD-B"}).status_code == 422
    assert client.post(path, headers=headers(), json={**body, "confirmed": True}).status_code == 422
    first = client.post(path, headers=headers(), json=body)
    assert first.status_code == 200 and first.json()["status"] == "succeeded", first.text
    assert client.post(path, headers=headers(), json=body).json() == first.json()
    assert len(business.writes) == 1


def test_unprocessed_cancel_blocks_confirmation_and_cancel_card_still_works(api, monkeypatch):
    from uuid import uuid4
    client, headers, sid, card, business = setup(api, monkeypatch)
    assert client.post('/api/v1/task-messages', headers=headers(), json=dict(
        session_id=sid, request_id=str(uuid4()), message="先别退")).status_code == 202
    path = f'/api/v1/task-operations/{card["operation_id"]}'
    assert client.post(path + '/confirm', headers=headers(), json=dict(
        session_id=sid, confirmation_token=card["confirmation_token"])).status_code == 409
    assert client.post(path + '/cancel', headers=headers(), json=dict(session_id=sid)).json()["status"] == "cancelled"
    assert business.writes == []


def test_default_off_api_exposes_no_write(api, monkeypatch):
    client, headers, sid, card, business = setup(api, monkeypatch)
    monkeypatch.setattr(settings, "task_operations_enabled", False)
    assert client.get(f"/api/v1/task-operations/{sid}", headers=headers()).status_code == 404
    assert client.post(f'/api/v1/task-operations/{card["operation_id"]}/confirm', headers=headers(), json=dict(
        session_id=sid, confirmation_token=card["confirmation_token"])).status_code == 404
    assert business.writes == []
