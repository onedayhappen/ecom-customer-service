"""Explicit, idempotent maintenance for pre-fix unscoped budget versions.

No model calls and no online-read migration. The default is a dry run; original
versions remain in the ledger, and only authenticated source evidence can
produce a replacement. Unknown scope is quarantined instead of guessed.
"""
from copy import deepcopy
from datetime import datetime, timezone

from app.memory.extraction import _atomic_facts
from app.memory.reconciliation import reconcile
from app.memory.runtime import _ground_facts
from app.memory.source_policy import SourcePolicy


def repair_budget_scopes(memory, support_store, *, apply=False):
    policy = SourcePolicy(support_store, memory.user_id)
    reports, replacements = [], []
    # Reload and commit under the same per-user lock as normal memory writes.
    with memory.store.transaction(memory.user_id):
        ledger = memory.history_facts()
        for fact in ledger:
            if (fact.schema_version != 2 or fact.predicate != 'budget_range' or fact.qualifiers
                    or fact.metadata.get('reset') or fact.metadata.get('archived_legacy')
                    or fact.status == 'deleted'):
                continue
            sid, _, sequence = fact.source_event_id.rpartition(':')
            with support_store.transaction(read_only=True) as tx:
                allowed = policy.allows(fact, tx=tx)
                original = policy.source(tx, sid, int(sequence)) if allowed else None
                event = tx.one('SELECT created_at FROM support_events WHERE session_id=? AND sequence=?',
                               (sid, int(sequence))) if original else None
            reason, recovered = 'invalid_source', []
            if original and event:
                messages = [dict(role='user', content=original['content'], sequence=int(sequence),
                                 created_at=event['created_at'], source_proof=original['proof'])]
                expression = fact.metadata.get('temporal_expression', '')
                item = dict(predicate='budget_range', value=deepcopy(fact.value), qualifiers={},
                    operation=fact.operation, polarity=fact.polarity, confidence=fact.confidence,
                    source_index=0, source_quote=fact.source_span,
                    valid_time_quote=expression if isinstance(expression, str)
                        and len(expression) <= 120 and expression in original['content'] else '')
                batch = _atomic_facts(dict(schema_version=2, facts=[item]), messages)
                if batch and not batch[0].qualifiers:
                    continue  # Original evidence explicitly supports a global budget.
                recovered = _ground_facts(batch, messages, sid)
                reason = 'category_recovered' if recovered else batch.issues[0]['reason']
            reports.append(dict(memory_id=fact.memory_id, source_event_id=fact.source_event_id,
                action='rescope' if recovered else 'quarantine', reason=reason,
                qualifiers=recovered[0].qualifiers if recovered else None))
            fact.status = 'superseded'
            fact.metadata.update(archived_legacy=True, scope_repair_reason=reason)
            for replacement in recovered:
                memory._apply_lifecycle(replacement, datetime.now(timezone.utc))
                replacement.metadata['repaired_from_memory_id'] = fact.memory_id
                replacements.append(replacement)
        if apply and reports:
            repaired, operations = reconcile(ledger, replacements, user_id=memory.user_id, hot_limit=memory.hot_limit)
            memory.store.commit_versions(memory.user_id, [memory._fact_to_item(f) for f in repaired])
            memory.last_operations = operations
    if apply and reports:
        memory.load()
    return reports


def main():
    import argparse
    import json
    from app.config.settings import settings
    from app.memory.runtime import _build_legacy_memory_manager
    from app.support.store import SupportStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    parser.add_argument('--apply', action='store_true', help='Commit the reviewed repair (default: dry run)')
    args = parser.parse_args()
    dsn = settings.support_database_url or settings.api_session_postgres_dsn
    if not dsn:
        parser.error('A support database is required to authenticate original evidence')
    support = SupportStore(dsn)
    with support.transaction(read_only=True) as tx:
        migrated = tx.one('SELECT migrated FROM memory_v3_users WHERE user_id=?', (args.user,))
    if migrated and migrated['migrated']:
        parser.error('This legacy repair must run before v3 migration; v3 corrections require new authenticated evidence')
    manager = _build_legacy_memory_manager(args.user, store=support)
    print(json.dumps(dict(applied=args.apply, changes=repair_budget_scopes(manager.ltm, support, apply=args.apply)),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
