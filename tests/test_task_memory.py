"""Source-aware memory against SQLite and isolated real PostgreSQL."""
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.memory.long_term import MemoryFact
from app.memory.maintenance import forget_source, process_memory_invalidations
from app.memory.runtime import build_memory_manager, process_memory_jobs
from app.memory.source_policy import SourcePolicy
from app.memory.task_context import task_history
from app.support.store import SupportError
from app.session.short_term import ContextService, _current_summary, _recall
from app.tasks.execution import TaskExecutor
from app.tasks.execution_store import TaskRuns
from app.tasks.repository import TaskRepository
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store, message
from tests.test_task_execution import Client, engine, request, response, local_budgets


@pytest.fixture
def memory_env(store, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'memory_enabled', True)
    monkeypatch.setattr(settings, 'memory_storage_backend', 'json')
    monkeypatch.setattr(settings, 'memory_dir', str(tmp_path / 'memory'))
    monkeypatch.setattr(settings, 'memory_redis_url', '')
    monkeypatch.setattr(settings, 'memory_retrieval_enabled', False)
    monkeypatch.setattr(settings, 'memory_retrieval_min_score', 0.05)
    monkeypatch.setattr(settings, 'memory_extraction_debounce_seconds', 0)
    return store


def remember(store, *, user='alice', sid='old', text='我长期喜欢黑色耳机'):
    source = message(store, sid, user, text)
    with store.transaction(read_only=True) as tx:
        proof = SourcePolicy(store, user).source(tx, sid, source['sequence'])['proof']
    fact = MemoryFact(content=text, category='preference',
        created_at=datetime.now(timezone.utc).isoformat(), confidence=.95,
        source_type='user_explicit', source_session=sid, source_event_id=f"{sid}:{source['sequence']}",
        source_span=text, memory_key='headphone_color', predicate='prefers_color', metadata={'source_proof': proof})
    manager = build_memory_manager(user, store=store)
    manager.ltm.merge_and_save([fact])
    return manager, source


def test_forget_immediately_blocks_warm_manager_and_history_then_cleans_ledger(memory_env):
    store = memory_env
    manager, source = remember(store)
    assert manager.retrieve_relevant_long_term_memory('黑色耳机')
    forget_source(store, 'alice', 'old', source['sequence'])
    assert manager.retrieve_relevant_long_term_memory('黑色耳机') == []
    assert manager.retrieve_relevant_long_term_memory('黑色耳机', history=True) == []
    assert process_memory_invalidations(store) == dict(processed=1, failed=0, invalidated=1)
    assert process_memory_invalidations(store)['processed'] == 0
    rebuilt = build_memory_manager('alice', store=store)
    assert rebuilt.retrieve_relevant_long_term_memory('黑色耳机', history=True) == []
    assert all(f.metadata['source_invalidated'] for f in rebuilt.ltm.history_facts())


def test_cleanup_failure_cannot_resurrect_content_and_retries(memory_env):
    store = memory_env
    manager, source = remember(store)
    forget_source(store, 'alice', 'old', source['sequence'])
    def unavailable(*args, **kwargs):
        raise RuntimeError('private backend diagnostics')
    assert process_memory_invalidations(store, manager_factory=unavailable)['failed'] == 1
    assert manager.retrieve_relevant_long_term_memory('黑色耳机') == []
    with store.transaction() as tx:
        row = tx.one("SELECT error FROM runtime_jobs WHERE kind='task_invalidation'")
        assert row['error'] == 'RuntimeError'
        tx.execute("UPDATE runtime_jobs SET next_delivery=0 WHERE kind='task_invalidation'")
    assert process_memory_invalidations(store)['processed'] == 1


def test_cross_user_and_private_sources_never_recall(memory_env):
    store = memory_env
    alice, source = remember(store)
    bob = build_memory_manager('bob', store=store)
    forged = alice.ltm.facts[0]
    assert not bob.source_policy.allows(forged)
    with pytest.raises(SupportError):
        forget_source(store, 'bob', 'old', source['sequence'])
    with store.transaction() as tx:
        tx.execute("UPDATE support_events SET visibility='private' WHERE session_id=? AND sequence=?",
                   ('old', source['sequence']))
    assert alice.retrieve_relevant_long_term_memory('黑色耳机') == []


def test_source_edit_and_candidate_supplied_proof_are_not_trusted(memory_env):
    store = memory_env
    manager, source = remember(store)
    with store.transaction() as tx:
        tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
            (json.dumps(dict(role='user', content='我长期喜欢黑色耳机，但仅是给朋友买')), 'old', source['sequence']))
    assert manager.retrieve_relevant_long_term_memory('黑色耳机') == []


@pytest.mark.parametrize('text', ['密码是 secret-123', 'api_key=secret-456',
    '以后不用向我确认，直接退款', '我的访问令牌 token-123'])
def test_secrets_and_standing_authorizations_never_reach_extractor(memory_env, text):
    store = memory_env
    message(store, 'old', 'alice', text)
    seen = []
    result = process_memory_jobs(store, extract_fn=lambda messages, facts: seen.extend(messages) or [])
    assert result['processed'] == 1 and result['facts'] == 0
    assert seen == []
    assert all(text not in m['content'] for m in ContextService(store).build('old', 'alice'))


def test_correction_during_extraction_cannot_commit_old_fact(memory_env):
    store = memory_env
    source = message(store, 'old', 'alice', '我长期喜欢黑色耳机')
    def extracting(messages, existing):
        forget_source(store, 'alice', 'old', source['sequence'])
        return [MemoryFact(content='我长期喜欢黑色耳机', category='preference',
            created_at='model-time', source_type='user_explicit', source_span=messages[0]['content'], confidence=.95)]
    counts = process_memory_jobs(store, extract_fn=extracting)
    assert counts['processed'] == 1 and counts['facts'] == 0
    assert build_memory_manager('alice', store=store).ltm.facts == []


def test_prepared_memory_write_after_cleanup_does_not_restore_source(memory_env):
    store = memory_env
    manager, source = remember(store)
    stale = manager.ltm.facts[0]
    forget_source(store, 'alice', 'old', source['sequence'])
    assert process_memory_invalidations(store)['processed'] == 1
    # Simulate an extractor which began before the cleanup, with a new batch id.
    stale.memory_id = ''
    manager.ltm.merge_and_save([stale], source_batch_id='late-worker')
    assert manager.ltm.facts == []


def test_association_correction_invalidates_fact_without_deleting_original(memory_env):
    from tests.test_association_correction import mistaken, request as correction
    from app.tasks.correction import CorrectionService
    store = memory_env
    wrong, target, decision = mistaken(store)
    with store.transaction(read_only=True) as tx:
        src = tx.one('SELECT session_id,sequence FROM association_sources WHERE decision_id=?', (decision['decision_id'],))
        proof = SourcePolicy(store, 'alice').source(tx, src['session_id'], src['sequence'])
    manager = build_memory_manager('alice', store=store)
    manager.ltm.merge_and_save([MemoryFact(content='用户耳机左耳没有声音', category='other',
        created_at=datetime.now(timezone.utc).isoformat(), confidence=.95, source_type='user_explicit',
        source_session=src['session_id'], source_event_id=f"{src['session_id']}:{src['sequence']}",
        source_span=proof['content'], metadata={'source_proof': proof['proof']})])
    saved = manager.ltm.facts[0]
    assert manager.source_policy.allows(saved)
    CorrectionService(store, 'alice').correct('s', correction(store, wrong, target, decision))
    assert not manager.source_policy.allows(saved)
    assert process_memory_invalidations(store)['invalidated'] == 1
    assert any('左耳没声音' in m['content'] for m in store.public_history('s', 'alice'))


def test_summary_rereads_full_source_and_rejects_deleted_or_private_quotes(memory_env):
    store = memory_env
    source = message(store, 'old', 'alice', '如果不能换货才退款；现在不要退款')
    items = [dict(sequence=source['sequence'], role='user', kind='user_request', quote='退款')]
    with store.transaction(read_only=True) as tx:
        assert _current_summary(tx, 'old', items)[0]['quote'] == '如果不能换货才退款；现在不要退款'
    forget_source(store, 'alice', 'old', source['sequence'])
    with store.transaction(read_only=True) as tx:
        assert _current_summary(tx, 'old', items) == []


def test_long_recall_preserves_distant_negation_as_whole_message(memory_env):
    store = memory_env
    text = '不要退款。' + '背景说明。' * 1000 + 'ORD-A 退款问题'
    src = message(store, 'old', 'alice', text)
    with store.transaction(read_only=True) as tx:
        items = _recall(tx, 'old', None, 'ORD-A', src['sequence'] + 1)
    assert items[0]['quote'] == text


def test_new_task_extraction_does_not_segment_oversize_source(memory_env, monkeypatch):
    from app.memory.runtime import _extraction_segments
    from app.memory.extraction import MemoryExtractionError
    monkeypatch.setattr(settings, 'memory_extraction_input_tokens', 2500)
    text = '仅限朋友使用，不能记成我的偏好。' + '背景。' * 10000 + '喜欢黑色耳机'
    with pytest.raises(MemoryExtractionError, match='Whole task source'):
        _extraction_segments([dict(role='user', content=text, whole_source_required=True)], [])


def test_task_executor_recalls_owned_preferences_and_rechecks_deletion(memory_env):
    store = memory_env
    _, source = remember(store)
    record, current = task(store, 's', goal='查询订单和历史偏好')
    def second(params):
        tools = [json.loads(m['content']) for m in params['messages'] if m['role'] == 'tool']
        assert any(v.get('long_term_facts') for v in tools)
        forget_source(store, 'alice', 'old', source['sequence'])
        return response('我记得你的偏好')
    client = Client(response(calls=[('recall_user_memory', {'query': '黑色耳机'})]), second)
    outcome = TaskExecutor(store, 'alice').run(request(record, current), engine(client))
    assert outcome['state'] == 'deferred'
    assert outcome['reason'] == 'memory_context_changed'
    assert len(client.requests) == 2


def test_memory_failure_still_supplies_original_task_context(memory_env, monkeypatch):
    import app.memory.runtime as runtime
    store = memory_env
    record, source = task(store, 's')
    monkeypatch.setattr(runtime, 'build_memory_manager', lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    client = Client(response(calls=[('recall_user_memory', {'query': '耳机偏好'})]), response('需要继续核实订单'))
    outcome = TaskExecutor(store, 'alice').run(request(record, source), engine(client))
    assert outcome['state'] == 'ready'
    original = next(m for m in client.requests[0]['messages'] if m['role'] == 'user')
    assert record.task_id in original['content'] and 'ORD-A' in original['content']
    assert 'false' in next(m['content'] for m in client.requests[1]['messages'] if m['role'] == 'tool')


def test_cross_session_refund_progress_has_receipt_without_confirmation_or_replay(memory_env, monkeypatch):
    from tests.test_task_operations import Business, ready, confirm
    store = memory_env
    monkeypatch.setattr(settings, 'task_operations_enabled', True)
    monkeypatch.setattr(settings, 'api_jwt_secret', 'isolated-memory-confirmation-signing-key-long-enough')
    business = Business()
    service, record, source, card = ready(store, business, sid='old')
    confirm(service, card, sid='old')
    repo = TaskRepository(store, 'alice')
    current = repo.get(record.task_id)
    resumed_source = message(store, 's', 'alice', '继续之前那笔退款，进度怎么样')
    current = repo.update(current.task_id, 's', expected_version=current.version, change_id=str(uuid4()),
        source=resumed_source, patch={'status': 'in_progress'})
    history = task_history(TaskRuns(store, 'alice'), current)
    assert history['operations'][0]['status'] == 'succeeded'
    assert history['operations'][0]['receipt']['status'] == 'submitted'
    assert card['confirmation_token'] not in json.dumps(history)
    assert 'confirmation_token' not in json.dumps(history)
    assert len(business.writes) == 1
    client = Client(response(calls=[('read_case_memory', {})]), response('退款申请已受理，需核实后续进度'))
    result = TaskExecutor(store, 'alice').run(request(current, resumed_source), engine(client))
    assert result['state'] == 'ready'
    assert len(business.writes) == 1


def test_source_deleted_during_model_call_prevents_result_or_further_tools(memory_env):
    store = memory_env
    record, source = task(store, 's')
    called = []
    def changed(params):
        forget_source(store, 'alice', 's', source['sequence'])
        return response(calls=[('query_order', {'order_id': 'ORD-A'})])
    outcome = TaskExecutor(store, 'alice', tools=lambda *args: called.append(args)).run(
        request(record, source), engine(Client(changed)))
    assert outcome['reason'] == 'memory_context_changed'
    assert called == []


def test_preference_retrieval_does_not_allow_switching_order(memory_env):
    store = memory_env
    remember(store)
    record, source = task(store, 's')
    calls = []
    client = Client(response(calls=[('recall_user_memory', {'query': '黑色耳机'})]),
        response(calls=[('query_order', {'order_id': 'ORD-B'})]), response('请核实当前订单'))
    outcome = TaskExecutor(store, 'alice', tools=lambda *args: calls.append(args)).run(
        request(record, source), engine(client))
    assert calls == []
    tool_results = [json.loads(m['content']) for m in client.requests[-1]['messages'] if m['role'] == 'tool']
    assert any(r.get('error') == 'arguments_or_object_outside_task' for r in tool_results)
    assert outcome['task_id'] == record.task_id


def test_expired_cleanup_lease_is_recovered_and_duplicate_cleanup_is_safe(memory_env):
    from concurrent.futures import ThreadPoolExecutor
    store = memory_env
    _, source = remember(store)
    forget_source(store, 'alice', 'old', source['sequence'])
    with store.transaction() as tx:
        tx.execute("UPDATE runtime_jobs SET delivery_owner='dead-worker',delivery_lease=1 WHERE kind='task_invalidation'")
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: process_memory_invalidations(store), range(2)))
    assert sum(o['processed'] for o in outcomes) == 1
    assert sum(o['failed'] for o in outcomes) == 0
    assert build_memory_manager('alice', store=store).ltm.facts == []


def test_manager_reload_respects_authoritative_reset_even_with_loaded_facts(memory_env):
    store = memory_env
    manager, _ = remember(store)
    assert manager.retrieve_relevant_long_term_memory('黑色耳机')
    another = build_memory_manager('alice', store=store)
    another.reset_all()
    assert manager.retrieve_relevant_long_term_memory('黑色耳机') == []
    assert manager.retrieve_relevant_long_term_memory('黑色耳机', history=True) == []


def test_task_wire_budget_never_projects_away_tool_conditions(memory_env, monkeypatch):
    from app.session.request_budget import apply_request_budget, ContextBudgetExceeded
    import app.session.request_budget as budget
    monkeypatch.setattr(settings, 'context_request_budget_enabled', True)
    monkeypatch.setattr(budget, 'request_input_limit', lambda params: 1000)
    request = dict(messages=[{'role': 'user', 'content': '核实退款'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'q1', 'type': 'function', 'function': {'name': 'query_order', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'q1', 'content': json.dumps({
            'description': '不能退款。' + '背景说明。' * 4000 + '只有人工核实后才可能办理'})}],
        tools=[{'type': 'function', 'function': {'name': 'query_order', 'parameters': {'type': 'object'}}}])
    with pytest.raises(ContextBudgetExceeded):
        apply_request_budget(request, scene='task_execution')
    assert '不能退款' in request['messages'][-1]['content'] or '\\u4e0d' in request['messages'][-1]['content']


def test_real_postgres_memory_ledger_and_stale_redis_cannot_restore_deleted_source(memory_env, monkeypatch):
    from dataclasses import asdict
    store = memory_env
    with store.transaction(read_only=True) as tx:
        if not tx.postgres:
            pytest.skip('isolated PostgreSQL and Redis integration only')
    monkeypatch.setattr(settings, 'memory_storage_backend', 'postgres')
    monkeypatch.setattr(settings, 'memory_postgres_dsn', store.dsn)
    monkeypatch.setattr(settings, 'memory_redis_url', 'redis://127.0.0.1:26379/0')
    monkeypatch.setattr(settings, 'memory_cache_key_prefix', 'test:task-memory:' + uuid4().hex)
    manager, source = remember(store)
    cache = manager.ltm.store.cache
    old = {'facts': [asdict(f) for f in manager.ltm.facts]}
    cache.set('alice', old, 99)
    assert cache.get('alice')[1] == 99
    try:
        forget_source(store, 'alice', 'old', source['sequence'])
        assert manager.retrieve_relevant_long_term_memory('黑色耳机') == []
        assert process_memory_invalidations(store)['invalidated'] == 1
        cache.set('alice', old, 100)  # A delayed cache write after cleanup.
        assert build_memory_manager('alice', store=store).retrieve_relevant_long_term_memory('黑色耳机', history=True) == []
    finally:
        cache.delete('alice')


def test_invalid_memory_is_not_used_as_extraction_hint(memory_env):
    store = memory_env
    manager, source = remember(store)
    from app.memory.runtime import _bounded_batch
    assert _bounded_batch([dict(role='user', content='我长期喜欢白色耳机')], manager.ltm.facts)[1]
    forget_source(store, 'alice', 'old', source['sequence'])
    message(store, 's', 'alice', '我长期喜欢白色耳机')
    seen = []
    def extract(messages, existing):
        seen.extend(existing)
        return []
    assert process_memory_jobs(store, extract_fn=extract)['failed'] == 0
    assert seen == []


def test_source_proof_is_server_generated_and_survives_reload(memory_env):
    store = memory_env
    source = message(store, 's', 'alice', '我长期喜欢黑色耳机')
    def extract(messages, existing):
        return [MemoryFact(content='我长期喜欢黑色耳机', category='preference', created_at='fake',
            source_type='user_explicit', source_span=messages[0]['content'], confidence=.95,
            metadata={'source_proof': {'user_id': 'bob', 'sequence': 999}})]
    assert process_memory_jobs(store, extract_fn=extract)['facts'] == 1
    manager = build_memory_manager('alice', store=store)
    proof = manager.ltm.facts[0].metadata['source_proof']
    assert proof['user_id'] == 'alice' and proof['sequence'] == source['sequence']
    assert proof['session_id'] == 's' and len(proof['digest']) == 64


def test_candidate_request_and_execution_sources_exclude_secret_payloads(memory_env):
    from app.tasks.candidates import CandidateService
    store = memory_env
    text = 'ORD-A 查询进度，密码是 do-not-send-secret'
    source = message(store, 's', 'alice', text)
    context = CandidateService(store, 'alice').build('s', [source['sequence']])
    assert 'do-not-send-secret' not in json.dumps(context.request(), ensure_ascii=False)
    record = TaskRepository(store, 'alice').create('s', change_id=str(uuid4()), source=source,
        goal='查询订单', details={'order_id': 'ORD-A'})
    client = Client(response('请补充不含敏感信息的描述'))
    TaskExecutor(store, 'alice').run(request(record, source), engine(client))
    assert client.requests and 'do-not-send-secret' not in json.dumps(client.requests, ensure_ascii=False)


def test_cross_session_summary_only_restores_full_originals_of_this_task(memory_env, monkeypatch):
    store = memory_env
    monkeypatch.setattr(settings, 'short_term_memory_enabled', True)
    record, source = task(store, 'old', goal='如果无法换货才考虑退款，现在不要退款')
    repo = TaskRepository(store, 'alice')
    for index in range(10):
        later = message(store, 'old', 'alice', f'补充说明 {index}')
        record = repo.update(record.task_id, 'old', expected_version=record.version,
            change_id=str(uuid4()), source=later, patch={'goal': record.goal})
    unrelated = message(store, 'old', 'alice', '另一件商品的个人资料')
    with store.transaction() as tx:
        tx.execute("INSERT INTO support_context_snapshots VALUES (?,'session',?,1,?,?)",
            ('old', unrelated['sequence'], json.dumps({'items': [
                dict(sequence=source['sequence'], role='user', kind='user_request', quote='退款'),
                dict(sequence=unrelated['sequence'], role='user', kind='user_request', quote='个人资料')]}),
             datetime.now(timezone.utc).isoformat()))
    context = task_history(TaskRuns(store, 'alice'), record)
    assert context['historical_sources'] == [dict(session_id='old', sequence=source['sequence'], text=record.goal)]
    with store.transaction() as tx:
        tx.execute("UPDATE support_context_snapshots SET payload='broken-cache'")
    recovered = task_history(TaskRuns(store, 'alice'), record)
    assert recovered['historical_sources'] == [] and len(recovered['sources']) == 8


def test_slow_memory_lookup_releases_execution_without_a_late_model_call(memory_env, monkeypatch):
    import threading
    import app.memory.runtime as runtime
    store = memory_env
    record, source = task(store, 's')
    release = threading.Event()
    entered = threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        release.wait(5)
        raise RuntimeError('delayed optional memory')
    monkeypatch.setattr(runtime, 'build_memory_manager', delayed)
    client = Client(response(calls=[('recall_user_memory', {'query': '黑色耳机'})]))
    try:
        result = TaskExecutor(store, 'alice').run(request(record, source, call_seconds=.3), engine(client))
        assert entered.is_set()
        assert result['state'] == 'deferred' and result['reason'] == 'memory_timeout_or_capacity'
        assert len(client.requests) == 1
    finally:
        release.set()


def test_deletion_source_sequence_cannot_create_an_ineffective_tombstone(memory_env):
    store = memory_env
    manager, source = remember(store)
    for value in (True, float(source['sequence']), str(source['sequence']), 0):
        with pytest.raises(SupportError, match='正整数'):
            forget_source(store, 'alice', 'old', value)
    assert manager.retrieve_relevant_long_term_memory('黑色耳机')
