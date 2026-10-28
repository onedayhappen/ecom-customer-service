"""Deterministic updates over atomic facts; the ledger keeps all versions.

Event time orders knowledge. Optional valid intervals describe explicitly dated
claims. Working-set limits never decide whether a fact remains valid.
"""
from __future__ import annotations

from copy import deepcopy
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json


def timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def effective_start(fact):
    return timestamp(fact.valid_from or fact.observed_at or fact.created_at) or datetime.min.replace(tzinfo=timezone.utc)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same_value(left, right):
    return left.polarity == right.polarity and _canonical(left.value) == _canonical(right.value)


def _evidence(fact):
    from app.memory.items import _merge_evidence
    values = deepcopy(fact.evidence)
    if fact.source_event_id and fact.source_span and not any(
            e.get('source_event_id') == fact.source_event_id and e.get('source_span') == fact.source_span
            for e in values):
        values.append({"source_event_id": fact.source_event_id, "source_session": fact.source_session,
                       "source_span": fact.source_span, "observed_at": fact.observed_at or fact.created_at})
    return _merge_evidence(values)


def _order(fact):
    # Numeric sequence ordering resolves multiple events at one clock tick.
    sid, _, seq = fact.source_event_id.rpartition(':')
    offsets = [e.get('start_offset', e.get('offset', [0])[0] if isinstance(e.get('offset'), list) and e['offset'] else 0)
               for e in fact.evidence if e.get('source_event_id') == fact.source_event_id
               and e.get('source_span') == fact.source_span]
    offset = max((o for o in offsets if type(o) is int), default=0)
    observed = timestamp(fact.observed_at or fact.updated_at or fact.created_at) or effective_start(fact)
    return effective_start(fact), observed, sid, int(seq) if seq.isdigit() else 0, fact.source_event_id, offset


def _identity(user_id, fact):
    content = _canonical([user_id, fact.memory_key, fact.source_event_id, fact.observed_at,
                          fact.valid_from, fact.valid_to, fact.operation, fact.polarity, fact.value])
    prefix = sha256(user_id.encode()).hexdigest()[:16]
    return 'memu-' + prefix + '-' + sha256(content.encode()).hexdigest()[:24]


def _slot(fact):
    from app.memory.atomic import canonical_memory_key
    qualifiers = {k: v for k, v in fact.qualifiers.items() if k != 'size_system'} if fact.schema_version >= 3 else fact.qualifiers
    key = canonical_memory_key(fact.predicate, qualifiers)
    if fact.polarity == 'negative':
        key += ':exclude=' + _canonical(fact.value)
    return key


def _ends_assertion(change, assertion):
    """Slot replacement, value-specific negation, and whole-slot revocation."""
    if _family(change) != _family(assertion):
        return False
    if change.operation == 'RETRACT':
        return change.value is None or _same_value(change, assertion)
    if change.memory_key == assertion.memory_key:
        return True
    return change.polarity != assertion.polarity and _canonical(change.value) == _canonical(assertion.value)


def _family(fact):
    # Opposite polarities and whole-slot retractions share this timeline.
    qualifiers = {k: v for k, v in fact.qualifiers.items() if k != 'size_system'} if fact.schema_version >= 3 else fact.qualifiers
    return fact.predicate, _canonical(qualifiers)


def _index(facts, order):
    values = sorted(facts, key=order)
    return [order(fact) for fact in values], values


def _neighbor(index, key, *, previous=False):
    keys, values = index
    position = bisect_left(keys, key) - 1 if previous else bisect_right(keys, key)
    return values[position] if 0 <= position < len(values) else None


def _rebuild_timeline(facts, order, now):
    """Indexed neighbors avoid comparing every assertion to every version.

    Regular changes cost O(n log n). Revoking already announced future plans
    additionally inspects matching retractions: O(assertions * retractions)
    in the worst case within this one predicate/qualifier family.
    """
    assertions = [fact for fact in facts if fact.operation == 'ASSERT']
    retractions = defaultdict(list)
    whole = []
    for fact in facts:
        if fact.operation == 'RETRACT':
            (whole if fact.value is None else retractions[(fact.polarity, _canonical(fact.value))]).append(fact)
            fact.status = 'retracted'
            fact.metadata['tier'] = 'cold'

    cancelled = {}
    for fact in assertions:
        matches = whole + retractions[(fact.polarity, _canonical(fact.value))]
        earlier = [change for change in matches
                   if order(change)[1:] >= order(fact)[1:] and order(change)[0] < order(fact)[0]]
        if earlier:
            cancelled[id(fact)] = min(earlier, key=order)

    by_slot, by_value = defaultdict(list), defaultdict(list)
    for fact in assertions:
        # A plan revoked before its start never interrupts the current fact.
        if id(fact) not in cancelled:
            by_slot[fact.memory_key].append(fact)
            by_value[(fact.polarity, _canonical(fact.value))].append(fact)
    slots = {key: _index(values, order) for key, values in by_slot.items()}
    values = {key: _index(items, order) for key, items in by_value.items()}
    retracts = {key: _index(items, order) for key, items in retractions.items()}
    whole_index, empty = _index(whole, order), ([], [])

    for fact in assertions:
        key, value = order(fact), _canonical(fact.value)
        same = slots.get(fact.memory_key, empty)
        opposite = values.get(('negative' if fact.polarity == 'positive' else 'positive', value), empty)
        later = [_neighbor(index, key) for index in (
            same, opposite, whole_index, retracts.get((fact.polarity, value), empty))]
        later.append(cancelled.get(id(fact)))
        successor = min((change for change in later if change is not None), key=order, default=None)
        declared_end = timestamp(fact.metadata.get('declared_valid_to', fact.valid_to))
        end = min([time for time in (declared_end, order(successor)[0] if successor else None) if time], default=None)
        fact.valid_to = end.isoformat() if end else ''
        previous = [_neighbor(index, key, previous=True) for index in (same, opposite)]
        predecessor = max((item for item in previous if item is not None), key=order, default=None)
        fact.supersedes_memory_id = predecessor.memory_id if predecessor else ''
        fact.superseded_by_memory_id = successor.memory_id if successor else ''
        fact.superseded_at = successor.recorded_at if successor else ''
        expiry = timestamp(fact.expires_at)
        if end and end <= now:
            fact.status = 'retracted' if successor and successor.operation == 'RETRACT' else 'superseded'
        elif expiry and expiry <= now:
            fact.status = 'expired'
        elif key[0] > now:
            fact.status = 'scheduled'
        else:
            fact.status = 'active'
        fact.metadata['tier'] = 'cold'

    by_id = {fact.memory_id: fact for fact in assertions}
    for fact in sorted(assertions, key=order):
        previous = by_id.get(fact.supersedes_memory_id)
        expiry = timestamp(previous.expires_at) if previous else None
        declared_end = timestamp(previous.metadata.get('declared_valid_to', '')) if previous else None
        uninterrupted = (previous and _same_value(previous, fact)
                         and previous.superseded_by_memory_id == fact.memory_id
                         and (not expiry or expiry > order(fact)[0])
                         and (not declared_end or declared_end > order(fact)[0]))
        fact.metadata['confirmation_count'] = previous.metadata.get('confirmation_count', 1) + 1 if uninterrupted else 1


def reconcile(existing, incoming, *, user_id, now=None, hot_limit=20):
    """Return a complete immutable-copy ledger and explainable update records."""
    now = now or datetime.now(timezone.utc)
    ledger = deepcopy(existing)
    operations = []
    atomic = [f for f in ledger if f.schema_version >= 2 and not f.metadata.get('reset') and not f.metadata.get('forgotten')
              and not f.metadata.get('archived_legacy') and f.status != 'deleted']
    pending = deepcopy(incoming)
    for candidate in pending:
        if candidate.schema_version < 2:
            raise ValueError('Atomic reconciliation requires schema_version 2')
        if candidate.operation not in {'ASSERT', 'RETRACT'} or candidate.polarity not in {'positive', 'negative'}:
            raise ValueError('Invalid atomic update')
        candidate.memory_key = _slot(candidate)
        candidate.observed_at = candidate.observed_at or candidate.updated_at or candidate.created_at
        if not timestamp(candidate.observed_at) or not candidate.source_event_id:
            raise ValueError('Atomic fact requires a source event and timestamp')
        candidate.evidence = _evidence(candidate)
        candidate.metadata['declared_valid_to'] = candidate.valid_to
        candidate.recorded_at = now.isoformat()

    orders = {id(fact): _order(fact) for fact in atomic + pending}
    order = lambda fact: orders[id(fact)]
    families, pending_families = defaultdict(list), defaultdict(list)
    for fact in atomic:
        families[_family(fact)].append(fact)
    for fact in pending:
        pending_families[_family(fact)].append(fact)
    for candidate in sorted(pending, key=order):
        family = families[_family(candidate)]
        slot = [f for f in family if f.memory_key == candidate.memory_key
                or (f.operation == 'RETRACT' and f.value is None
                    and f.predicate == candidate.predicate and f.qualifiers == candidate.qualifiers)]
        duplicate = next((f for f in slot if f.source_event_id == candidate.source_event_id
                          and f.operation == candidate.operation and _same_value(f, candidate)
                          and f.valid_from == candidate.valid_from), None)
        if duplicate:
            operations.append({'operation': 'NOOP', 'memory_id': duplicate.memory_id, 'reason': 'duplicate_source'})
            continue
        if any(order(f) == order(candidate) for f in slot):
            operations.append({'operation': 'NOOP', 'reason': 'ambiguous_source_order'})
            continue
        prior = [f for f in slot if order(f) <= order(candidate)]
        previous = max(prior, key=order) if prior else None
        next_events = [f for f in slot if order(f) > order(candidate)]
        if candidate.operation == 'RETRACT' and previous and previous.operation == 'ASSERT' and (
                candidate.value is not None and not _same_value(previous, candidate)):
            # A specific cancellation may target an announced future plan,
            # while the currently effective preference has another value.
            known_future = any(f.operation == 'ASSERT' and _same_value(f, candidate)
                               and order(f)[1:] <= order(candidate)[1:] and order(f)[0] > order(candidate)[0]
                               for f in family + pending_families[_family(candidate)])
            if not known_future:
                operations.append({'operation': 'NOOP', 'memory_id': previous.memory_id, 'reason': 'different_value'})
                continue
        # Reconfirm only an uninterrupted, still valid version. An old source
        # arriving after a newer update cannot refresh or overwrite that update.
        expiry = timestamp(previous.expires_at) if previous else None
        declared_end = timestamp(previous.metadata.get('declared_valid_to', '')) if previous else None
        if (candidate.operation == 'ASSERT' and previous and previous.operation == 'ASSERT'
                and _same_value(previous, candidate) and not next_events
                and (not expiry or expiry > effective_start(candidate))
                and (not declared_end or declared_end > effective_start(candidate))):
            # Keep each independent confirmation as a version boundary. A
            # late conflicting event can then be inserted between confirmations
            # without erasing the most recent confirmed preference.
            # Each physical version stores its own source quotes. Earlier
            # evidence remains in the version chain, avoiding quadratic copies.
            update_operation = 'MERGE_EVIDENCE'
        else:
            update_operation = None
        candidate.version = max((f.version for f in slot), default=0) + 1
        candidate.memory_id = _identity(user_id, candidate)
        if previous and previous.operation == 'ASSERT' and candidate.operation == 'ASSERT':
            candidate.supersedes_memory_id = previous.memory_id
        ledger.append(candidate)
        atomic.append(candidate)
        family.append(candidate)
        operation = update_operation or ('RETRACT' if candidate.operation == 'RETRACT' else
                     'SUPERSEDE' if previous and previous.operation == 'ASSERT' else 'ADD')
        candidate.metadata['update_operation'] = operation
        operations.append({'operation': operation, 'memory_id': candidate.memory_id,
                           'memory_key': candidate.memory_key, 'source_event_id': candidate.source_event_id})

    # Recompute only related predicate/qualifier families with cached order.
    for family in families.values():
        _rebuild_timeline(family, order, now)
    active = sorted((f for f in atomic if f.status == 'active'),
                    key=lambda f: (f.metadata.get('confirmation_count', 1), len(f.evidence),
                                   timestamp(f.updated_at) or order(f)[0], f.memory_id), reverse=True)
    for fact in active[:hot_limit]:
        fact.metadata['tier'] = 'hot'
    return ledger, operations
