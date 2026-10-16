"""Memory growth and timeline semantics, without timing-sensitive assertions."""
from datetime import datetime, timedelta, timezone

import pytest

from app.memory.long_term import MemoryFact
from app.memory import reconciliation


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)


def _fact(sequence, value='black', *, category='headphones', **changes):
    observed = (BASE + timedelta(days=sequence)).isoformat()
    source = f's:{sequence}'
    quote = f'我喜欢{value}耳机'
    values = dict(content=quote, category='preference', created_at=observed, updated_at=observed,
                  observed_at=observed, schema_version=2, predicate='prefers_color', value=value,
                  qualifiers={'product_category': category}, source_session='s', source_event_id=source,
                  source_span=quote, source_type='user_explicit', confidence=.95,
                  evidence=[{'source_event_id': source, 'source_span': quote, 'source_session': 's',
                             'observed_at': observed, 'start_offset': 0, 'end_offset': len(quote)}])
    values.update(changes)
    return MemoryFact(**values)


def _active(ledger):
    return [fact for fact in ledger if fact.status == 'active']


def test_confirmation_evidence_and_versions_grow_linearly():
    count = 160
    ledger, operations = reconciliation.reconcile([], [_fact(i) for i in range(1, count + 1)],
                                                  user_id='u', now=NOW, hot_limit=1)
    assert len(ledger) == count
    assert sum(len(fact.evidence) for fact in ledger) == count
    assert operations[0]['operation'] == 'ADD'
    assert all(operation['operation'] == 'MERGE_EVIDENCE' for operation in operations[1:])
    current = _active(ledger)[0]
    assert current.metadata['confirmation_count'] == count
    by_id = {fact.memory_id: fact for fact in ledger}
    events = set()
    while current:
        events.update(evidence['source_event_id'] for evidence in current.evidence)
        current = by_id.get(current.supersedes_memory_id)
    assert len(events) == count  # All source evidence is still reachable.


def test_order_is_computed_once_per_version_during_read_rebuild(monkeypatch):
    ledger, _ = reconciliation.reconcile([], [_fact(i) for i in range(1, 129)], user_id='u', now=NOW)
    original, calls = reconciliation._order, []

    def counted(fact):
        calls.append(fact.memory_id)
        return original(fact)

    monkeypatch.setattr(reconciliation, '_order', counted)
    rebuilt, _ = reconciliation.reconcile(ledger, [], user_id='u', now=NOW)
    assert len(calls) == len(ledger)
    assert len(_active(rebuilt)) == 1
    assert sum(len(fact.evidence) for fact in rebuilt) == len(ledger)


def test_late_conflict_keeps_confirmation_boundary_but_resets_consecutive_count():
    ledger, _ = reconciliation.reconcile([], [_fact(1), _fact(3)], user_id='u', now=NOW)
    assert _active(ledger)[0].metadata['confirmation_count'] == 2
    ledger, _ = reconciliation.reconcile(ledger, [_fact(2, 'white')], user_id='u', now=NOW)
    by_event = {fact.source_event_id: fact for fact in ledger}
    assert _active(ledger)[0].source_event_id == 's:3'
    assert _active(ledger)[0].metadata['confirmation_count'] == 1
    assert by_event['s:3'].supersedes_memory_id == by_event['s:2'].memory_id
    assert sum(len(fact.evidence) for fact in ledger) == 3


def test_same_effective_date_respects_observed_time_before_session_id():
    old = _fact(1, source_event_id='z-session:1', valid_from='2026-11-01T00:00:00+00:00')
    newer = _fact(2, 'white', source_event_id='a-session:1', valid_from=old.valid_from)
    ledger, _ = reconciliation.reconcile([], [newer, old], user_id='u', now=NOW)
    assert [fact.value for fact in _active(ledger)] == ['white']


@pytest.mark.parametrize('same_batch', [True, False])
def test_specific_retraction_cancels_known_future_plan_without_ending_current(same_batch):
    planned = _fact(1, valid_from='2026-11-01T00:00:00+00:00')
    current = _fact(2, 'white')
    retraction = _fact(3, operation='RETRACT')
    ledger, operations = [], []
    batches = [[planned, current, retraction]] if same_batch else [[planned], [current], [retraction]]
    for batch in batches:
        ledger, latest = reconciliation.reconcile(ledger, batch, user_id='u',
                                                   now=datetime(2026, 10, 15, tzinfo=timezone.utc))
        operations.extend(latest)
    assert any(operation['operation'] == 'RETRACT' for operation in operations)
    assert [fact.value for fact in _active(ledger)] == ['white']
    after_activation, _ = reconciliation.reconcile(ledger, [], user_id='u', now=NOW)
    assert [fact.value for fact in _active(after_activation)] == ['white']
    assert next(fact for fact in after_activation if fact.source_event_id == planned.source_event_id).status == 'retracted'
    assert _active(after_activation)[0].valid_to == ''


def test_future_plan_observed_after_retraction_is_a_new_assertion():
    retraction = _fact(2, operation='RETRACT')
    future = _fact(3, valid_from='2026-11-01T00:00:00+00:00')
    ledger, _ = reconciliation.reconcile([], [_fact(1, 'white'), retraction, future], user_id='u', now=NOW)
    assert [fact.value for fact in _active(ledger)] == ['black']


def test_multiple_quotes_for_one_confirmation_are_retained_without_copying_history():
    first, second = _fact(1), _fact(2)
    second.evidence.append({**second.evidence[0], 'start_offset': 50, 'end_offset': 50 + len(second.source_span)})
    ledger, _ = reconciliation.reconcile([], [first, second], user_id='u', now=NOW)
    assert len(ledger[0].evidence) == 1 and len(ledger[1].evidence) == 2
    assert _active(ledger)[0].metadata['confirmation_count'] == 2


@pytest.mark.parametrize('retracted_value', [None, 'black'])
def test_same_event_revocation_does_not_cancel_a_later_announced_plan(retracted_value):
    retraction = _fact(2, value=retracted_value, operation='RETRACT')
    future = _fact(2, valid_from='2026-11-01T00:00:00+00:00')
    retraction.evidence[0].update(start_offset=0, end_offset=10)
    future.evidence[0].update(start_offset=20, end_offset=30)
    ledger, _ = reconciliation.reconcile([], [_fact(1, 'white'), future, retraction], user_id='u', now=NOW)
    assert [fact.value for fact in _active(ledger)] == ['black']
