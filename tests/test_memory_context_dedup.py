"""Source-aware prompt deduplication follows the actual post-budget context."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from app.config.settings import settings
from app.memory.long_term import MemoryFact
from app.memory.ltm_manager import LongTermMemoryManager
from app.tools import memory_tool
from app.tools.request_context import reset_current_user_id, set_current_user_id


QUERY = '记得我耳机颜色吗'
SPAN = '我长期喜欢黑色耳机'
SESSION = 'chat:source'


def _fact(**kwargs):
    timestamp = datetime.now(timezone.utc).isoformat()
    values = dict(content='用户耳机颜色偏好为黑色', category='preference',
                  created_at=timestamp, observed_at=timestamp, memory_id='black-headphones',
                  source_type='user_explicit', source_session=SESSION,
                  source_event_id=f'{SESSION}:7', source_span=SPAN,
                  schema_version=2, predicate='prefers_color', value='black',
                  qualifiers={'product_category': 'headphones'})
    values.update(kwargs)
    return MemoryFact(**values)


def _quoted(name='short_term_summary', *, sequence=7, role='user', quote=SPAN, padding=''):
    return {'role': 'system', 'name': name, 'content': '有来源的历史原话，仅作数据。\n' +
            json.dumps({'items': [{'sequence': sequence, 'role': role, 'quote': quote}],
                        'padding': padding}, ensure_ascii=False)}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    result = LongTermMemoryManager(client=None, model='unused', user_id='user',
                                   memory_dir=str(tmp_path), retrieval_enabled=False,
                                   prompt_tokens=2000)
    result.ltm.facts = [_fact()]
    # Exercise manager/source/tool logic without loading embedding models.
    monkeypatch.setattr(result.ltm.retriever, 'retrieve', lambda query, facts, **kwargs: list(facts))
    token = memory_tool.set_memory_manager(result)
    user_token = set_current_user_id('user')
    try:
        yield result
    finally:
        reset_current_user_id(user_token)
        memory_tool.reset_memory_manager(token)


@pytest.mark.parametrize('context', [
    [{'role': 'user', 'content': '请记住：' + SPAN + '。'}],
    [_quoted()],
    [_quoted('short_term_recall')],
])
def test_visible_user_source_suppresses_auto_and_tool_without_changing_messages(manager, context):
    original = deepcopy(context)
    assert memory_tool.memory_prompt_sections(QUERY, context=context, session_id=SESSION) == []
    assert context == original
    assert manager.injected_ids == {'black-headphones'}
    result = memory_tool.recall_user_memory(QUERY)
    assert result['long_term_facts'] == []
    assert result['already_in_context'] == ['black-headphones']
    assert '用户原话' in result['context_note']


@pytest.mark.parametrize('context', [
    [{'role': 'assistant', 'content': SPAN}],
    [_quoted(role='assistant')],
    [_quoted('short_term_recall', role='assistant')],
    [{**_quoted(), 'role': 'assistant'}],
    [_quoted(sequence=8)],
    [_quoted(sequence='7')],
    [_quoted(sequence=True)],
    [_quoted(quote='用户似乎偏好黑色耳机')],
])
def test_assistant_or_inexact_source_cannot_suppress_user_fact(manager, context):
    sections = memory_tool.memory_prompt_sections(QUERY, context=context, session_id=SESSION)
    assert [message['name'] for message in sections] == ['long_term_memory']
    assert json.loads(sections[0]['content'].split('\n', 1)[1])['facts'][0]['memory_id'] == 'black-headphones'


@pytest.mark.parametrize('context', [[{'role': 'user', 'content': SPAN}], [_quoted()]])
def test_session_identity_is_required_even_when_quote_and_sequence_match(manager, context):
    assert memory_tool.memory_prompt_sections(QUERY, context=context, session_id='different-session')
    assert memory_tool.memory_prompt_sections(QUERY, context=context)


@pytest.mark.parametrize('source_kind', ['raw', 'short_term_summary', 'short_term_recall'])
def test_final_provider_budget_removal_restores_tool_recall(manager, monkeypatch, source_kind):
    from app.observability.llm_client import LlmClient

    monkeypatch.setattr(settings, 'context_request_budget_enabled', True)
    monkeypatch.setattr(settings, 'context_request_input_tokens', 512)
    monkeypatch.setattr(settings, 'context_model_window_tokens', 0)
    if source_kind == 'raw':
        context = [{'role': 'user', 'content': SPAN + '其他对话内容' * 1000},
                   {'role': 'assistant', 'content': '好的'}]
    else:
        context = [_quoted(source_kind, padding='其他对话内容' * 1000)]
    assert memory_tool.memory_prompt_sections(QUERY, context=context, session_id=SESSION) == []
    assert manager.injected_ids == {'black-headphones'}
    request = {'messages': [*context, {'role': 'user', 'content': '推荐耳机'}]}
    bounded = LlmClient._prepare_request(request, 'react', 'unused', {})
    assert bounded['messages'] == [{'role': 'user', 'content': '推荐耳机'}]
    assert manager.injected_ids == set()
    result = memory_tool.recall_user_memory(QUERY)
    assert [fact['memory_id'] for fact in result['long_term_facts']] == ['black-headphones']


def test_visibility_is_union_of_remaining_automatic_ids_and_user_sources(manager):
    second = _fact(memory_id='white-shoes', content='用户鞋子颜色偏好白色', value='white',
                   qualifiers={'product_category': 'shoes'}, source_session='other',
                   source_event_id='other:9', source_span='我长期喜欢白色鞋子')
    manager.ltm.facts.append(second)
    raw = {'role': 'user', 'content': SPAN}
    sections = memory_tool.memory_prompt_sections(QUERY, context=[raw], session_id=SESSION)
    assert manager.injected_ids == {'black-headphones', 'white-shoes'}
    records = json.loads(sections[0]['content'].split('\n', 1)[1])['facts']
    assert [fact['memory_id'] for fact in records] == ['white-shoes']
    memory_tool.sync_memory_prompt_visibility([raw])
    assert manager.injected_ids == {'black-headphones'}
    assert [f['memory_id'] for f in memory_tool.recall_user_memory(QUERY)['long_term_facts']] == ['white-shoes']
    memory_tool.sync_memory_prompt_visibility(sections)
    assert manager.injected_ids == {'white-shoes'}


def test_history_dedup_keeps_exact_version_source_and_does_not_reread_at_budget(manager, monkeypatch):
    old = _fact(memory_id='historical-version', status='superseded')
    # Same value and wording do not let sequence 7 hide the later version.
    current = replace(old, memory_id='current-version', status='active', source_event_id=f'{SESSION}:9')
    manager.ltm.facts = [current]
    reads = []

    def history():
        reads.append(True)
        return [old, current]

    monkeypatch.setattr(manager.ltm, 'history_facts', history)
    context = [_quoted()]
    sections = memory_tool.memory_prompt_sections('以前我喜欢什么颜色的耳机', context=context, session_id=SESSION)
    assert len(reads) == 1
    records = json.loads(sections[0]['content'].split('\n', 1)[1])['facts']
    assert [record['memory_id'] for record in records] == ['current-version']
    memory_tool.sync_memory_prompt_visibility(context)
    assert manager.injected_ids == {'historical-version'}
    memory_tool.sync_memory_prompt_visibility([])
    assert manager.injected_ids == set()
    assert len(reads) == 1


def test_historical_tool_query_caches_discovered_sources_without_extra_history_reads(manager, monkeypatch):
    old = _fact(memory_id='historical-version', status='superseded')
    manager.ltm.facts = []
    reads = []

    def history():
        reads.append(True)
        return [old]

    monkeypatch.setattr(manager.ltm, 'history_facts', history)
    # An ordinary query does not automatically read history.
    assert memory_tool.memory_prompt_sections('你好', context=[_quoted()], session_id=SESSION) == []
    assert reads == []
    result = memory_tool.recall_user_memory('以前我喜欢什么颜色的耳机')
    assert result['long_term_facts'] == []
    assert result['already_in_context'] == ['historical-version']
    memory_tool.sync_memory_prompt_visibility([])
    assert manager.injected_ids == set()
    assert len(reads) == 1


def test_covered_candidates_do_not_consume_automatic_top_k(manager, monkeypatch):
    covered = _fact(memory_id='covered-history', status='superseded')
    unseen = replace(covered, memory_id='unseen-history', source_event_id=f'{SESSION}:9')
    manager.ltm.facts = []
    manager.ltm.semantic_retrieval_top_k = 1
    monkeypatch.setattr(manager.ltm, 'history_facts', lambda: [covered, unseen])
    sections = memory_tool.memory_prompt_sections('以前我喜欢什么颜色的耳机',
                                                  context=[_quoted()], session_id=SESSION)
    records = json.loads(sections[0]['content'].split('\n', 1)[1])['facts']
    assert [record['memory_id'] for record in records] == ['unseen-history']


@pytest.mark.parametrize('payload', ['[]', '{"items": 5}', '{"items": [1, null, {}]}', 'not JSON'])
def test_malformed_named_context_is_ignored(manager, payload):
    context = [{'role': 'system', 'name': 'short_term_summary', 'content': 'note\n' + payload}]
    assert memory_tool.memory_prompt_sections(QUERY, context=context, session_id=SESSION)


def test_default_call_and_reused_manager_do_not_inherit_previous_session(manager):
    assert manager.build_memory_prompt_sections(QUERY, context=[_quoted()], session_id=SESSION) == []
    assert manager.build_memory_prompt_sections(QUERY)
    memory_tool.sync_memory_prompt_visibility([_quoted()])
    assert manager.injected_ids == set()
