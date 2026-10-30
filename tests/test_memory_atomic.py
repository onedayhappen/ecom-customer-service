# 文件说明：测试原子偏好规范化、语义范围、否定和逐字证据约束。
"""Atomic preferences: conservative semantics, source binding and segmentation."""
import json
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.memory.atomic import AtomicValidationError, canonical_memory_key, ground_time, normalize_candidate, query_slots
from app.memory.extraction import MemoryExtractionError, _atomic_facts, extract_long_term_facts
from app.memory import runtime
from tests.test_long_term_runtime import env, append, job, due, response_client  # noqa: F401


def candidate(quote='我长期喜欢黑色耳机', **changes):
    value = dict(predicate='prefers_color', value='black', qualifiers={'product_category': 'headphones'},
                 polarity='positive', operation='ASSERT', source_index=0, source_quote=quote, confidence=.95)
    value.update(changes)
    return value


def source(text):
    return [dict(role='user', content=text, created_at='2026-10-15T03:00:00+00:00', sequence=7)]


def normalize(item, text=None):
    return normalize_candidate(item, source(text or item['source_quote']))


def test_normalization_is_server_owned_and_category_scoped():
    headphones = normalize(candidate('我长期喜欢漆黑耳机', value='漆黑', qualifiers={'product_category': '耳机'}))
    shoes = normalize(candidate('我的鞋子长期偏好黑色', qualifiers={'product_category': '运动鞋'}))
    assert headphones['value'] == 'black'
    assert headphones['qualifiers'] == {'product_category': 'headphones'}
    assert headphones['memory_key'] != shoes['memory_key']
    assert headphones['memory_key'] == canonical_memory_key('prefers_color', {'product_category': 'headphones'})
    assert '漆黑' not in headphones['content']


@pytest.mark.parametrize('changes', [{'memory_key': 'identity:admin'}, {'user_id': 'other'}, {'observed_at': '2099-01-01'},
                                    {'source_event_id': 'fake'}, {'operation': 'DELETE'}, {'predicate': 'is_admin'},
                                    {'confidence': float('nan')}, {'source_index': -1}, {'value': ['black', 'white']}])
def test_untrusted_or_malformed_fields_are_rejected(changes):
    with pytest.raises(AtomicValidationError):
        normalize(candidate(**changes))


@pytest.mark.parametrize('text', ['这次买耳机我要黑色', '给朋友买黑色耳机', '如果买耳机我会选黑色',
                                '朋友说他喜欢黑色耳机', '我可能喜欢黑色耳机', '黑色白色耳机我都喜欢'])
def test_temporary_gift_hypothetical_reported_and_ambiguous_preferences_are_ignored(text):
    assert normalize(candidate(text)) is None
    # Selecting only the value does not remove the original event's conditions.
    assert normalize(candidate('黑色'), text) is None


def test_negation_is_distinct_from_revocation_and_positive_assertion():
    assert normalize(candidate('我不喜欢黑色耳机')) is None
    negative = normalize(candidate('我不喜欢黑色耳机', polarity='negative'))
    assert negative['polarity'] == 'negative' and negative['operation'] == 'ASSERT'
    revoked = normalize(candidate('我不再喜欢黑色耳机', operation='RETRACT'))
    assert revoked['operation'] == 'RETRACT'
    entire = normalize(candidate('不要再按以前的耳机颜色偏好推荐', operation='RETRACT', value=None))
    assert entire['value'] is None
    assert normalize(candidate('我长期喜欢黑色耳机', operation='RETRACT')) is None


def test_missing_size_system_and_unknown_category_are_not_guessed():
    uncertain = candidate('我平时穿42码的鞋', predicate='wears_size', value='42', qualifiers={'product_category': 'shoes'})
    assert normalize(uncertain) is None
    uncertain['qualifiers']['size_system'] = 'EU'
    assert normalize(uncertain) is None
    explicit = candidate('我平时的鞋穿欧码42', predicate='wears_size', value='42',
                         qualifiers={'product_category': '鞋', 'size_system': '欧码'})
    assert normalize(explicit)['qualifiers'] == {'product_category': 'shoes', 'size_system': 'EU'}


def test_budget_units_and_communication_values_are_normalized():
    budget = candidate('我买耳机长期预算人民币1千到2千元', predicate='budget_range',
                       value={'min': 1000, 'max': 2000, 'currency': '人民币'})
    assert normalize(budget)['value'] == {'min': 1000, 'max': 2000, 'currency': 'CNY'}
    language = candidate('以后请用英文回复我', predicate='preferred_language', value='英语', qualifiers={})
    assert normalize(language)['value'] == 'en'
    channel = candidate('以后请通过邮件联系我', predicate='contact_channel', value='电子邮件', qualifiers={})
    assert normalize(channel)['value'] == 'email'


def test_source_index_cannot_borrow_another_messages_quote():
    with pytest.raises(AtomicValidationError, match='indexed source'):
        normalize_candidate(candidate(source_index=1), [*source('我长期喜欢黑色耳机'), *source('谢谢')])


def test_qualifiers_cannot_be_borrowed_from_another_clause():
    text = '我喜欢黑色耳机和白色鞋子'
    assert normalize(candidate('黑色', qualifiers={'product_category': 'shoes'}), text) is None
    assert normalize(candidate('黑色耳机', qualifiers={'product_category': 'shoes'}), text) is None
    assert normalize(candidate(text, qualifiers={'product_category': 'shoes'}), text) is None
    assert normalize(candidate('黑色耳机'), text)['qualifiers']['product_category'] == 'headphones'
    assert normalize(candidate('白色鞋子', value='white', qualifiers={'product_category': 'shoes'}), text)['value'] == 'white'


def test_size_cannot_be_mixed_with_another_size_system_in_the_event():
    text = '我的鞋穿US 8，欧码是42'
    item = candidate('8', predicate='wears_size', value='8', qualifiers={'product_category': 'shoes', 'size_system': 'EU'})
    assert normalize(item, text) is None
    item['source_quote'] = text
    assert normalize(item, text) is None


def test_single_colour_character_inside_a_brand_is_not_colour_evidence():
    item = candidate('我长期喜欢黑莓手机', qualifiers={'product_category': 'phone'})
    assert normalize(item) is None
    assert normalize(candidate('我长期喜欢纯黑手机', qualifiers={'product_category': 'phone'}))['value'] == 'black'


def test_undated_past_preference_is_not_promoted_to_current():
    text = '以前我喜欢白色耳机，现在我长期喜欢黑色耳机'
    assert normalize(candidate('以前我喜欢白色耳机', value='white'), text) is None
    assert normalize(candidate('白色耳机', value='white'), text) is None
    assert normalize(candidate('我长期喜欢黑色耳机'), text)['value'] == 'black'
    assert normalize(candidate('去年我喜欢白色耳机', value='white', valid_time_quote='去年')) is not None


def test_v2_is_strict_and_legacy_entry_remains_compatible():
    messages = source('我长期喜欢黑色耳机')
    raw = json.dumps({'schema_version': 2, 'facts': [candidate()]}, ensure_ascii=False)
    facts = extract_long_term_facts(response_client(raw), 'fake', messages, None, [], strict=True,
                                    user_sources_only=True, schema_version=2)
    assert facts[0].schema_version == 2 and facts[0].predicate == 'prefers_color'
    assert facts[0].created_at == '' and facts[0].source_event_id == ''
    legacy = json.dumps({'facts': [dict(content='喜欢黑色耳机', category='preference', source_type='user_explicit',
                                       source_span='我长期喜欢黑色耳机', confidence=.9)]}, ensure_ascii=False)
    assert extract_long_term_facts(response_client(legacy), 'fake', messages, None, [], strict=True)[0].schema_version == 1
    with pytest.raises(MemoryExtractionError):
        extract_long_term_facts(response_client(legacy), 'fake', messages, None, [], strict=True, schema_version=2)


def test_event_time_and_exact_quote_offsets_are_bound_by_runtime():
    messages = source('去年我喜欢白色耳机，现在我长期喜欢黑色耳机')
    item = candidate('我长期喜欢黑色耳机', valid_time_quote='现在')
    fact = _atomic_facts({'schema_version': 2, 'facts': [item]}, messages)[0]
    grounded = runtime._ground_facts([fact], messages, 'session')[0]
    assert grounded.source_event_id == 'session:7'
    assert grounded.observed_at == '2026-10-15T03:00:00+00:00'
    assert grounded.valid_from == grounded.observed_at
    evidence = grounded.evidence[0]
    assert messages[0]['content'][evidence['start_offset']:evidence['end_offset']] == fact.source_span
    assert 'source_index' not in grounded.metadata


def test_calendar_precision_uses_source_time_not_worker_clock(monkeypatch):
    monkeypatch.setattr(settings, 'memory_timezone', 'Asia/Shanghai')
    past = ground_time('去年', '2026-03-15T00:00:00+00:00')
    assert past['valid_from'] == '2024-12-31T16:00:00+00:00'
    assert past['valid_to'] == '2025-12-31T16:00:00+00:00'
    assert past['metadata']['valid_time_precision'] == 'year'
    month = ground_time('从2025年3月起', '2026-03-15T00:00:00+00:00')
    assert month['valid_from'] == '2025-02-28T16:00:00+00:00' and month['valid_to'] == ''
    assert month['metadata']['valid_time_precision'] == 'month'
    assert ground_time('很久以前', '2026-03-15T00:00:00+00:00') == {}


def test_query_slot_hints_share_canonical_categories():
    predicates, qualifiers = query_slots('我以前的耳机颜色偏好是什么')
    assert 'prefers_color' in predicates
    assert qualifiers == {'product_category': 'headphones'}


def fake_atomic_api(monkeypatch, *, fail_at=None, callback=None):
    import openai
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create(self, **kwargs):
            payload = json.loads(kwargs['messages'][-1]['content'])
            calls.append(payload)
            if callback:
                callback(len(calls))
            if fail_at == len(calls):
                raise RuntimeError('segment failed')
            facts = [candidate(source_index=index) for index, message in enumerate(payload['messages'])
                     if '我长期喜欢黑色耳机' in message['content']]
            response = json.dumps({'schema_version': 2, 'facts': facts}, ensure_ascii=False)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response, tool_calls=[]))],
                model='fake', usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    monkeypatch.setattr(openai, 'OpenAI', Client)
    return calls


def setup_atomic_env(env):
    store, memories, config = env
    config.openai_api_key = 'offline-test'
    config.openai_base_url = 'https://unused.example'
    config.model_name = 'fake'
    config.memory_extraction_input_tokens = 2500
    return store, memories, config


def test_long_source_segments_cover_all_text_before_single_commit(env, monkeypatch):
    store, memories, config = setup_atomic_env(env)
    text = '历史背景。' * 1800 + '我长期喜欢黑色耳机' + '其他背景。' * 1500
    append(store, text)
    segments = runtime._extraction_segments(source(text), [])
    coverage = [False] * len(text)
    for part, existing in segments:
        assert runtime._input_cost(part, existing) <= config.memory_extraction_input_tokens
        for message in part:
            start = message['_offset']
            assert text[start:start+len(message['content'])] == message['content']
            coverage[start:start+len(message['content'])] = [True] * len(message['content'])
    assert all(coverage)
    calls = fake_atomic_api(monkeypatch)
    assert runtime.process_memory_jobs(store)['processed'] == 1
    assert len(calls) > 1 and len(memories['u'].facts) == 1
    assert job(store)['through_sequence'] == 1
    assert memories['u'].facts[0].source_event_id == 's:1'


def test_middle_segment_failure_does_not_mark_event_processed(env, monkeypatch):
    store, memories, _ = setup_atomic_env(env)
    append(store, '我长期喜欢黑色耳机' + '后续背景。' * 2000)
    fake_atomic_api(monkeypatch, fail_at=2)
    assert runtime.process_memory_jobs(store)['failed'] == 1
    assert job(store)['through_sequence'] == 0
    assert not memories['u'].facts and not memories['u'].receipts
    due(store)
    fake_atomic_api(monkeypatch)
    assert runtime.process_memory_jobs(store)['processed'] == 1
    assert len(memories['u'].facts) == 1


def test_full_source_conditions_survive_segmentation(env, monkeypatch):
    store, memories, _ = setup_atomic_env(env)
    append(store, '这次是给朋友买。' + '背景内容。' * 1300 + '我长期喜欢黑色耳机')
    fake_atomic_api(monkeypatch)
    assert runtime.process_memory_jobs(store)['processed'] == 1
    assert not memories['u'].facts


def test_losing_lease_between_segments_prevents_any_commit(env, monkeypatch):
    store, memories, _ = setup_atomic_env(env)
    append(store, '我长期喜欢黑色耳机' + '后续背景。' * 1800)
    def steal(count):
        if count == 1:
            with store.transaction() as tx:
                tx.execute("UPDATE support_memory_jobs SET lease_token='replacement',version=version+1")
    fake_atomic_api(monkeypatch, callback=steal)
    assert runtime.process_memory_jobs(store)['stale'] == 1
    assert job(store)['through_sequence'] == 0
    assert not memories['u'].facts


@pytest.mark.parametrize('quote,value,accepted', [
    ('耳机预算不超过100美元', {'min': None, 'max': 100, 'currency': 'USD'}, True),
    ('耳机预算不超过100美元', {'min': None, 'max': 100, 'currency': 'CNY'}, False),
    ('耳机预算不超过100元', {'min': 100, 'max': None, 'currency': 'CNY'}, False),
    ('耳机预算至少100元', {'min': None, 'max': 100, 'currency': 'CNY'}, False),
])
def test_budget_currency_and_bound_direction_require_source_support(quote, value, accepted):
    candidate = dict(predicate='budget_range', value=value, qualifiers={'product_category': 'headphones'},
                     polarity='positive', operation='ASSERT', source_index=0, source_quote=quote, confidence=.9)
    normalized = normalize_candidate(candidate, [{'role': 'user', 'content': quote}])
    assert (normalized is not None) == accepted
