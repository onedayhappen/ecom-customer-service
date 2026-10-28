"""User-serialized commits; receipts, checkpoints and facts share one transaction."""
from dataclasses import asdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

from app.memory.long_term import MemoryFact
from app.memory.reconciliation import reconcile
from app.memory.source_policy import SourcePolicy
from app.memory.retrieval import time_eligible
from app.memory.v3.models import Conflict
from app.memory.v3.validation import encoded, family, order, sources_valid


def now():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, store, user):
        self.store, self.user = store, user
        self.policy = SourcePolicy(store, user)

    def lock(self, tx):
        from app.tasks.provenance import lock_input_user
        lock_input_user(tx, self.user)
        tx.execute('INSERT INTO memory_v3_users(user_id) VALUES (?) ON CONFLICT DO NOTHING', (self.user,))
        return tx.one('SELECT * FROM memory_v3_users WHERE user_id=?' + (' FOR UPDATE' if tx.postgres else ''), (self.user,))

    def revision(self):
        with self.store.transaction(read_only=True) as tx:
            row = tx.one('SELECT revision FROM memory_v3_users WHERE user_id=?', (self.user,))
            return row['revision'] if row else 0

    def write(self, tx, facts):
        for fact in facts:
            tx.execute('''INSERT INTO memory_v3_records VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(user_id,memory_id) DO UPDATE SET status=excluded.status,
                payload=excluded.payload,valid_from=excluded.valid_from''',
                (self.user, fact.memory_id, family(fact.predicate, fact.qualifiers), fact.status,
                 fact.observed_at, fact.valid_from, encoded(asdict(fact))))

    def read(self, *, history=False, limit=500):
        self.activate_due()
        with self.store.transaction(read_only=True) as tx:
            condition = "status NOT IN ('forgotten','deleted')" if history else "status='active'"
            rows = tx.all(f'''SELECT payload FROM memory_v3_records WHERE user_id=? AND {condition}
                ORDER BY observed_at DESC,memory_id LIMIT ?''', (self.user, limit))
            facts = [MemoryFact(**json.loads(r['payload'])) for r in rows]
            return [f for f in facts if not f.metadata.get('forgotten')
                    and time_eligible(f, history=history) and sources_valid(self.policy, f, tx)]

    def activate_due(self):
        with self.store.transaction(read_only=True) as tx:
            due = tx.all("SELECT DISTINCT family FROM memory_v3_records WHERE user_id=? AND status='scheduled' AND valid_from<=?",
                         (self.user, now()))
        if due:
            with self.store.transaction() as tx:
                self.lock(tx)
                for row in due:
                    facts = self._family(tx, row['family'])
                    facts, _ = reconcile(facts, [], user_id=self.user)
                    self._forget_filter(tx, row['family'], facts)
                    self.write(tx, facts)
                tx.execute('UPDATE memory_v3_users SET revision=revision+1 WHERE user_id=?', (self.user,))

    def _family(self, tx, key):
        return [MemoryFact(**json.loads(r['payload'])) for r in tx.all(
            'SELECT payload FROM memory_v3_records WHERE user_id=? AND family=?', (self.user, key))]

    def _forget_filter(self, tx, key, facts):
        tombstones = [MemoryFact(**json.loads(r['payload'])) for r in tx.all(
            'SELECT payload FROM memory_v3_forgets WHERE user_id=? AND family=?', (self.user, key))]
        for tombstone in tombstones:
            matching = lambda f: tombstone.value is None or encoded(f.value) == encoded(tombstone.value)
            restores = [f for f in facts if f.operation == 'ASSERT' and f.metadata.get('restore')
                        and matching(f) and order(f) > order(tombstone)]
            for fact in facts:
                if fact.operation != 'ASSERT' or not matching(fact):
                    continue
                if order(fact) <= order(tombstone) or not any(order(r) <= order(fact) for r in restores):
                    fact.status = 'forgotten'
                    fact.metadata['forgotten'] = True

    def commit(self, validated, outcomes, *, batch_id, sid, through, expected_revision, job=None):
        with self.store.transaction() as tx:
            state = self.lock(tx)
            saved = tx.one('SELECT result FROM memory_v3_receipts WHERE user_id=? AND batch_id=?', (self.user, batch_id))
            if saved:
                return json.loads(saved['result'])
            if state['revision'] != expected_revision:
                raise Conflict('memory_snapshot_changed')
            if job:
                from time import time
                lease = tx.one('SELECT lease_token,lease_until FROM memory_v3_jobs WHERE session_id=?', (sid,))
                if not lease or lease['lease_token'] != job['lease_token'] or lease['lease_until'] <= time():
                    raise Conflict('worker_lease_lost')
            groups = {}
            for item in validated:
                groups.setdefault(item.change.group_id, []).append(item)
            accepted = []
            for group, items in groups.items():
                error = None
                for item in items:
                    if not sources_valid(self.policy, item.fact, tx):
                        error = 'source_invalidated'
                    if item.change.action == 'MERGE' and not any(
                            f.status == 'active' and f.value == item.fact.value and f.polarity == item.fact.polarity
                            for f in self._family(tx, item.family)):
                        error = 'merge_requires_same_value'
                    if item.change.action != 'FORGET':
                        trial = deepcopy(item.fact)
                        self._forget_filter(tx, item.family, [*self._family(tx, item.family), trial])
                        if trial.metadata.get('forgotten'):
                            error = 'explicit_restore_required'
                    for target in item.change.target_ids:
                        row = tx.one('SELECT family FROM memory_v3_records WHERE user_id=? AND memory_id=?', (self.user, target))
                        if not row or row['family'] != item.family:
                            error = 'invalid_update_target'
                if error:
                    outcomes.append(dict(group_id=group, status='rejected', reason=error))
                else:
                    accepted.extend(items)
            families = {}
            for item in accepted:
                families.setdefault(item.family, []).append(item)
            applied = 0
            for key, items in families.items():
                ledger = self._family(tx, key)
                for item in items:
                    if item.change.action == 'FORGET':
                        command_id = sha256(encoded([self.user, key, item.fact.source_event_id, item.fact.value]).encode()).hexdigest()
                        tx.execute('INSERT INTO memory_v3_forgets VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                            (self.user, command_id, key, encoded(item.fact.value), item.fact.observed_at, encoded(asdict(item.fact))))
                    ledger, operations = reconcile(ledger, [item.fact], user_id=self.user)
                    self._forget_filter(tx, key, ledger)
                    hidden = any(f.source_event_id == item.fact.source_event_id and f.value == item.fact.value
                                 and f.metadata.get('forgotten') for f in ledger)
                    status = 'needs_clarification' if hidden and item.change.action != 'FORGET' else 'applied'
                    outcomes.append(dict(group_id=item.change.group_id, action=item.change.action, status=status,
                                         reason='explicit_restore_required' if status != 'applied' else 'committed'))
                    applied += status == 'applied'
                self.write(tx, ledger)
            revision = state['revision'] + bool(families)
            tx.execute('UPDATE memory_v3_users SET revision=? WHERE user_id=?', (revision, self.user))
            result = dict(status='completed_with_issues' if any(o['status'] not in {'applied', 'noop'} for o in outcomes) else 'completed',
                          revision=revision, applied=applied, outcomes=outcomes, through_sequence=through)
            tx.execute('INSERT INTO memory_v3_receipts VALUES (?,?,?,?,?,?,?,?)',
                (self.user, batch_id, sid, through, revision, result['status'], encoded(result), now()))
            if job:
                tx.execute('''UPDATE memory_v3_jobs SET through_sequence=?,attempts=0,last_error=NULL,
                    lease_token=NULL,lease_until=0,status=CASE WHEN requested>? THEN 'pending' ELSE ? END
                    WHERE session_id=? AND lease_token=?''', (through, through, result['status'], sid, job['lease_token']))
            return result

    def migrate(self, legacy_factory):
        with self.store.transaction(read_only=True) as tx:
            row = tx.one('SELECT migrated FROM memory_v3_users WHERE user_id=?', (self.user,))
            if row and row['migrated']:
                return
        # Import is one-off, outside the commit lock; source checks are repeated inside.
        legacy = legacy_factory().ltm.history_facts()
        with self.store.transaction() as tx:
            if self.lock(tx)['migrated']:
                return
            migrated = []
            skipped = {}
            for fact in legacy:
                if fact.schema_version < 2 or not fact.predicate or fact.metadata.get('reset'):
                    skipped['legacy_unstructured_or_reset'] = skipped.get('legacy_unstructured_or_reset', 0) + 1
                    continue
                if not sources_valid(self.policy, fact, tx):
                    skipped['invalid_source'] = skipped.get('invalid_source', 0) + 1
                    continue
                fact.schema_version = 3
                fact.metadata['migrated_from'] = 2
                migrated.append(fact)
            # Normalize slot identity when a previously guessed unit was part
            # of the v2 key; v3 stores units on the value's evidence instead.
            from app.memory.reconciliation import _slot
            for fact in migrated:
                fact.memory_key = _slot(fact)
            migrated, _ = reconcile(migrated, [], user_id=self.user)
            self.write(tx, migrated)
            tx.execute('UPDATE memory_v3_users SET migrated=1,revision=revision+1 WHERE user_id=?', (self.user,))
            revision = tx.one('SELECT revision FROM memory_v3_users WHERE user_id=?', (self.user,))['revision']
            result = dict(imported=len(migrated), skipped=skipped, revision=revision,
                          status='completed_with_issues' if skipped else 'completed')
            tx.execute('INSERT INTO memory_v3_receipts VALUES (?,?,?,?,?,?,?,?)',
                       (self.user, 'migration:v2', '', 0, revision, result['status'], encoded(result), now()))
