"""New memory commands through authenticated events, jobs, archives and reloads."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.config.settings import settings
from app.memory.runtime import build_memory_manager, process_memory_jobs
from app.memory.v3.jobs import archive, archive_status
from app.memory.v3.context import load
from app.memory.v3.models import Conflict
from app.memory.v3.repository import Repository
from app.memory.v3.service import MemoryService
from app.support.store import SupportStore, SupportError
from tests.api_fixtures import api  # noqa: F401


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name, value in dict(memory_engine='v3', memory_enabled=True, memory_storage_backend='json',
        memory_dir=str(tmp_path / 'legacy'), memory_redis_url='', memory_retrieval_enabled=False,
        memory_extraction_debounce_seconds=0, memory_extraction_input_tokens=12000,
        short_term_memory_enabled=False, semantic_memory_retrieval_top_k=20,
        memory_retrieval_min_score=.01).items():
        monkeypatch.setattr(settings, name, value)
    return SupportStore('sqlite:///' + (tmp_path / 'support.db').as_posix())


def message(store, text, sid='s', user='u'):
    store.ensure(sid, user)
    with store.transaction() as tx:
        c = store._control(tx, sid, user)
        seq = store._event(tx, c, 'message', user, dict(role='user', content=text))
    return f'{sid}:{seq}'


def change(mid, quote, value='black', predicate='prefers_color', action='UPDATE', group='g', **extras):
    return dict(action=action, group_id=group, predicate=predicate, value=value,
        scope=dict(product_category='clothing'), primary_message_id=mid,
        evidence=[dict(message_id=mid, quote=quote)], **extras)


def run(store, changes):
    return process_memory_jobs(store, extract_fn=lambda *_: dict(schema_version=3, changes=changes))


def current(store, user='u'):
    return build_memory_manager(user, store=store).ltm.facts


def test_update_forget_archive_restart_and_late_replay(store):
    old = message(store, '以后衣服喜欢蓝色，尺码M')
    size = change(old, '尺码M', 'M', 'wears_size')
    size['scope']['size_system'] = 'INT'
    size['evidence'].append(dict(message_id=old, quote='衣服'))
    assert run(store, [change(old, '以后衣服喜欢蓝色', 'blue'), size])['facts'] == 2
    warm = build_memory_manager('u', store=store)
    new = message(store, '以后衣服按L码看吧，现在更喜欢黑的，蓝色M那套老喜好改一下。', 'next')
    size = change(new, '以后衣服按L码看吧', 'L', 'wears_size')
    size['scope']['size_system'] = 'INT'
    color = change(new, '现在更喜欢黑的')
    color['evidence'].append(dict(message_id=new, quote='衣服'))
    assert run(store, [size, color])['facts'] == 2
    assert {f.value for f in current(store)} == {'black', 'L'}
    # Background evidence survives separate worker batches in this session.
    forget = message(store, '旧的蓝色偏好忘掉吧', 'next')
    command = change(forget, '旧的蓝色偏好忘掉吧', 'blue', action='FORGET')
    command['evidence'].append(dict(message_id=new, quote='衣服'))
    barrier = archive(store, 'u', 'next')
    assert run(store, [command])['facts'] == 1
    assert archive_status(store, 'u', barrier['archive_id'])['status'] == 'completed'
    assert {f.value for f in warm.retrieve_relevant_long_term_memory('衣服颜色尺码', history=True)} <= {'black', 'M', 'L'}
    assert 'blue' not in {f.value for f in Repository(store, 'u').read(history=True)}
    assert {f.value for f in current(store)} == {'black', 'L'}
    # Reprocessing the original event cannot resurrect a forgotten version.
    with store.transaction() as tx:
        tx.execute("UPDATE memory_v3_jobs SET through_sequence=0,status='pending' WHERE session_id='s'")
        tx.execute("DELETE FROM memory_v3_receipts WHERE session_id='s'")
    run(store, [change(old, '以后衣服喜欢蓝色', 'blue')])
    assert 'blue' not in {f.value for f in Repository(store, 'u').read(history=True)}


def test_cross_turn_evidence_and_unknown_size_system(store):
    earlier = message(store, '说下我平时穿的鞋')
    assert run(store, [])['processed'] == 1
    mid = message(store, '日常38，跑步39，以后就按这个记')
    changes = []
    for quote, value, usage in [('日常38', '38', 'daily'), ('跑步39', '39', 'running')]:
        item = change(mid, quote, value, 'wears_size', group=usage)
        item['scope'] = dict(product_category='shoes', usage=usage, size_system='unknown')
        item['evidence'].append(dict(message_id=earlier, quote='鞋'))
        changes.append(item)
    assert run(store, changes)['facts'] == 2
    assert {(f.value, f.qualifiers['usage'], f.qualifiers['size_system']) for f in current(store)} == {
        ('38', 'daily', 'unknown'), ('39', 'running', 'unknown')}


def test_invalid_group_does_not_half_apply_and_independent_group_survives(store):
    mid = message(store, '以后衣服喜欢蓝色，手机喜欢黑色')
    bad = change(mid, '以后衣服喜欢蓝色', 'L', 'wears_size')
    bad['scope']['size_system'] = 'INT'
    phone = change(mid, '手机喜欢黑色', group='phone')
    phone['scope']['product_category'] = 'phone'
    result = run(store, [change(mid, '以后衣服喜欢蓝色', 'blue'), bad, phone])
    assert result['partial'] == 1 and result['facts'] == 1
    assert [(f.value, f.qualifiers['product_category']) for f in current(store)] == [('black', 'phone')]


@pytest.mark.parametrize('text', ['这次衣服买黑色', '给朋友买黑色衣服', '我不喜欢黑色衣服', '以前喜欢黑色衣服'])
def test_narrow_quote_cannot_erase_conditions_negation_or_time(store, text):
    mid = message(store, text)
    item = change(mid, '黑色')
    item['evidence'].append(dict(message_id=mid, quote='衣服'))
    assert run(store, [item])['partial'] == 1
    assert current(store) == []


def test_ownership_targets_and_deleted_context_sources(store):
    mid = message(store, '以后衣服喜欢黑色')
    assert run(store, [change(mid, '以后衣服喜欢黑色')])['facts'] == 1
    memory_id = current(store)[0].memory_id
    other = message(store, '以后衣服喜欢蓝色', 'other', 'bob')
    assert run(store, [change(other, '以后衣服喜欢蓝色', 'blue', target_ids=[memory_id])])['partial'] == 1
    assert current(store, 'bob') == []
    from app.memory.maintenance import forget_source, process_memory_invalidations
    forget_source(store, 'u', 's', 1)
    assert current(store) == []
    assert process_memory_invalidations(store)['failed'] == 0
    with pytest.raises(SupportError):
        archive(store, 'bob', 's')


def test_forgotten_value_requires_explicit_restoration(store):
    mid = message(store, '以后衣服喜欢蓝色')
    run(store, [change(mid, '以后衣服喜欢蓝色', 'blue')])
    mid = message(store, '忘掉衣服蓝色偏好')
    run(store, [change(mid, '忘掉衣服蓝色偏好', 'blue', action='FORGET')])
    mid = message(store, '衣服喜欢蓝色')
    assert run(store, [change(mid, '衣服喜欢蓝色', 'blue')])['partial'] == 1
    assert current(store) == []
    mid = message(store, '现在衣服又喜欢蓝色，重新记住')
    # The explicit request authorizes restoration even if the model omits its flag.
    assert run(store, [change(mid, '现在衣服又喜欢蓝色', 'blue')])['facts'] == 1
    assert [f.value for f in current(store)] == ['blue']


def test_model_failures_are_bounded_and_archive_exposes_failure(store):
    message(store, '衣服改黑色')
    checkpoint = archive(store, 'u', 's')
    def broken(*_):
        raise TimeoutError('provider detail must not leak')
    for _ in range(3):
        assert process_memory_jobs(store, extract_fn=broken)['failed'] == 1
        with store.transaction() as tx:
            tx.execute('UPDATE memory_v3_jobs SET available_at=0')
    state = archive_status(store, 'u', checkpoint['archive_id'])
    assert state['status'] == 'failed' and state['processed_sequence'] == 0
    assert state['last_error'] == 'TimeoutError'
    assert process_memory_jobs(store, extract_fn=broken)['failed'] == 0


def test_atomic_receipt_checkpoint_rollback_and_idempotency(store, monkeypatch):
    mid = message(store, '以后衣服喜欢黑色')
    build_memory_manager('u', store=store)
    original = Repository.write
    def broken(self, tx, facts):
        original(self, tx, facts)
        raise RuntimeError('disk failure')
    monkeypatch.setattr(Repository, 'write', broken)
    assert run(store, [change(mid, '以后衣服喜欢黑色')])['failed'] == 1
    monkeypatch.setattr(Repository, 'write', original)
    with store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM memory_v3_records')['n'] == 0
        assert tx.one("SELECT COUNT(*) AS n FROM memory_v3_receipts WHERE session_id!=''")['n'] == 0
        tx.execute('UPDATE memory_v3_jobs SET available_at=0')
    assert run(store, [change(mid, '以后衣服喜欢黑色')])['facts'] == 1
    assert run(store, [change(mid, '以后衣服喜欢黑色')])['processed'] == 0


def test_changed_snapshot_cannot_overwrite_concurrent_user_update(store):
    mid = message(store, '以后衣服喜欢黑色')
    manager = build_memory_manager('u', store=store)
    revision = manager.repository.revision()
    with store.transaction(read_only=True) as tx:
        job = tx.one('SELECT * FROM memory_v3_jobs')
    messages, _, through = load(store, job, [])
    assert run(store, [change(mid, '以后衣服喜欢黑色')])['facts'] == 1
    with pytest.raises(Conflict):
        MemoryService(store, 'u').apply(dict(changes=[change(mid, '以后衣服喜欢黑色')]), messages,
            batch_id='stale', sid='s', through=through, expected_revision=revision)


def test_ten_archived_sessions_with_ten_turns_each(store):
    # All messages are real support events; deterministic planner keeps this an
    # offline lifecycle regression, not a claim of live-model conversation QA.
    for session in range(10):
        sid = f'round-{session}'
        for turn in range(10):
            text = '以后衣服喜欢' + ('蓝色' if session % 2 == 0 else '黑色') if turn == 0 else f'第{turn}轮，只问问，不改偏好'
            mid = message(store, text, sid)
            changes = [change(mid, text, 'blue' if session % 2 == 0 else 'black')] if turn == 0 else []
            assert run(store, changes)['failed'] == 0
        checkpoint = archive(store, 'u', sid)
        run(store, [])
        assert archive_status(store, 'u', checkpoint['archive_id'])['status'] == 'completed'
        assert [f.value for f in current(store)] == ['blue' if session % 2 == 0 else 'black']


def test_chinese_budget_and_topic_continuation_forgetting(store):
    mid = message(store, '衣服预算一百五十元以内')
    budget = change(mid, '衣服预算一百五十元以内',
                    dict(min=0, max=150, currency='CNY'), 'budget_range')
    assert run(store, [budget])['facts'] == 1
    mid = message(store, '以后衣服喜欢蓝色')
    run(store, [change(mid, '以后衣服喜欢蓝色', 'blue')])
    message(store, '衣服旧的喜好清一下')
    mid = message(store, '旧的蓝色偏好忘掉吧')
    assert run(store, [change(mid, '旧的蓝色偏好忘掉吧', 'blue', action='FORGET')])['facts'] == 1
    assert not any(f.value == 'blue' for f in Repository(store, 'u').read(history=True))
    # A topic with multiple categories must not be completed speculatively.
    message(store, '衣服和鞋的喜好清一下')
    mid = message(store, '旧的黑色偏好忘掉吧')
    assert run(store, [change(mid, '旧的黑色偏好忘掉吧', action='FORGET')])['partial'] == 1


def test_later_unit_clarification_and_invalidated_auxiliary_source(store):
    mid = message(store, '鞋子日常穿38')
    later = message(store, '我说的是欧码')
    item = change(mid, '鞋子日常穿38', '38', 'wears_size')
    item['scope'] = dict(product_category='shoes', usage='daily', size_system='EU')
    item['evidence'].append(dict(message_id=later, quote='欧码'))
    assert run(store, [item])['facts'] == 1
    assert current(store)[0].qualifiers['size_system'] == 'EU'
    from app.memory.maintenance import forget_source
    forget_source(store, 'u', 's', 2)
    assert current(store) == []


def test_unit_change_is_same_preference_and_old_value_does_not_return(store):
    mid = message(store, '日常鞋穿38')
    item = change(mid, '日常鞋穿38', '38', 'wears_size')
    item['scope'] = dict(product_category='shoes', usage='daily', size_system='unknown')
    run(store, [item])
    mid = message(store, '日常鞋改成欧码39')
    item = change(mid, '日常鞋改成欧码39', '39', 'wears_size')
    item['scope'] = dict(product_category='shoes', usage='daily', size_system='EU')
    run(store, [item])
    assert [(f.value, f.qualifiers['size_system']) for f in current(store)] == [('39', 'EU')]


def test_migration_is_audited_idempotent_and_rollback_stays_v3(store, monkeypatch):
    from types import SimpleNamespace
    from app.memory.v3.models import Change
    from app.memory.v3.validation import validate
    from app.memory.reconciliation import reconcile
    mid = message(store, '以后衣服喜欢黑色')
    with store.transaction(read_only=True) as tx:
        job = tx.one('SELECT * FROM memory_v3_jobs')
    messages, _, _ = load(store, job, [])
    fact = validate(Change.model_validate(change(mid, '以后衣服喜欢黑色')), messages).fact
    fact.schema_version = 2
    facts, _ = reconcile([], [fact], user_id='u')
    unsupported = deepcopy(facts[0])
    unsupported.schema_version = 1
    repo = Repository(store, 'u')
    factory = lambda: SimpleNamespace(ltm=SimpleNamespace(history_facts=lambda: [*facts, unsupported]))
    repo.migrate(factory)
    revision = repo.revision()
    repo.migrate(lambda: pytest.fail('migration must not read legacy twice'))
    assert repo.revision() == revision
    with store.transaction(read_only=True) as tx:
        report = json.loads(tx.one("SELECT result FROM memory_v3_receipts WHERE batch_id='migration:v2'")['result'])
    assert report['imported'] == 1 and report['skipped'] == {'legacy_unstructured_or_reset': 1}
    monkeypatch.setattr(settings, 'memory_engine', 'legacy')
    assert type(build_memory_manager('u', store=store)).__module__ == 'app.memory.v3.manager'
    new = message(store, '忘掉衣服黑色偏好')
    monkeypatch.setattr('app.memory.v3.extractor.extract',
                        lambda *_: dict(changes=[change(new, '忘掉衣服黑色偏好', action='FORGET')]))
    monkeypatch.setattr(settings, 'openai_api_key', 'test-memory-model-stub')
    assert process_memory_jobs(store)['facts'] == 1
    assert current(store) == []


def test_archive_barrier_empty_and_crossing_partial_receipt(store):
    store.ensure('empty', 'u')
    assert archive(store, 'u', 'empty')['status'] == 'completed'
    mid = message(store, '以后衣服喜欢黑色')
    checkpoint = archive(store, 'u', 's')
    message(store, '嗯')
    assert run(store, [change(mid, '以后衣服喜欢黑色', 'blue')])['partial'] == 1
    state = archive_status(store, 'u', checkpoint['archive_id'])
    assert state['processed_sequence'] > state['through_sequence']
    assert state['status'] == 'completed_with_issues'


def test_archive_api_authentication_and_owner_isolation(api, store, monkeypatch):
    from uuid import uuid4
    client, headers, *_ = api
    sid = str(uuid4())
    monkeypatch.setattr(settings, 'memory_enabled', True)
    monkeypatch.setattr('app.api.memory.get_store', lambda: store)
    store.ensure(sid, 'user')
    path = f'/api/v1/sessions/{sid}/memory-archive'
    assert client.post(path).status_code == 401
    assert client.post(path, headers=headers('other')).status_code in (403, 404)
    result = client.post(path, headers=headers())
    assert result.status_code == 202
    status_path = '/api/v1/memory-archives/' + result.json()['archive_id']
    assert client.get(status_path, headers=headers()).json()['status'] == 'completed'
    assert client.get(status_path, headers=headers('other')).status_code == 404


def test_conflicting_values_from_identical_evidence_are_not_reported_as_saved(store):
    mid = message(store, '衣服喜欢黑色还是蓝色我没决定')
    quote = '衣服喜欢黑色还是蓝色我没决定'
    result = run(store, [change(mid, quote, 'black', group='black'),
                         change(mid, quote, 'blue', group='blue')])
    assert result['partial'] == 1 and result['facts'] == 0
    assert current(store) == []
