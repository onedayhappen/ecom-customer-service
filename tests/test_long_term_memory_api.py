"""Authenticated API -> durable extraction -> a new chat's bounded memory."""
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from app.agents.engine import AgentEngine
from app.api import main
from app.config.settings import settings
from app.execution.types import ExecutionResult
from app.memory.long_term import MemoryFact
from app.memory.runtime import build_memory_manager, process_memory_jobs
from app.session.request_budget import apply_request_budget
from app.support.runtime import get_store
from app.tools import memory_tool
from app.tools.request_context import get_current_user_id, set_current_user_id, reset_current_user_id
from tests.test_multiturn_tasks_api import api  # noqa: F401


@pytest.fixture
def memory_api(api, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'memory_enabled', True)
    monkeypatch.setattr(settings, 'memory_storage_backend', 'json')
    monkeypatch.setattr(settings, 'memory_dir', str(tmp_path / 'memory'))
    monkeypatch.setattr(settings, 'memory_redis_url', '')
    monkeypatch.setattr(settings, 'memory_retrieval_enabled', False)
    monkeypatch.setattr(settings, 'memory_retrieval_min_score', 0.15)
    monkeypatch.setattr(settings, 'memory_extraction_debounce_seconds', 0)
    calls = []
    def execute(engine, text='', **kwargs):
        calls.append({'text': text, 'messages': kwargs['messages'],
                      'recall': memory_tool.recall_user_memory('黑色耳机')})
        return ExecutionResult(intent=engine.intent, execution=engine.plan.execution, reply='已收到。')
    monkeypatch.setattr(AgentEngine, 'execute', execute)
    return api, calls


def fact(content='用户偏好黑色耳机', **kwargs):
    values = dict(content=content, category='preference',
                  created_at=datetime.now(timezone.utc).isoformat(),
                  source_type='user_explicit', source_span='我长期喜欢黑色耳机',
                  memory_key='preference:headphone_color', confidence=0.95,
                  source_session='source', source_event_id='source:1')
    values.update(kwargs)
    return MemoryFact(**values)


def test_chat_extracts_then_recalls_in_another_chat_for_the_same_user(memory_api, monkeypatch):
    api, calls = memory_api
    client, headers, sid, send, _, _ = api
    assert send('我长期喜欢黑色耳机').status_code == 200
    extracted = []
    def extract(messages, existing):
        extracted.extend(messages)
        return [fact()]
    result = process_memory_jobs(get_store(), extract_fn=extract)
    assert result['processed'] == 1 and result['facts'] == 1
    assert extracted[0]['content'] == '我长期喜欢黑色耳机'
    assert all(m['role'] == 'user' for m in extracted)

    import app.intent
    from app.intent import query_rewrite
    original_recognize, original_rewrite = app.intent.recognize, query_rewrite.rewrite_query
    routing = []
    def recognize(text, **kwargs):
        routing.extend(kwargs['messages'])
        return original_recognize(text, **kwargs)
    def rewrite(text, **kwargs):
        routing.extend(kwargs['session_context'])
        return original_rewrite(text, **kwargs)
    monkeypatch.setattr(app.intent, 'recognize', recognize)
    monkeypatch.setattr(query_rewrite, 'rewrite_query', rewrite)
    response = client.post('/api/v1/chat', headers=headers(), json={
        'message': '还记得我喜欢黑色耳机吗', 'session_id': str(uuid4()), 'request_id': str(uuid4())})
    assert response.status_code == 200, response.text
    memory = next(m for m in calls[-1]['messages'] if m.get('name') == 'long_term_memory')
    assert '用户偏好黑色耳机' in memory['content']
    assert sid in memory['content']
    assert '当前用户要求和任务状态优先' in memory['content']
    assert calls[-1]['recall']['long_term_facts'] == []
    assert calls[-1]['recall']['already_in_context']
    assert not any(m.get('name') == 'long_term_memory' for m in routing)

    response = client.post('/api/v1/chat', headers=headers('another-user'), json={
        'message': '还记得我喜欢黑色耳机吗', 'session_id': str(uuid4()), 'request_id': str(uuid4())})
    assert response.status_code == 200
    assert not any(m.get('name') == 'long_term_memory' for m in calls[-1]['messages'])
    assert calls[-1]['recall']['long_term_facts'] == []
    assert memory_tool._memory_manager.get() is None
    assert get_current_user_id() is None


def test_memory_load_failure_does_not_fail_chat(memory_api, monkeypatch):
    from app.memory import runtime
    api, calls = memory_api
    def fail(user):
        raise OSError('simulated unavailable storage')
    monkeypatch.setattr(runtime, 'build_memory_manager', fail)
    response = api[3]('还记得我的耳机偏好吗')
    assert response.status_code == 200
    assert calls[-1]['recall']['success'] is False
    assert memory_tool._memory_manager.get() is None


def test_chat_exception_resets_both_contexts(memory_api, monkeypatch):
    def fail(*args):
        assert get_current_user_id() == 'user'
        assert memory_tool._memory_manager.get().ltm.user_id == 'user'
        raise RuntimeError('execution failed')
    monkeypatch.setattr(main, '_handle_chat_inner_impl', fail)
    with pytest.raises(RuntimeError, match='execution failed'):
        main._handle_chat_inner(main.ChatRequest(message='你好', session_id=str(uuid4())), 'user', None)
    assert memory_tool._memory_manager.get() is None
    assert get_current_user_id() is None


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


def test_disabled_memory_does_not_enqueue_or_inject(memory_api, monkeypatch):
    api, calls = memory_api
    monkeypatch.setattr(settings, 'memory_enabled', False)
    assert api[3]('我长期喜欢黑色耳机').status_code == 200
    with get_store().transaction(read_only=True) as tx:
        assert tx.one('SELECT * FROM support_memory_jobs WHERE session_id=?', (api[2],)) is None
    assert not any(m.get('name') == 'long_term_memory' for m in calls[-1]['messages'])
    assert not calls[-1]['recall']['success']


def test_v2_production_extractor_persists_updates_and_revocation_across_chats(memory_api, monkeypatch):
    """Only the remote model is replaced; all runtime/schema/storage paths run."""
    import openai
    from types import SimpleNamespace
    api, calls = memory_api
    client, headers, sid, send, _, _ = api
    monkeypatch.setattr(settings, 'openai_api_key', 'offline-test-key')
    model_calls = []

    class Model:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create(self, **kwargs):
            payload = json.loads(kwargs['messages'][-1]['content'])
            model_calls.append(payload)
            candidates = []
            for index, message in enumerate(payload['messages']):
                text = message['content']
                if not (text.startswith('我长期喜欢') or text.startswith('撤销')):
                    continue
                candidates.append(dict(predicate='prefers_color', value=None if text.startswith('撤销') else
                    'white' if '白色' in text else 'black', qualifiers={'product_category': 'headphones'},
                    polarity='positive', operation='RETRACT' if text.startswith('撤销') else 'ASSERT',
                    source_index=index, source_quote=text, confidence=.8))
            result = json.dumps({'schema_version': 2, 'facts': candidates}, ensure_ascii=False)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=result, tool_calls=[]))],
                                   model='offline-model', usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    monkeypatch.setattr(openai, 'OpenAI', Model)
    for text, expected in [('我长期喜欢白色耳机', 'white'), ('我长期喜欢黑色耳机', 'black')]:
        assert send(text).status_code == 200
        result = process_memory_jobs(get_store())
        assert result['processed'] == 1 and result['failed'] == 0
        manager = build_memory_manager('user')
        assert [f.value for f in manager.ltm.facts] == [expected]
        current = manager.ltm.facts[0]
        assert current.schema_version == 2
        evidence = manager.ltm.store.list_evidence('user', current.memory_id)
        assert evidence[0]['source_session'] == sid and evidence[0]['start_offset'] == 0
    history = manager.ltm.history_facts()
    assert {f.value for f in history} == {'white', 'black'}
    response = client.post('/api/v1/chat', headers=headers(), json={
        'message': '按我的偏好推荐耳机', 'session_id': str(uuid4()), 'request_id': str(uuid4())})
    assert response.status_code == 200
    section = next(m for m in calls[-1]['messages'] if m.get('name') == 'long_term_memory')
    records = json.loads(section['content'].split('\n', 1)[1])['facts']
    assert [item['value'] for item in records] == ['black']
    assert send('撤销我的全部耳机颜色偏好').status_code == 200
    assert process_memory_jobs(get_store())['failed'] == 0
    manager = build_memory_manager('user')
    assert manager.ltm.facts == []
    assert manager.retrieve_relevant_long_term_memory('耳机颜色') == []
    assert len(manager.ltm.history_facts()) == 3
    assert model_calls


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
