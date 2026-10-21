"""Authenticated API fixture; no model or business write runs during admission."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.config.settings import settings


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'support_database_url', f"sqlite:///{tmp_path / 'tasks.db'}")
    monkeypatch.setattr(settings, 'memory_enabled', False)
    monkeypatch.setattr(settings, 'short_term_memory_enabled', False)
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')
    monkeypatch.setattr(settings, 'api_jwt_secret', 'task-tests-isolated-secret-at-least-32bytes')
    client = TestClient(main.app)
    def headers(user='user'):
        return {'Authorization': 'Bearer ' + jwt.encode(
            {'sub': user, 'exp': datetime.now(timezone.utc) + timedelta(minutes=10)}, settings.api_jwt_secret, algorithm='HS256')}
    sid = str(uuid4())
    def old_send(message, **extra):
        return client.post('/api/v1/chat', headers=headers(), json=dict(
            message=message, session_id=sid, request_id=str(uuid4()), **extra))
    def tasks():
        return client.get(f'/api/v1/sessions/{sid}/tasks', headers=headers()).json()['tasks']
    yield client, headers, sid, old_send, tasks, []
    client.close()
