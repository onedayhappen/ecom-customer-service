"""All-version persistence, evidence ownership and transactional durability."""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from app.memory.items import JsonMemoryItemRepository, MemoryItem, PostgresMemoryItemRepository
from app.memory.store import build_memory_store


def _item(identifier='fact-1', *, user='alice', status='active', tier='hot', evidence=None):
    return MemoryItem(
        identifier, user, 'user', user, 'fact', 'preference', '喜欢黑色',
        memory_key='preference:color', source_type='user_explicit',
        source_session='session', source_event_id='session:1', source_span='我喜欢黑色',
        created_at='2026-10-01T10:00:00+00:00', updated_at='2026-10-01T10:00:00+00:00',
        status=status, metadata={'tier': tier, 'version': 1, 'evidence': evidence or []},
    )


def _store(directory):
    return build_memory_store('json', str(directory))


def test_all_statuses_survive_reload_and_omitted_versions_remain(tmp_path):
    store = _store(tmp_path)
    versions = [_item(status, status=status) for status in ('active', 'superseded', 'retracted', 'expired')]
    store.commit_versions('alice', versions)
    store.commit_versions('alice', [_item('cold-active', tier='cold')])
    reloaded = _store(tmp_path)
    assert {item.memory_id for item in reloaded.list_versions('alice')} == {
        'active', 'superseded', 'retracted', 'expired', 'cold-active'}
    assert {item.memory_id for item in reloaded.list_items('alice')} == {'active', 'cold-active'}
    assert reloaded.list_versions('bob') == []


def test_replacement_and_reset_preserve_history_evidence_and_receipts(tmp_path):
    store = _store(tmp_path)
    store.replace_items('alice', [_item('old')])
    store.record_batch('alice', 'session:0:1')
    store.record_source('alice', 'session', 1)
    store.replace_items('alice', [_item('new')])
    assert {item.memory_id: item.status for item in store.list_versions('alice')} == {
        'old': 'superseded', 'new': 'active'}
    store.delete('alice')
    assert store.list_items('alice') == []
    versions = store.list_versions('alice')
    assert len(versions) == 2 and all(item.metadata['reset'] for item in versions)
    assert next(item for item in versions if item.memory_id == 'new').status == 'retracted'
    assert store.list_evidence('alice', 'old')[0]['source_span'] == '我喜欢黑色'
    assert store.batch_processed('alice', 'session:0:1')
    assert store.source_through('alice', 'session') == 1


def test_evidence_deduplicates_retries_preserves_multiple_spans_and_offsets(tmp_path):
    store = _store(tmp_path)
    evidence = [
        {'source_event_id': 'session:1', 'source_session': 'session', 'source_span': '我喜欢黑色', 'offset': [0, 5]},
        {'source_event_id': 'session:1', 'source_session': 'session', 'source_span': '黑色更好', 'offset': [6, 10]},
        {'source_event_id': 'session:1', 'source_session': 'session', 'source_span': '黑色更好', 'offset': [11, 15]},
    ]
    first = _item(evidence=evidence)
    store.commit_versions('alice', [first])
    store.commit_versions('alice', [first])
    # A status-only caller need not repeat earlier evidence.
    store.commit_versions('alice', [replace(first, status='superseded', metadata={'tier': 'cold'})])
    assert len(store.list_evidence('alice', first.memory_id)) == 3
    assert store.list_evidence('bob', first.memory_id) == []
    assert store.list_evidence('alice', 'unknown') == []
    persisted = store.list_versions('alice')[0]
    assert len(persisted.metadata['evidence']) == 3


def test_embedding_and_version_metadata_roundtrip_without_interpretation(tmp_path):
    store = _store(tmp_path)
    item = _item()
    item.metadata.update(schema_version=2, embedding=[0.1, -0.3, 0.9],
                         embedding_model='test', valid_from='2026-10-01', valid_to=None,
                         qualifiers={'product_category': '电子产品'})
    store.commit_versions('alice', [item])
    loaded = _store(tmp_path).list_versions('alice')[0]
    for key, value in item.metadata.items():
        if key != 'evidence':
            assert loaded.metadata[key] == value


def test_runtime_start_end_offsets_preserve_repeated_quotes_and_legacy_alias(tmp_path):
    store = _store(tmp_path)
    first = _item(evidence=[
        {'source_event_id': 'session:1', 'source_span': '我喜欢黑色', 'start_offset': 0, 'end_offset': 5},
        {'source_event_id': 'session:1', 'source_span': '我喜欢黑色', 'start_offset': 6, 'end_offset': 11},
    ])
    store.commit_versions('alice', [first])
    store.commit_versions('alice', [_item(evidence=[
        {'source_event_id': 'session:1', 'source_span': '我喜欢黑色', 'offset': [0, 5]},
    ])])
    assert len(store.list_evidence('alice', first.memory_id)) == 2


def test_transaction_rolls_back_versions_evidence_and_receipts_together(tmp_path):
    store = _store(tmp_path)
    store.commit_versions('alice', [_item('existing')])
    path = tmp_path / 'items' / 'alice.items.json'
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='crash'):
        with store.transaction('alice'):
            store.commit_versions('alice', [_item('uncommitted')])
            store.record_batch('alice', 'uncommitted-batch')
            store.record_source('alice', 'session', 99)
            raise RuntimeError('crash')
    assert path.read_bytes() == before
    assert not store.batch_processed('alice', 'uncommitted-batch')
    assert store.list_evidence('alice', 'uncommitted') == []


def test_owner_and_duplicate_id_fail_before_writing(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match='owner'):
        store.commit_versions('alice', [_item(user='bob')])
    with pytest.raises(ValueError, match='distinct'):
        store.commit_versions('alice', [_item(), _item()])
    assert not (tmp_path / 'items' / 'alice.items.json').exists()


def test_legacy_file_and_archive_migrate_only_on_explicit_write(tmp_path):
    repository = JsonMemoryItemRepository(str(tmp_path))
    path = repository._path('alice')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'user_id': 'alice', 'items': [asdict(_item('current'))]}), encoding='utf-8')
    store = _store(tmp_path)
    store.item_archive.append('alice', [_item('old')])
    before = path.read_bytes()
    assert [item.memory_id for item in store.list_versions('alice')] == ['current']
    assert path.read_bytes() == before
    store.commit_versions('alice', [])
    versions = store.list_versions('alice')
    assert len(versions) == 2
    legacy = next(item for item in versions if item.memory_id != 'current')
    assert legacy.status == 'historical' and legacy.metadata['archived_legacy'] is True
    assert store.list_items('alice')[0].memory_id == 'current'
    store.item_archive.append('alice', [_item('later-audit-only')])
    store.commit_versions('alice', [])
    assert len(store.list_versions('alice')) == 2


def test_authoritative_empty_versions_never_falls_back_to_items(tmp_path):
    repository = JsonMemoryItemRepository(str(tmp_path))
    path = repository._path('alice')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'user_id': 'alice', 'items': [asdict(_item())], 'versions': []}), encoding='utf-8')
    assert repository.list_versions('alice') == []
    assert repository.list_active('alice') == []


class _PgCursor:
    """Capture SQL/parameter semantics without requiring external PostgreSQL."""
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.connection.calls.append((sql, params))
        if sql.lstrip().startswith('SELECT user_id,metadata'):
            previous = self.connection.items.get(params[0])
            self.row = ((previous['user_id'], previous['metadata'], previous['source_session'],
                         previous['source_event_id'], previous['source_span'], previous['updated_at'])
                        if previous else None)

    def executemany(self, sql, rows):
        rows = list(rows)
        self.connection.calls.append((sql, rows))
        self.rowcount = 0
        for row in rows:
            if 'INSERT INTO agent_memory_items' in sql:
                old = self.connection.items.get(row['memory_id'])
                if old and old['user_id'] != row['user_id']:
                    continue
                self.connection.items[row['memory_id']] = deepcopy(row)
            elif 'INSERT INTO memory_evidence' in sql:
                key = tuple(row[name] for name in ('user_id', 'memory_id', 'source_event_id', 'evidence_id'))
                self.connection.evidence[key] = json.loads(row['payload'])
            self.rowcount += 1

    def fetchone(self):
        return self.row


class _PgConnection:
    def __init__(self):
        self.items, self.evidence, self.calls = {}, {}, []

    def cursor(self):
        return _PgCursor(self)

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def __enter__(self):
        self.before = deepcopy((self.items, self.evidence))
        return self

    def __exit__(self, error_type, *args):
        if error_type:
            self.items, self.evidence = self.before
        return False


def _postgres(monkeypatch):
    repository = object.__new__(PostgresMemoryItemRepository)
    repository._transaction = ContextVar('test_version_pg_transaction', default=None)
    connection = _PgConnection()
    monkeypatch.setattr(repository, '_raw_connect', lambda: connection)
    return repository, connection


def test_postgres_keeps_version_rows_and_scoped_multi_span_evidence(monkeypatch):
    repository, connection = _postgres(monkeypatch)
    first = _item('first', evidence=[
        {'source_event_id': 'session:1', 'source_span': '我喜欢黑色', 'offset': [0, 5]},
        {'source_event_id': 'session:1', 'source_span': '其他片段', 'offset': [6, 10]},
    ])
    repository.commit_versions('alice', [first])
    repository.commit_versions('alice', [replace(first, status='superseded'), _item('second', tier='cold')])
    assert len(connection.items) == 2
    assert connection.items['first']['status'] == 'superseded'
    assert len([key for key in connection.evidence if key[1] == 'first']) == 2
    assert all(key[0] == 'alice' for key in connection.evidence)
    assert not any('DELETE FROM' in sql for sql, _ in connection.calls)
    assert sum('pg_advisory_xact_lock' in sql for sql, _ in connection.calls) == 2


def test_postgres_rejects_cross_owner_id_and_rolls_back_whole_batch(monkeypatch):
    repository, connection = _postgres(monkeypatch)
    repository.commit_versions('bob', [_item('foreign', user='bob')])
    before = deepcopy((connection.items, connection.evidence))
    with pytest.raises(ValueError, match='another user'):
        repository.commit_versions('alice', [_item('new'), _item('foreign')])
    assert (connection.items, connection.evidence) == before


def test_postgres_schema_constrains_evidence_owner_and_source(monkeypatch):
    _, connection = _postgres(monkeypatch)
    PostgresMemoryItemRepository._schema_ddl(connection.cursor())
    ddl = '\n'.join(sql for sql, _ in connection.calls)
    assert 'PRIMARY KEY(user_id,memory_id,source_event_id,evidence_id)' in ddl
    assert 'FOREIGN KEY(user_id,memory_id) REFERENCES agent_memory_items(user_id,memory_id)' in ddl
