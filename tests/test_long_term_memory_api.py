# 文件说明：测试鉴权消息经持久提取后，在新会话按需读取长期记忆。
"""Authenticated API -> durable extraction -> a new chat's bounded memory."""
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from app.api import main
from app.config.settings import settings
from app.execution.types import ExecutionResult
from app.memory.long_term import MemoryFact
from app.memory.runtime import build_memory_manager, process_memory_jobs
from app.session.request_budget import apply_request_budget
from app.support.runtime import get_store
from app.memory import recall as memory_tool
from app.tools.request_context import get_current_user_id, set_current_user_id, reset_current_user_id
from tests.api_fixtures import api  # noqa: F401


@pytest.fixture
def memory_api(api, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'memory_engine', 'legacy')  # v1/v2 facade compatibility.
    monkeypatch.setattr(settings, 'memory_enabled', True)
    monkeypatch.setattr(settings, 'memory_storage_backend', 'json')
    monkeypatch.setattr(settings, 'memory_dir', str(tmp_path / 'memory'))
    monkeypatch.setattr(settings, 'memory_redis_url', '')
    monkeypatch.setattr(settings, 'memory_retrieval_enabled', False)
    monkeypatch.setattr(settings, 'memory_retrieval_min_score', 0.15)
    monkeypatch.setattr(settings, 'memory_extraction_debounce_seconds', 0)
    return api, []


def fact(content='用户偏好黑色耳机', **kwargs):
    values = dict(content=content, category='preference',
                  created_at=datetime.now(timezone.utc).isoformat(),
                  source_type='user_explicit', source_span='我长期喜欢黑色耳机',
                  memory_key='preference:headphone_color', confidence=0.95,
                  source_session='source', source_event_id='source:1')
    values.update(kwargs)
    return MemoryFact(**values)


def test_memory_tool_rejects_missing_and_mismatched_identity(memory_api):
    manager = build_memory_manager('user')
    manager.ltm.facts = [fact()]
    token = memory_tool.set_memory_manager(manager)
    try:
        assert not memory_tool.recall_user_memory('黑色耳机')['success']
        user_token = set_current_user_id('other')
        try:
            assert not memory_tool.recall_user_memory('黑色耳机')['success']
            assert memory_tool.memory_prompt_sections('记得黑色耳机吗') == []
        finally:
            reset_current_user_id(user_token)
    finally:
        memory_tool.reset_memory_manager(token)


def test_manager_filters_business_scope_unproven_sources_and_agent_permissions(memory_api):
    manager = build_memory_manager('user')
    manager.ltm.semantic_retrieval_top_k = 20
    manager.ltm.facts = [fact(), fact('耳机 SECRET-CASE', scope='case:case-1'),
        fact('耳机 SECRET-ISSUE', category='issue'),
        fact('耳机 SECRET-AGENT', allowed_agents=['another_agent']),
        fact('耳机 SECRET-LEGACY', source_event_id=''),
        fact('耳机 SECRET-INFERRED', source_type='llm_inferred')]
    facts = manager.retrieve_relevant_long_term_memory('耳机')
    assert [f.content for f in facts] == ['用户偏好黑色耳机']
    assert 'SECRET' not in json.dumps(manager.build_memory_prompt_sections('记得耳机吗'), ensure_ascii=False)


def test_long_term_memory_is_optional_under_final_request_budget(monkeypatch):
    monkeypatch.setattr(settings, 'context_request_budget_enabled', True)
    monkeypatch.setattr(settings, 'context_request_input_tokens', 512)
    monkeypatch.setattr(settings, 'context_model_window_tokens', 0)
    request = {'messages': [{'role': 'system', 'content': '固定规则'},
        {'role': 'system', 'name': 'long_term_memory', 'content': '历史偏好' * 2000},
        {'role': 'user', 'content': '这次我要白色耳机'}]}
    result = apply_request_budget(request)
    assert len(result['messages']) == 2
    assert result['messages'][-1] == request['messages'][-1]
    assert len(request['messages']) == 3


def test_final_wire_budget_restores_recall_when_automatic_block_is_removed(memory_api, monkeypatch):
    from app.observability.llm_client import LlmClient
    monkeypatch.setattr(settings, 'context_request_budget_enabled', True)
    monkeypatch.setattr(settings, 'context_request_input_tokens', 512)
    monkeypatch.setattr(settings, 'context_model_window_tokens', 0)
    manager = build_memory_manager('user')
    value = fact(memory_id='visible-fact')
    manager.ltm.facts = [value]
    manager_token = memory_tool.set_memory_manager(manager)
    user_token = set_current_user_id('user')
    try:
        sections = memory_tool.memory_prompt_sections('记得黑色耳机吗')
        assert manager.injected_ids == {'visible-fact'}
        assert memory_tool.recall_user_memory('耳机')['long_term_facts'] == []
        # Enlarge a valid optional JSON block to exercise the final wire guard.
        note, payload = sections[0]['content'].split('\n', 1)
        payload = json.loads(payload)
        payload['padding'] = '有来源的历史背景' * 1000
        sections[0]['content'] = note + '\n' + json.dumps(payload, ensure_ascii=False)
        request = {'messages': [*sections, {'role': 'user', 'content': '推荐耳机'}]}
        bounded = LlmClient._prepare_request(request, 'react', 'test-model', {})
        assert not any(m.get('name') == 'long_term_memory' for m in bounded['messages'])
        assert manager.injected_ids == set()
        assert memory_tool.recall_user_memory('耳机')['long_term_facts'][0]['memory_id'] == 'visible-fact'
    finally:
        reset_current_user_id(user_token)
        memory_tool.reset_memory_manager(manager_token)
