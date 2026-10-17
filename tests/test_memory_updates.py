"""Atomic version updates preserve event order, constraints and cold facts."""
from datetime import datetime, timedelta, timezone

import pytest

from app.memory.atomic import render_content
from app.memory.long_term import LongTermMemory, MemoryFact
from app.memory.reconciliation import reconcile


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def fact(value='white', *, day=1, sequence=None, category='headphones', polarity='positive',
         operation='ASSERT', confidence=.9, predicate='prefers_color', **changes):
    observed = datetime(2026, 10, day, tzinfo=timezone.utc).isoformat()
    qualifiers = {'product_category': category}
    values = dict(content=render_content(predicate, value, qualifiers, polarity, operation),
                  category='preference', created_at=observed, updated_at=observed, observed_at=observed,
                  source_session='s', source_event_id=f's:{sequence if sequence is not None else day}',
                  source_span=f'用户原话 {day}: {value}', source_type='user_explicit', confidence=confidence,
                  schema_version=2, predicate=predicate, value=value, qualifiers=qualifiers,
                  polarity=polarity, operation=operation)
    values.update(changes)
    return MemoryFact(**values)


def active(ledger):
    return [item for item in ledger if item.status == 'active']


def test_new_explicit_preference_wins_over_higher_old_model_confidence():
    ledger, _ = reconcile([], [fact('white', day=1, confidence=.99)], user_id='u', now=NOW)
    old_id = ledger[0].memory_id
    updated, operations = reconcile(ledger, [fact('black', day=2, confidence=.61)], user_id='u', now=NOW)
    assert [item.value for item in active(updated)] == ['black']
    assert operations[-1]['operation'] == 'SUPERSEDE'
    assert active(updated)[0].supersedes_memory_id == old_id
    assert ledger[0].status == 'active'  # Caller-owned snapshots are unchanged.


def test_same_slot_different_product_categories_coexist():
    ledger, operations = reconcile([], [fact('white', category='shoes'), fact('black', category='headphones')],
                                   user_id='u', now=NOW)
    assert len(active(ledger)) == 2
    assert len({f.memory_key for f in ledger}) == 2
    assert [item['operation'] for item in operations] == ['ADD', 'ADD']


def test_confirmation_survives_late_intermediate_change_and_keeps_full_version_chain():
    ledger, _ = reconcile([], [fact('white', day=1)], user_id='u', now=NOW)
    ledger, operations = reconcile(ledger, [fact('white', day=3)], user_id='u', now=NOW)
    assert operations[-1]['operation'] == 'MERGE_EVIDENCE'
    ledger, _ = reconcile(ledger, [fact('black', day=2)], user_id='u', now=NOW)
    by_event = {f.source_event_id: f for f in ledger}
    assert active(ledger)[0].source_event_id == 's:3'
    assert active(ledger)[0].value == 'white'
    assert by_event['s:1'].valid_to == by_event['s:2'].observed_at
    assert by_event['s:2'].valid_to == by_event['s:3'].observed_at
    assert by_event['s:3'].supersedes_memory_id == by_event['s:2'].memory_id
    assert by_event['s:2'].supersedes_memory_id == by_event['s:1'].memory_id


def test_retraction_tombstone_stays_in_history_and_new_assertion_can_restore_slot():
    ledger, _ = reconcile([], [fact('white', day=1), fact(None, day=2, operation='RETRACT')], user_id='u', now=NOW)
    assert not active(ledger)
    assert all(f.status == 'retracted' for f in ledger)
    tombstone_id = next(f.memory_id for f in ledger if f.operation == 'RETRACT')
    ledger, operations = reconcile(ledger, [fact('black', day=3)], user_id='u', now=NOW)
    assert [f.value for f in active(ledger)] == ['black']
    assert any(f.memory_id == tombstone_id and f.status == 'retracted' for f in ledger)
    assert operations[-1]['operation'] == 'ADD'


def test_retracting_a_different_value_leaves_current_preference_alone():
    ledger, operations = reconcile([], [fact('white', day=1), fact('black', day=2, operation='RETRACT')],
                                   user_id='u', now=NOW)
    assert [f.value for f in active(ledger)] == ['white']
    assert operations[-1]['operation'] == 'NOOP'


def test_negative_constraint_coexists_with_a_different_positive_value():
    ledger, _ = reconcile([], [fact('white', day=1), fact('black', day=2, polarity='negative')],
                           user_id='u', now=NOW)
    assert {(f.value, f.polarity) for f in active(ledger)} == {('white', 'positive'), ('black', 'negative')}


@pytest.mark.parametrize('first,second', [('positive', 'negative'), ('negative', 'positive')])
def test_new_opposite_polarity_closes_same_value_conflict(first, second):
    ledger, _ = reconcile([], [fact('black', day=1, polarity=first), fact('black', day=2, polarity=second)],
                           user_id='u', now=NOW)
    assert [(f.value, f.polarity) for f in active(ledger)] == [('black', second)]
    assert next(f for f in ledger if f.source_event_id == 's:1').valid_to == '2026-10-02T00:00:00+00:00'


def test_whole_slot_retraction_also_clears_negative_constraints():
    ledger, _ = reconcile([], [fact('white', day=1), fact('black', day=2, polarity='negative'),
                              fact(None, day=3, operation='RETRACT')], user_id='u', now=NOW)
    assert not active(ledger)


def test_hot_limit_keeps_valid_cold_facts_searchable_in_ledger():
    categories = ['headphones', 'shoes', 'clothing', 'phone', 'computer', 'cosmetics', 'food', 'appliance']
    inputs = [fact('black', category=category, sequence=index + 1) for index, category in enumerate(categories)]
    ledger, _ = reconcile([], inputs, user_id='u', now=NOW, hot_limit=2)
    assert len(active(ledger)) == 8
    assert sum(f.metadata['tier'] == 'hot' for f in ledger) == 2
    assert sum(f.metadata['tier'] == 'cold' for f in ledger) == 6
    later, _ = reconcile(ledger, [], user_id='u', now=NOW, hot_limit=1)
    assert len(active(later)) == 8


def test_explicit_historical_and_future_intervals_do_not_override_current_early():
    past = fact('red', day=5, valid_from='2025-01-01T00:00:00+00:00', valid_to='2026-01-01T00:00:00+00:00')
    current = fact('white', day=1)
    future = fact('black', day=6, valid_from='2027-01-01T00:00:00+00:00')
    ledger, _ = reconcile([], [current, past, future], user_id='u', now=NOW)
    assert [f.value for f in active(ledger)] == ['white']
    assert next(f for f in ledger if f.value == 'black').status == 'scheduled'
    assert next(f for f in ledger if f.value == 'red').valid_to == '2026-01-01T00:00:00+00:00'
    activated, _ = reconcile(ledger, [], user_id='u', now=datetime(2027, 1, 2, tzinfo=timezone.utc))
    assert [f.value for f in active(activated)] == ['black']


def test_same_event_can_hold_past_and_current_versions():
    old = fact('white', day=3, sequence=3, valid_from='2025-01-01T00:00:00+00:00', valid_to='2026-01-01T00:00:00+00:00')
    current = fact('black', day=3, sequence=3)
    ledger, _ = reconcile([], [old, current], user_id='u', now=NOW)
    assert len(ledger) == 2
    assert [f.value for f in active(ledger)] == ['black']


def test_same_event_correction_uses_original_quote_order():
    before, after = fact('white', day=3, sequence=3), fact('black', day=3, sequence=3)
    for item, start in ((before, 0), (after, 20)):
        item.evidence = [dict(source_event_id=item.source_event_id, source_session='s', source_span=item.source_span,
                              observed_at=item.observed_at, start_offset=start, end_offset=start+len(item.source_span))]
    # Candidate JSON order is not trustworthy; actual source offsets order it.
    ledger, _ = reconcile([], [after, before], user_id='u', now=NOW)
    assert [f.value for f in active(ledger)] == ['black']


def test_revocation_cancels_an_already_known_future_plan():
    scheduled = fact('black', day=1, valid_from='2026-11-01T00:00:00+00:00')
    revoked = fact(None, day=2, operation='RETRACT')
    ledger, _ = reconcile([], [scheduled, revoked], user_id='u', now=NOW)
    later, _ = reconcile(ledger, [], user_id='u', now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert not active(later)
    assert next(f for f in later if f.operation == 'ASSERT').status == 'retracted'


def test_exact_source_replay_adds_no_version_or_evidence():
    original = fact('white')
    ledger, _ = reconcile([], [original], user_id='u', now=NOW)
    repeated, operations = reconcile(ledger, [original], user_id='u', now=NOW)
    assert len(repeated) == 1
    assert repeated[0].evidence == ledger[0].evidence
    assert operations == [{'operation': 'NOOP', 'memory_id': ledger[0].memory_id, 'reason': 'duplicate_source'}]


def test_evidence_offsets_do_not_duplicate_a_single_origin_quote():
    original = fact('white')
    original.evidence = [dict(source_event_id=original.source_event_id, source_session='s',
                             source_span=original.source_span, observed_at=original.observed_at,
                             start_offset=0, end_offset=len(original.source_span))]
    ledger, _ = reconcile([], [original], user_id='u', now=NOW)
    assert len(ledger[0].evidence) == 1
    assert 'start_offset' in ledger[0].evidence[0]


def test_json_roundtrip_retains_versions_cold_facts_and_batch_receipts(tmp_path):
    now = datetime.now(timezone.utc)
    observed = (now - timedelta(days=2)).isoformat()
    memory = LongTermMemory(user_id='u', memory_dir=str(tmp_path), retrieval_enabled=False, hot_limit=1, max_facts=1)
    originals = [fact('white', category='headphones', sequence=1, created_at=observed, updated_at=observed, observed_at=observed),
                 fact('black', category='shoes', sequence=2, created_at=observed, updated_at=observed, observed_at=observed)]
    assert memory.merge_and_save(originals, source_batch_id='s:0:2')
    assert not memory.merge_and_save(originals, source_batch_id='s:0:2')
    newer = (now - timedelta(days=1)).isoformat()
    change = fact('blue', category='headphones', sequence=3, created_at=newer, updated_at=newer, observed_at=newer)
    assert memory.merge_and_save([change], source_batch_id='s:2:3')
    loaded = LongTermMemory(user_id='u', memory_dir=str(tmp_path), retrieval_enabled=False, hot_limit=1, max_facts=1)
    loaded.load()
    assert len(loaded.facts) == 2
    history = loaded.history_facts()
    assert len(history) == 3
    assert len([f for f in history if f.status == 'superseded']) == 1
    assert len([f for f in loaded.facts if f.metadata['tier'] == 'cold']) == 1
    assert not loaded.merge_and_save(originals, source_batch_id='s:0:2')
    assert {f.value for f in loaded.facts} == {'black', 'blue'}


def test_explicit_future_preference_does_not_expire_before_activation(tmp_path):
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=180)
    observed = now.isoformat()
    scheduled = fact('black', created_at=observed, updated_at=observed, observed_at=observed, valid_from=future.isoformat())
    memory = LongTermMemory(user_id='u', memory_dir=str(tmp_path), retrieval_enabled=False, preference_ttl_days=90)
    memory.merge_and_save([scheduled], source_batch_id='s:0:1')
    stored = memory.history_facts()
    activated, _ = reconcile(stored, [], user_id='u', now=future + timedelta(days=1))
    assert [f.value for f in active(activated)] == ['black']
