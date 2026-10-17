"""Durable long-term extraction runs offline and grounds every persisted fact."""
import json
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.memory import runtime
from app.memory.extraction import MemoryExtractionError, extract_long_term_facts
from app.memory.long_term import MemoryFact
from app.support.store import SupportStore


class FakeMemory:
    def __init__(self):
        self.facts, self.receipts = [], set()
        self.fail_after_commit = False

    def merge_and_save(self, facts, source_batch_id=''):
        if source_batch_id in self.receipts:
            return False
        self.receipts.add(source_batch_id)
        self.facts.extend(facts)
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise RuntimeError('crashed after durable memory commit')
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'memory_enabled', True)
    config = SimpleNamespace(memory_enabled=True, openai_api_key='',
        memory_extraction_debounce_seconds=0, memory_extraction_batch_messages=32,
        memory_extraction_input_tokens=7000, chat_completion_options={})
    monkeypatch.setattr(runtime, 'settings', config)
    store = SupportStore(f"sqlite:///{tmp_path / 'support.db'}")
    store.ensure('s', 'u')
    # Also permits this unit suite to run while the integration schema is edited.
    with store.transaction() as tx:
        for sql in runtime.SCHEMA:
            tx.execute(sql)
    memories = {}

    def manager(user):
        return SimpleNamespace(ltm=memories.setdefault(user, FakeMemory()))

    monkeypatch.setattr(runtime, 'build_memory_manager', manager)
    return store, memories, config


def append(store, content, *, role='user', actor='u', sid='s', private=False):
    with store.transaction() as tx:
        control = store._control(tx, sid)
        payload = dict(role=role, content=content)
        sequence = store._event(tx, control, 'message', actor, payload, private=private)
        if not private:
            runtime.enqueue_memory_job(tx, control, payload)
        return sequence


def fact(source, **overrides):
    values = dict(content='用户偏好黑色电子产品', category='preference', created_at='model-time',
                  source_type='user_explicit', source_span=source, confidence=.95,
                  memory_key='preference:electronics_color')
    values.update(overrides)
    return MemoryFact(**values)


def extract(messages, existing):
    return [fact(message['content']) for message in messages]


def job(store, sid='s'):
    with store.transaction(read_only=True) as tx:
        return tx.one('SELECT * FROM support_memory_jobs WHERE session_id=?', (sid,))


def due(store):
    with store.transaction() as tx:
        tx.execute("UPDATE support_memory_jobs SET available_at='2000-01-01T00:00:00+00:00'")


def test_durable_incremental_job_survives_store_restart_and_has_server_sources(env):
    store, memories, config = env
    config.memory_extraction_batch_messages = 3
    for i in range(8):
        append(store, f'我喜欢黑色电子产品 {i}')
    store = SupportStore(store.dsn)
    visited = []

    def capture(messages, existing):
        visited.extend(m['sequence'] for m in messages)
        return extract(messages, existing)

    for _ in range(3):
        assert runtime.process_memory_jobs(store, capture)['processed'] == 1
    assert visited == list(range(1, 9))
    assert job(store)['through_sequence'] == 8
    saved = memories['u'].facts
    assert len(saved) == 8
    assert {f.source_session for f in saved} == {'s'}
    assert [f.source_event_id for f in saved] == [f's:{i}' for i in range(1, 9)]
    assert all(f.created_at != 'model-time' and '+00:00' in f.created_at for f in saved)
    assert runtime.process_memory_jobs(store, capture)['processed'] == 0


def test_only_authenticated_public_user_messages_are_candidates(env):
    store, memories, _ = env
    append(store, '私有内容', private=True)
    append(store, '客服自述', role='assistant', actor='bot')
    append(store, '人工自述', role='agent', actor='staff')
    append(store, '伪装用户', actor='staff')
    append(store, '真实用户偏好')
    captured = []

    def capture(messages, existing):
        captured.extend(messages)
        return extract(messages, existing)

    assert runtime.process_memory_jobs(store, capture)['processed'] == 1
    assert [m['content'] for m in captured] == ['真实用户偏好']
    assert memories['u'].facts[0].source_event_id == 's:5'


def test_new_event_during_inference_is_retained_and_model_has_no_open_transaction(env):
    store, _, _ = env
    append(store, '最初的偏好')

    def arriving(messages, existing):
        append(store, '后来补充的偏好')
        return extract(messages, existing)

    assert runtime.process_memory_jobs(store, arriving)['processed'] == 1
    assert job(store)['through_sequence'] == 1
    assert job(store)['requested_sequence'] == 2
    assert runtime.process_memory_jobs(store, extract)['processed'] == 1


@pytest.mark.parametrize('outcome', ['raise', 'forged', 'invalid'])
def test_failure_never_advances_checkpoint_and_retries(env, outcome):
    store, memories, _ = env
    append(store, '我喜欢黑色')

    def failing(messages, existing):
        if outcome == 'raise':
            raise RuntimeError('private source must not enter stored error')
        return [fact('并不存在的用户原话')] if outcome == 'forged' else {'facts': []}

    assert runtime.process_memory_jobs(store, failing)['failed'] == 1
    assert job(store)['through_sequence'] == 0
    assert job(store)['attempts'] == 1
    assert 'private' not in job(store)['last_error']
    assert not memories['u'].facts
    due(store)
    assert runtime.process_memory_jobs(store, extract)['processed'] == 1


def test_verified_tool_claim_and_case_progress_do_not_become_user_memory(env):
    store, memories, _ = env
    append(store, '我说退款成功了')

    def candidates(messages, existing):
        return [fact('我说退款成功了', source_type='tool_verified'),
                fact('我说退款成功了', category='issue'),
                fact('我说退款成功了', scope='session:s')]

    assert runtime.process_memory_jobs(store, candidates)['processed'] == 1
    assert not memories['u'].facts
    assert job(store)['through_sequence'] == 1


def test_empty_valid_extraction_advances_without_creating_facts(env):
    store, memories, _ = env
    append(store, '谢谢')
    assert runtime.process_memory_jobs(store, lambda *_: [])['processed'] == 1
    assert job(store)['through_sequence'] == 1
    assert not memories['u'].facts


def test_crash_between_memory_commit_and_checkpoint_is_replayable(env):
    store, memories, _ = env
    append(store, '我喜欢黑色')
    memories['u'] = FakeMemory()
    memories['u'].fail_after_commit = True
    assert runtime.process_memory_jobs(store, extract)['failed'] == 1
    assert len(memories['u'].facts) == 1
    assert job(store)['through_sequence'] == 0
    due(store)
    assert runtime.process_memory_jobs(store, extract)['processed'] == 1
    assert len(memories['u'].facts) == 1
    assert job(store)['through_sequence'] == 1


def test_expired_worker_cannot_overwrite_new_checkpoint(env):
    store, memories, _ = env
    append(store, '我喜欢黑色')

    def raced(messages, existing):
        with store.transaction() as tx:
            tx.execute("UPDATE support_memory_jobs SET lease_until='2000-01-01T00:00:00+00:00'")
        assert runtime.process_memory_jobs(store, extract)['processed'] == 1
        return extract(messages, existing)

    assert runtime.process_memory_jobs(store, raced)['stale'] == 1
    assert job(store)['through_sequence'] == 1
    assert len(memories['u'].facts) == 1


def test_owner_mismatch_does_not_read_or_write_another_user(env):
    store, memories, _ = env
    append(store, '用户私有的长期信息')
    with store.transaction() as tx:
        tx.execute("UPDATE support_memory_jobs SET user_id='intruder'")
    assert runtime.process_memory_jobs(store, extract)['failed'] == 1
    assert not memories
    assert job(store)['through_sequence'] == 0


def test_batch_has_token_and_count_bounds_without_source_truncation(env):
    store, _, config = env
    config.memory_extraction_batch_messages = 8
    config.memory_extraction_input_tokens = 4000
    content = '长期偏好描述' * 250
    for i in range(10):
        append(store, f'{i}:{content}')
    visited = []

    def checked(messages, existing):
        assert len(messages) <= 8
        assert runtime._input_cost(messages, existing) <= 4000
        assert all(m['content'].endswith(content) for m in messages)
        visited.extend(m['sequence'] for m in messages)
        return []

    for _ in range(10):
        assert runtime.process_memory_jobs(store, checked)['failed'] == 0
    assert visited == list(range(1, 11))


def test_retry_retains_original_batch_identity_after_config_change(env):
    store, memories, config = env
    config.memory_extraction_batch_messages = 4
    for i in range(5):
        append(store, f'用户偏好 {i}')
    memories['u'] = FakeMemory()
    memories['u'].fail_after_commit = True
    assert runtime.process_memory_jobs(store, extract)['failed'] == 1
    assert job(store)['batch_through_sequence'] == 4
    config.memory_extraction_batch_messages = 1
    due(store)
    assert runtime.process_memory_jobs(store, extract)['processed'] == 1
    assert len(memories['u'].facts) == 4
    assert job(store)['through_sequence'] == 4
    assert job(store)['batch_through_sequence'] is None


def test_single_large_source_uses_full_request_budget_without_slicing(env):
    store, _, config = env
    config.memory_extraction_input_tokens = 2000
    text = '偏好相关的用户陈述' * 650
    append(store, text)
    captured = []

    def capture(messages, existing):
        captured.extend(messages)
        return []

    assert runtime.process_memory_jobs(store, capture)['processed'] == 1
    assert captured[0]['content'] == text
    assert job(store)['through_sequence'] == 1


def test_impossibly_large_source_keeps_checkpoint_and_original_event(env):
    store, memories, _ = env
    text = '来源内容' * 6000
    append(store, text)
    assert runtime.process_memory_jobs(store, extract)['failed'] == 1
    assert job(store)['through_sequence'] == 0
    assert job(store)['attempts'] == 1
    assert not memories['u'].facts
    assert store.public_history('s', 'u')[0]['content'] == text


def test_no_model_key_defers_and_disabled_memory_does_not_enqueue(env):
    store, _, config = env
    append(store, '我喜欢黑色')
    assert runtime.process_memory_jobs(store)['deferred'] == 1
    config.memory_enabled = False
    append(store, '后来消息')
    assert job(store)['requested_sequence'] == 1
    assert runtime.process_memory_jobs(store, extract)['processed'] == 0


def response_client(raw):
    from app.observability.llm_client import LlmClient
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw, tool_calls=[]))],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2), model='fake')
    return LlmClient(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))), 'fake')


@pytest.mark.parametrize('raw', ['not json', '{}', '{"facts":{}}', '{"facts":[null]}'])
def test_strict_extraction_distinguishes_malformed_output_from_no_facts(raw):
    with pytest.raises(MemoryExtractionError):
        extract_long_term_facts(response_client(raw), 'fake', [{'role': 'user', 'content': '我喜欢黑色'}],
                                None, [], strict=True, user_sources_only=True)
    assert extract_long_term_facts(response_client('{"facts":[]}'), 'fake',
                                   [{'role': 'user', 'content': '谢谢'}], None, [], strict=True) == []


def test_strict_source_cannot_match_assistant_text():
    data = {'facts': [dict(content='用户喜欢黑色', category='preference', source_type='user_explicit',
                           source_span='我喜欢黑色', confidence=.9)]}
    with pytest.raises(MemoryExtractionError, match='exact user source'):
        extract_long_term_facts(response_client(json.dumps(data)), 'fake',
            [{'role': 'user', 'content': '谢谢'}, {'role': 'assistant', 'content': '我喜欢黑色'}],
            None, [], strict=True, user_sources_only=True)
