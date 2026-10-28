"""Replay recorded user messages AND original extraction responses, without an LLM."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from app.memory import runtime
from app.memory.atomic import AtomicEvidenceError, normalize_candidate
from app.memory.extraction import _atomic_facts, build_extraction_messages
from app.memory.long_term import LongTermMemory, MemoryFact
from app.memory.source_policy import SourcePolicy
from tests.test_long_term_runtime import append, due, env, job


CASES = {c['session']: c for c in json.loads(
    (Path(__file__).parent / 'fixtures/memory_dialogue_cases.json').read_text(encoding='utf-8'))}


def candidate(text, *, predicate='budget_range', value=None, qualifiers=None, **overrides):
    return dict(predicate=predicate,
        value=value if value is not None else dict(min=None, max=100, currency='CNY'),
        qualifiers=qualifiers or {}, polarity='positive', operation='ASSERT',
        source_index=0, source_quote=text, confidence=.95, **overrides)


def source(text):
    return [dict(role='user', content=text, created_at='2026-10-26T16:00:00+00:00', sequence=1)]


def normalize(text, **kwargs):
    return normalize_candidate(candidate(text, **kwargs), source(text), explain=True)


def outcomes(store):
    with store.transaction(read_only=True) as tx:
        return tx.all('SELECT * FROM support_memory_candidate_outcomes ORDER BY source_sequence')


def install_real_memory(env, tmp_path, monkeypatch):
    store, _, _ = env
    memory = LongTermMemory(user_id='u', memory_dir=str(tmp_path / 'ltm'),
                            retrieval_enabled=False, preference_ttl_days=3650)
    memory.source_filter = SourcePolicy(store, 'u').allows
    monkeypatch.setattr(runtime, 'build_memory_manager', lambda user, **kw:
        SimpleNamespace(ltm=memory, source_policy=SourcePolicy(store, user)))
    return memory


def replay_case(store, case, *, sid):
    store.ensure(sid, 'u')
    for m in case['messages']:
        seq = append(store, m['content'], sid=sid)
        with store.transaction() as tx:
            tx.execute('UPDATE support_events SET created_at=? WHERE session_id=? AND sequence=?',
                       (m['created_at'], sid, seq))
    calls = []
    def extract(messages, existing):
        assert [m['content'] for m in messages] == [m['content'] for m in case['messages']]
        calls.append(messages)
        return _atomic_facts(deepcopy(case['response']), messages)
    result = runtime.process_memory_jobs(store, extract)
    assert result['processed'] == 1 and result['failed'] == 0
    assert len(calls) == 1
    assert job(store, sid)['through_sequence'] == len(case['messages'])
    return result


def test_real_session12_keeps_l_and_unrelated_updates_despite_bad_android(env, tmp_path, monkeypatch):
    store, _, _ = env
    memory = install_real_memory(env, tmp_path, monkeypatch)
    result = replay_case(store, CASES[12], sid='case12')
    assert result['facts'] == 7
    memory.load()
    size = next(f for f in memory.facts if f.predicate == 'wears_size')
    assert size.value == 'L' and size.qualifiers == {'product_category': 'clothing', 'size_system': 'INT'}
    assert size.source_span == '我现在买衣服喜欢黑色，常穿L码'
    assert any(f.operation == 'RETRACT' and f.value == 'blue' for f in memory.history_facts())
    budgets = {f.qualifiers['product_category']: f.value['max']
               for f in memory.facts if f.predicate == 'budget_range'}
    assert budgets == {'clothing': 100, 'phone': 3000}
    rows = outcomes(store)
    assert len(rows) == 3 and all(r['status'] == 'deferred' for r in rows)
    assert {r['reason'] for r in rows} == {'Unsupported normalized value', 'missing_size_system'}
    assert rows[0]['source_sequence'] == 6
    for fact in memory.facts:
        evidence = fact.evidence[0]
        original = CASES[12]['messages'][int(fact.source_event_id.split(':')[-1])-1]['content']
        assert original[evidence['start_offset']:evidence['end_offset']] == fact.source_span
        assert SourcePolicy(store, 'u').allows(fact)
        assert not SourcePolicy(store, 'another-user').allows(fact)
    assert runtime.process_memory_jobs(store, lambda *_: pytest.fail('already checkpointed'))['processed'] == 0


def test_real_budget_history_retains_both_categories_after_reload(env, tmp_path, monkeypatch):
    store, _, _ = env
    memory = install_real_memory(env, tmp_path, monkeypatch)
    expected = ({'phone': 2500}, {'phone': 2500, 'clothing': 100}, {'phone': 3000, 'clothing': 100})
    for number, values in zip((3, 4, 11), expected):
        replay_case(store, CASES[number], sid=f'case{number}')
        memory.load()
        budgets = {f.qualifiers['product_category']: f.value['max']
                   for f in memory.facts if f.predicate == 'budget_range'}
        assert budgets == values
    history = [f for f in memory.history_facts() if f.predicate == 'budget_range']
    assert next(f for f in history if f.value['max'] == 2500).status == 'superseded'
    assert all(f.memory_key != 'preference:budget_range' for f in history)


@pytest.mark.parametrize('text', ['平常短袖预算一百元以内', '平常T恤预算100元以内', '衣服预算100元以内'])
def test_clothing_aliases_and_chinese_amount(text):
    assert normalize(text)['qualifiers'] == {'product_category': 'clothing'}


@pytest.mark.parametrize(('text', 'reason'), [
    ('预算100元以内', 'budget_scope_unknown'),
    ('总预算100元以内', 'budget_scope_unknown'),
    ('衣服预算一百以内', 'missing_or_ambiguous_currency'),
    ('手机和衣服预算100元以内', 'ambiguous_product_category'),
    ('如果我买衣服，预算100元以内', 'non_stable_or_uncertain_source'),
    ('这次衣服预算100元以内', 'non_stable_or_uncertain_source'),
])
def test_unknown_scope_currency_and_temporary_budget_are_not_global(text, reason):
    with pytest.raises(AtomicEvidenceError, match=reason):
        normalize(text)


def test_only_explicit_global_budget_uses_global_slot():
    assert normalize('所有商品总预算100元以内')['memory_key'] == 'preference:budget_range'


@pytest.mark.parametrize(('text', 'amount'), [('手机预算3千元以内', 1000),
    ('衣服预算一百五元以内', 105), ('衣服预算一百百元以内', 200),
    ('我一直买衣服，预算100元以内', 1)])
def test_amount_parser_never_invents_numeric_suffix_or_colloquial_units(text, amount):
    with pytest.raises(AtomicEvidenceError, match='missing_amount_evidence'):
        normalize(text, value=dict(min=None, max=amount, currency='CNY'))


@pytest.mark.parametrize('text', ['我爸买衣服，常穿L码', '我买衣服；常穿L码',
    '我买衣服。他常穿L码', '我买衣服和鞋子，常穿L码', '我以前买衣服，常穿L码'])
def test_short_quote_cannot_borrow_other_subject_sentence_or_ambiguous_category(text):
    item = candidate('常穿L码', predicate='wears_size', value='L',
                     qualifiers={'product_category': 'clothing', 'size_system': 'INT'})
    assert normalize_candidate(item, source(text)) is None


@pytest.mark.parametrize(('predicate', 'value', 'category', 'text'), [
    ('prefers_operating_system', 'android', 'phone', '我的手机长期选择安卓系统'),
    ('prefers_fit', 'loose', 'clothing', '我买衣服喜欢宽松版型'),
])
def test_supported_os_fit_share_prompt_vocabulary(predicate, value, category, text):
    result = normalize(text, predicate=predicate, value=value, qualifiers={'product_category': category})
    assert result['value'] == value
    prompt = build_extraction_messages([], None, [], schema_version=2)[0]['content']
    assert predicate in prompt and value in prompt


def test_new_preferences_survive_store_reload_and_category_retrieval(env, tmp_path, monkeypatch):
    store, _, _ = env
    memory = install_real_memory(env, tmp_path, monkeypatch)
    text = '我的手机长期选择安卓系统。我买衣服喜欢宽松版型'
    append(store, text)
    items = [
        candidate('我的手机长期选择安卓系统', predicate='prefers_operating_system', value='android',
                  qualifiers={'product_category': 'phone'}),
        candidate('我买衣服喜欢宽松版型', predicate='prefers_fit', value='loose',
                  qualifiers={'product_category': 'clothing'}),
    ]
    assert runtime.process_memory_jobs(store, lambda messages, _: _atomic_facts(
        dict(schema_version=2, facts=items), messages))['facts'] == 2
    memory.load()
    assert [(f.predicate, f.value) for f in memory.retrieve_relevant('我的手机系统偏好')] == [
        ('prefers_operating_system', 'android')]
    assert [(f.predicate, f.value) for f in memory.retrieve_relevant('我的衣服版型偏好')] == [
        ('prefers_fit', 'loose')]


@pytest.mark.parametrize(('text', 'reason'), [
    ('父亲的手机一直选安卓', 'non_stable_or_uncertain_source'),
    ('我的手机选安卓或苹果系统', 'ambiguous_value'),
])
def test_new_system_predicate_never_borrows_family_or_chooses_between_two_values(text, reason):
    with pytest.raises(AtomicEvidenceError, match=reason):
        normalize(text, predicate='prefers_operating_system', value='android',
                  qualifiers={'product_category': 'phone'})


def test_bad_replacement_defers_related_retraction_only():
    messages = source('不要再按衣服蓝色，衣服现在喜欢黑色。手机预算100元以内')
    retract = candidate('不要再按衣服蓝色', predicate='prefers_color', value='blue',
                        qualifiers={'product_category': 'clothing'})
    retract['operation'] = 'RETRACT'
    invalid = candidate('衣服现在喜欢黑色', predicate='prefers_color', value='made-up',
                        qualifiers={'product_category': 'clothing'})
    budget = candidate('手机预算100元以内')
    batch = _atomic_facts(dict(schema_version=2, facts=[retract, invalid, budget]), messages)
    assert [f.predicate for f in batch] == ['budget_range']
    assert {i['reason'] for i in batch.issues} == {'Unsupported normalized value', 'related_candidate_failed'}
    valid = dict(invalid, value='black')
    batch = _atomic_facts(dict(schema_version=2, facts=[retract, invalid, valid, budget]), messages)
    assert [f.predicate for f in batch] == ['budget_range']
    assert len(batch.issues) == 3  # Neither half of the related update commits.


def test_unknown_budget_does_not_block_independent_scoped_assertion():
    text = '预算100元以内。手机预算100元以内'
    batch = _atomic_facts(dict(schema_version=2, facts=[
        candidate('预算100元以内'), candidate('手机预算100元以内')]), source(text))
    assert len(batch) == 1 and batch[0].qualifiers == {'product_category': 'phone'}
    assert batch.issues[0]['reason'] == 'budget_scope_unknown'


def test_outcomes_are_durable_before_commit_and_deduplicate_after_crash(env):
    store, memories, _ = env
    append(store, '衣服预算100元以内。预算200元以内')
    def extract(messages, existing):
        return _atomic_facts(dict(schema_version=2, facts=[
            candidate('衣服预算100元以内'),
            candidate('预算200元以内', value=dict(min=None, max=200, currency='CNY'))]), messages)
    runtime.build_memory_manager('u', store=store).ltm.fail_after_commit = True
    assert runtime.process_memory_jobs(store, extract)['failed'] == 1
    assert job(store)['through_sequence'] == 0
    assert len(outcomes(store)) == 1
    due(store)
    assert runtime.process_memory_jobs(store, extract)['processed'] == 1
    assert job(store)['through_sequence'] == 1
    assert len(outcomes(store)) == 1 and len(memories['u'].facts) == 1


def test_legacy_schema_upgrade_adds_candidate_outcomes(env):
    from app.support.store import SCHEMA_VERSION, SupportStore
    store, _, _ = env
    append(store, '我买衣服喜欢黑色')
    with store.transaction() as tx:
        tx.execute('DROP TABLE support_memory_candidate_outcomes')
        tx.execute('DELETE FROM support_schema_versions WHERE version=?', (SCHEMA_VERSION,))
        tx.execute("INSERT INTO support_schema_versions VALUES (23,'2026-10-26')")
    reopened = SupportStore(store.dsn)
    assert outcomes(reopened) == []
    assert job(reopened)['requested_sequence'] == 1


def test_real_responses_use_one_model_call_and_keep_outcomes(env, monkeypatch):
    import openai
    store, _, config = env
    config.openai_api_key, config.openai_base_url, config.model_name = 'offline', 'https://unused.example', 'fake'
    calls = []
    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(model='fake', usage=None, choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(CASES[12]['response']), tool_calls=[]))])
    monkeypatch.setattr(openai, 'OpenAI', Client)
    for message in CASES[12]['messages']:
        append(store, message['content'])
    result = runtime.process_memory_jobs(store)
    assert result['processed'] == 1 and result['facts'] == 7
    assert len(calls) == 1 and len(outcomes(store)) == 3


def test_related_failures_in_later_segment_defer_earlier_retraction(env, monkeypatch):
    import openai
    _, _, config = env
    config.openai_api_key, config.openai_base_url, config.model_name = 'offline', 'https://unused.example', 'fake'
    messages = source('不要再按衣服蓝色') + source('衣服现在喜欢黑色')
    parts = [([dict(m, _batch_index=i, _offset=0)], []) for i, m in enumerate(messages)]
    monkeypatch.setattr(runtime, '_extraction_segments', lambda *_: parts)
    items = [candidate(messages[0]['content'], predicate='prefers_color', value='blue',
                       qualifiers={'product_category': 'clothing'}),
             candidate(messages[1]['content'], predicate='prefers_color', value='invalid',
                       qualifiers={'product_category': 'clothing'})]
    items[0]['operation'] = 'RETRACT'
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(openai, 'OpenAI', Client)
    def extract(client, model, part, *args, **kwargs):
        return _atomic_facts(dict(schema_version=2, facts=[items[part[0]['_batch_index']]]), part)
    monkeypatch.setattr(runtime, 'extract_long_term_facts', extract)
    batch = runtime._extract(messages, [])
    assert not batch
    assert {i['source_index'] for i in batch.issues} == {0, 1}
    assert {i['reason'] for i in batch.issues} == {'Unsupported normalized value', 'related_candidate_failed'}


def test_historical_collided_budgets_repair_from_real_sources_idempotently(env, tmp_path, monkeypatch):
    from app.memory.atomic import render_content
    from app.memory.repair import repair_budget_scopes
    store, _, _ = env
    memory = install_real_memory(env, tmp_path, monkeypatch)
    # Reproduce the pre-fix ledger: the original model omitted category, so
    # these four records shared one slot and overwrote each other's validity.
    for number in (3, 4, 11):
        case, sid = CASES[number], f'old{number}'
        store.ensure(sid, 'u')
        for m in case['messages']:
            seq = append(store, m['content'], sid=sid)
            with store.transaction() as tx:
                tx.execute('UPDATE support_events SET created_at=? WHERE session_id=? AND sequence=?',
                           (m['created_at'], sid, seq))
        messages = [dict(m, sequence=i+1) for i, m in enumerate(case['messages'])]
        batch = _atomic_facts(case['response'], messages)
        facts = runtime._ground_facts(batch, messages, sid)
        for fact in facts:
            if fact.predicate == 'budget_range':
                fact.qualifiers = {}
                fact.memory_key = 'preference:budget_range'
                fact.content = render_content(fact.predicate, fact.value, {})
        memory.merge_and_save([f for f in facts if f.predicate == 'budget_range'],
                              source_batch_id=f'{sid}:0:{len(messages)}')
    before = memory.store.list_versions('u')
    assert [f.value['max'] for f in memory.facts] == [3000]
    preview = repair_budget_scopes(memory, store)
    assert len(preview) == 4 and all(r['action'] == 'rescope' for r in preview)
    assert memory.store.list_versions('u') == before
    assert repair_budget_scopes(memory, store, apply=True) == preview
    assert {f.qualifiers['product_category']: f.value['max'] for f in memory.facts} == {
        'phone': 3000, 'clothing': 100}
    assert repair_budget_scopes(memory, store, apply=True) == []
    assert len(memory.history_facts()) == 8  # Preserve four original versions.
    other = LongTermMemory(user_id='another-user', memory_dir=str(tmp_path / 'ltm'), retrieval_enabled=False)
    assert repair_budget_scopes(other, store, apply=True) == []


@pytest.mark.parametrize(('text', 'private', 'reason'), [
    ('预算100元以内', False, 'budget_scope_unknown'),
    ('所有商品预算100元以内', False, None),
    ('衣服预算100元以内', True, 'invalid_source'),
])
def test_history_repair_quarantines_unknown_or_hidden_source_but_keeps_explicit_global(
        env, tmp_path, monkeypatch, text, private, reason):
    from app.memory.repair import repair_budget_scopes
    store, _, _ = env
    memory = install_real_memory(env, tmp_path, monkeypatch)
    seq = append(store, text, private=private)
    fact = MemoryFact(content='用户预算100元以内', category='preference',
        created_at='2026-10-26T16:00:00+00:00', source_type='user_explicit',
        source_span=text, source_session='s', source_event_id=f's:{seq}',
        confidence=.95, schema_version=2, predicate='budget_range',
        value=dict(min=None, max=100, currency='CNY'), memory_key='preference:budget_range')
    # Simulate an old/imported version predating current admission controls.
    memory.source_filter = None
    memory.merge_and_save([fact])
    report = repair_budget_scopes(memory, store, apply=True)
    if reason:
        assert report[0]['reason'] == reason and report[0]['action'] == 'quarantine'
        assert not memory.facts
        assert memory.history_facts()[0].metadata['archived_legacy']
    else:
        assert report == [] and len(memory.facts) == 1 and memory.facts[0].qualifiers == {}
