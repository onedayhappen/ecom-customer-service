"""Durable preference dependencies; validation performs no retrieval or model I/O."""
import json

from app.config.settings import settings
from app.memory.long_term import MemoryFact
from app.memory.ltm_manager import LongTermMemoryManager
from app.memory.retrieval import time_eligible
from app.memory.source_policy import SourcePolicy, memory_safe
from app.memory.v3.validation import sources_valid


def snapshot_current(tx, store, user, snapshot):
    """Recheck exact owned records, including empty-query and forgetting fences."""
    if not settings.memory_enabled or not isinstance(snapshot, dict) or snapshot.get('user_id') != user:
        return False
    from app.memory.v3.routing import enabled
    if not enabled(user, tx=tx):
        return False
    state = tx.one('SELECT revision FROM memory_v3_users WHERE user_id=?', (user,))
    if type(snapshot.get('revision')) is not int or snapshot['revision'] != (state['revision'] if state else 0):
        return False
    facts = snapshot.get('facts')
    if (not isinstance(facts, list) or len(facts) > 20
            or type(snapshot.get('history')) is not bool
            or snapshot.get('as_of') is not None and not isinstance(snapshot['as_of'], str)):
        return False
    policy, seen = SourcePolicy(store, user), set()
    try:
        for record in facts:
            mid = record['memory_id']
            if not isinstance(mid, str) or mid in seen:
                return False
            seen.add(mid)
            row = tx.one('SELECT payload FROM memory_v3_records WHERE user_id=? AND memory_id=?', (user, mid))
            if not row:
                return False
            fact = MemoryFact(**json.loads(row['payload']))
            if (LongTermMemoryManager.fact_record(fact) != record
                    or not memory_safe(fact.content) or not memory_safe(fact.source_span)
                    or fact.category not in {'identity', 'preference', 'behavior', 'other'}
                    or fact.scope not in {'user_preference', 'user_profile'}
                    or fact.source_type != 'user_explicit'
                    or fact.allowed_agents and 'customer_service' not in fact.allowed_agents
                    or not time_eligible(fact, history=snapshot['history'], as_of=snapshot['as_of'])
                    or not sources_valid(policy, fact, tx)):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def dependencies_current(tx, store, user, snapshots):
    return (isinstance(snapshots, list) and 0 < len(snapshots) <= 30
            and all(snapshot_current(tx, store, user, value) for value in snapshots))
