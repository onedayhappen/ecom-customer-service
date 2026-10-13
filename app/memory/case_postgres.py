"""Shared Case/confirmation/idempotency snapshots for multi-host Workers.

Only short state transactions hold a per-user row lock. MutationGuard releases
its Case transaction before calling a business service.
"""
import json
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict

from app.memory.case_manager import CaseManager, CaseRecord

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS case_snapshots (user_id TEXT PRIMARY KEY, payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS case_archives (case_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS case_archives_user ON case_archives(user_id)',
)


class CaseStateConflict(RuntimeError):
    """A stale writer must reload instead of overwriting newer business state."""


_MISSING = object()


def merge_changes(baseline, local, current):
    """Three-way merge; disjoint edits commute, competing edits fail closed."""
    if local == baseline:
        return deepcopy(current)
    if current == baseline or current == local:
        return deepcopy(local)
    if all(isinstance(value, dict) for value in (baseline, local, current)):
        merged = deepcopy(current)
        for key in baseline.keys() | local.keys():
            before, after = baseline.get(key, _MISSING), local.get(key, _MISSING)
            if before == after:
                continue
            actual = current.get(key, _MISSING)
            if key in {'updated_at', 'last_active_at'}:
                merged[key] = max(str(after), str(actual))
            elif after is _MISSING:
                if actual != before and actual is not _MISSING:
                    raise CaseStateConflict('业务状态已改变，请重新加载后重试')
                merged.pop(key, None)
            elif before is _MISSING:
                if actual is not _MISSING and actual != after:
                    raise CaseStateConflict('业务状态已改变，请重新加载后重试')
                merged[key] = deepcopy(after)
            elif actual is _MISSING:
                raise CaseStateConflict('业务事项已归档或删除，请重新加载')
            else:
                merged[key] = merge_changes(before, after, actual)
        return merged
    raise CaseStateConflict('业务状态已改变，请重新加载后重试')


class PostgresCaseManager(CaseManager):
    def __init__(self, user_id, store):
        if not store.postgres:
            raise ValueError('Shared Case storage requires PostgreSQL')
        self.store, self._case_tx = store, None
        super().__init__(user_id)

    def load(self):
        with self.store.transaction(read_only=True) as tx:
            self._read(tx)

    def _read(self, tx):
        row = tx.one('SELECT payload FROM case_snapshots WHERE user_id=?', (self.user_id,))
        data = json.loads(row['payload']) if row else {}
        self.cases = {item['case_id']: CaseRecord(**item) for item in data.get('cases', []) if item.get('user_id') == self.user_id}
        self.events = [event for event in data.get('events', []) if event.get('user_id') == self.user_id]
        self.pending_order_selections = data.get('pending_order_selections', {})
        self._capture_baseline()

    def _load_unlocked(self):
        self._read(self._case_tx)

    def _refresh_unlocked(self):
        local_cases = {key: asdict(case) for key, case in self.cases.items()}
        baseline_cases = self._baseline_cases
        local_events, baseline_events = list(self.events), self._baseline_event_ids
        local_pending, baseline_pending = deepcopy(self.pending_order_selections), deepcopy(self._baseline_pending)
        for sid in self._deleted_pending_order_selections:
            local_pending.pop(sid, None)
        self._load_unlocked()
        current_cases = {key: asdict(case) for key, case in self.cases.items()}
        merged = merge_changes(baseline_cases, local_cases, current_cases)
        self.cases = {key: CaseRecord(**value) for key, value in merged.items()}
        self.pending_order_selections = merge_changes(baseline_pending, local_pending, self.pending_order_selections)
        removed = baseline_events - {event['event_id'] for event in local_events}
        self.events = [event for event in self.events if event['event_id'] not in removed]
        existing = {event['event_id'] for event in self.events}
        self.events.extend(event for event in local_events
                           if event['event_id'] not in baseline_events and event['event_id'] not in existing)

    def _capture_baseline(self):
        super()._capture_baseline()
        self._baseline_pending = deepcopy(self.pending_order_selections)

    @contextmanager
    def transaction(self):
        if self._lock_depth:
            yield
            return
        with self.store.transaction() as tx:
            tx.execute('INSERT INTO case_snapshots VALUES (?,?) ON CONFLICT(user_id) DO NOTHING', (self.user_id, '{}'))
            tx.one('SELECT user_id FROM case_snapshots WHERE user_id=? FOR UPDATE', (self.user_id,))
            self._case_tx, self._lock_depth = tx, 1
            try:
                self._refresh_unlocked()
                yield
            except BaseException:
                # Rollback must not leave the object claiming an uncommitted
                # confirmation or idempotency result as its new baseline.
                self.cases, self.events, self.pending_order_selections = {}, [], {}
                self._deleted_pending_order_selections.clear()
                self._capture_baseline()
                raise
            finally:
                self._case_tx, self._lock_depth = None, 0

    def save(self):
        if self._lock_depth:
            self._save_unlocked()
        else:
            with self.transaction():
                self._save_unlocked()

    def _save_unlocked(self, merge_disk=False):
        for sid in self._deleted_pending_order_selections:
            self.pending_order_selections.pop(sid, None)
        data = {'user_id': self.user_id, 'cases': [asdict(c) for c in self.cases.values()],
                'events': self.events, 'pending_order_selections': self.pending_order_selections}
        self._case_tx.execute('UPDATE case_snapshots SET payload=? WHERE user_id=?', (json.dumps(data, ensure_ascii=False), self.user_id))
        self._deleted_pending_order_selections.clear()
        self._capture_baseline()

    def _archive_case(self, case):
        payload = {'archived_at': self._now(), 'case': asdict(case),
                   'events': [event for event in self.events if event.get('case_id') == case.case_id]}
        def write(tx):
            tx.execute('INSERT INTO case_archives VALUES (?,?,?) ON CONFLICT(case_id) DO NOTHING',
                       (case.case_id, self.user_id, json.dumps(payload, ensure_ascii=False)))
        if self._case_tx:
            write(self._case_tx)
        else:
            with self.store.transaction() as tx:
                write(tx)

    def prune_cases(self, now=None):
        # Archive + removal must commit together under the same user row lock.
        with self.transaction():
            changed = super().prune_cases(now)
            if changed:
                self._save_unlocked()
            return changed


def create_case_manager(user_id):
    from app.config.settings import settings
    if settings.case_storage_backend == 'postgres':
        from app.support.runtime import get_store
        return PostgresCaseManager(user_id, get_store())
    if settings.case_storage_backend != 'json':
        raise ValueError('CASE_STORAGE_BACKEND must be json or postgres')
    return CaseManager(user_id, settings.case_dir)
