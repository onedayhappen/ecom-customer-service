"""Durable user isolation, source ordering and crash-safe memory merging."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os

import pytest

from app.memory.items import MemoryItem, PostgresMemoryItemRepository
from app.memory.long_term import LongTermMemory, MemoryFact


def _fact(content='喜欢黑色', key='preference:color', *, event='s:1', at=None):
    timestamp = (at or datetime.now(timezone.utc)).isoformat()
    return MemoryFact(content=content, category='preference', created_at=timestamp,
                      updated_at=timestamp, source_event_id=event, source_session='s',
                      source_type='user_explicit', source_span=content, memory_key=key,
                      confidence=0.9)


def _memory(directory, user='user', **kwargs):
    return LongTermMemory(user_id=user, memory_dir=str(directory), **kwargs)


def _concurrent_merge(directory, key, barrier):
    memory = _memory(directory)
    memory.load()
    barrier.wait(timeout=20)
    memory.merge_and_save([_fact(content=key, key=key, event=key)], source_batch_id=key)


def _contend_empty_lock(path, result):
    from app.memory.file_lock import ProcessFileLock
    try:
        with ProcessFileLock(path, timeout=0.2):
            result.put('acquired')
    except TimeoutError:
        result.put('timeout')
    except Exception as exc:
        result.put(type(exc).__name__)


@pytest.mark.skipif(os.name != 'nt', reason='Windows mandatory byte-range locks')
def test_empty_lock_file_contention_waits_without_writing_before_lock(tmp_path):
    import msvcrt
    from app.memory.file_lock import ProcessFileLock
    path = tmp_path / 'empty.lock'
    context = multiprocessing.get_context('spawn')
    results = context.Queue()
    process = context.Process(target=_contend_empty_lock, args=(str(path), results))
    with path.open('a+b') as owner:
        msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            process.start()
            process.join(timeout=20)
            assert process.exitcode == 0
            assert results.get(timeout=2) == 'timeout'
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            owner.seek(0)
            msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
            results.close()
            results.join_thread()
    with ProcessFileLock(path, timeout=0.2):
        pass


@pytest.mark.parametrize('_iteration', range(4))
def test_cross_process_stale_instances_merge_without_lost_updates(tmp_path, _iteration):
    context = multiprocessing.get_context('spawn')
    barrier = context.Barrier(2)
    processes = [context.Process(target=_concurrent_merge,
                                args=(str(tmp_path), key, barrier))
                 for key in ('preference:color', 'preference:size')]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    memory = _memory(tmp_path)
    memory.load()
    assert {fact.memory_key for fact in memory.facts} == {'preference:color', 'preference:size'}


def test_ids_are_user_scoped_and_same_owner_ids_survive_reload(tmp_path):
    first, second = _memory(tmp_path, 'alice'), _memory(tmp_path, 'bob')
    source = _fact()
    first.merge_and_save([source], source_batch_id='s:0:1')
    second.merge_and_save([source], source_batch_id='s:0:1')
    assert first.facts[0].memory_id != second.facts[0].memory_id
    identifier = first.facts[0].memory_id
    first.merge_and_save([], source_batch_id='s:1:2')
    assert first.facts[0].memory_id == identifier
    assert source.memory_id == ''  # Merging does not mutate caller candidates.


def test_receipt_survives_supersession_and_reset(tmp_path):
    memory = _memory(tmp_path)
    old = _fact(event='s:1', at=datetime.now(timezone.utc) - timedelta(days=1))
    new = _fact('喜欢白色', event='s:2')
    assert memory.merge_and_save([old], source_batch_id='s:0:1')
    assert memory.merge_and_save([new], source_batch_id='s:1:2')
    expected = deepcopy(memory.facts)
    assert memory.merge_and_save([old], source_batch_id='s:0:1') is False
    assert memory.facts == expected
    memory.reset()
    assert memory.merge_and_save([new], source_batch_id='s:1:2') is False
    assert memory.facts == []


@pytest.mark.parametrize('older_content', ['喜欢黑色', '喜欢白色'])
def test_out_of_order_source_does_not_replace_or_refresh_newer_slot(tmp_path, older_content):
    memory = _memory(tmp_path)
    now = datetime.now(timezone.utc)
    newer = _fact('喜欢白色', event='new:1', at=now)
    older = _fact(older_content, event='old:1', at=now - timedelta(days=2))
    older.confidence = 1.0
    memory.merge_and_save([newer], source_batch_id='new:0:1')
    expected = deepcopy(memory.facts)
    memory.merge_and_save([older], source_batch_id='old:0:1')
    assert memory.facts == expected
    assert memory.store.batch_processed('user', 'old:0:1')


def test_same_event_reframed_as_new_batch_does_not_refresh_ttl(tmp_path):
    memory = _memory(tmp_path)
    fact = _fact()
    memory.merge_and_save([fact], source_batch_id='s:0:1')
    expected = deepcopy(memory.facts)
    memory.merge_and_save([fact], source_batch_id='s:0:2')
    assert memory.facts == expected


def test_changed_batch_cannot_replay_a_superseded_or_deleted_source(tmp_path):
    memory = _memory(tmp_path)
    first = _fact(event='s:1', at=datetime.now(timezone.utc) - timedelta(days=1))
    memory.merge_and_save([first], source_batch_id='s:0:1')
    memory.merge_and_save([_fact('喜欢白色', event='s:2')], source_batch_id='s:1:2')
    expected = deepcopy(memory.facts)
    memory.merge_and_save([first], source_batch_id='s:0:3')
    assert memory.facts == expected
    memory.reset()
    memory.merge_and_save([first], source_batch_id='s:0:4')
    assert memory.facts == []


def test_valid_empty_batch_records_source_watermark(tmp_path):
    memory = _memory(tmp_path)
    memory.merge_and_save([], source_batch_id='session:with:colon:0:10')
    old = _fact(event='session:with:colon:1')
    memory.merge_and_save([old], source_batch_id='session:with:colon:0:11')
    assert memory.facts == []
    assert memory.store.source_through('user', 'session:with:colon') == 11


def test_failure_before_receipt_commit_leaves_items_and_receipts_unchanged(tmp_path, monkeypatch):
    memory = _memory(tmp_path)
    memory.merge_and_save([_fact()], source_batch_id='s:0:1')
    path = tmp_path / 'items' / 'user.items.json'
    original = path.read_bytes()

    def crash(*args):
        raise RuntimeError('simulated crash between item save and receipt')

    monkeypatch.setattr(memory.store, 'record_batch', crash)
    with pytest.raises(RuntimeError, match='simulated crash'):
        memory.merge_and_save([_fact('喜欢白色', event='s:2')], source_batch_id='s:1:2')
    assert path.read_bytes() == original


def test_authoritative_empty_items_do_not_revive_legacy_snapshot(tmp_path):
    memory = _memory(tmp_path)
    memory.store.repository.save('user', {'facts': [{'content': 'legacy identity',
                                                    'category': 'identity', 'created_at': ''}]}, 0)
    memory.load()
    assert memory.facts[0].content == 'legacy identity'
    memory.facts = []
    memory.save()
    reloaded = _memory(tmp_path)
    reloaded.load()
    assert reloaded.facts == []


def test_expiration_reads_do_not_write_or_overwrite_newer_memory(tmp_path):
    reader = _memory(tmp_path)
    expired = _fact(at=datetime.now(timezone.utc) - timedelta(days=100))
    expired.expires_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    reader.facts = [expired]
    reader.save()
    path = tmp_path / 'items' / 'user.items.json'
    original = path.read_bytes()
    reader.load()
    assert reader.facts == []
    assert path.read_bytes() == original
    writer = _memory(tmp_path)
    writer.merge_and_save([_fact('喜欢白色', event='s:2')], source_batch_id='s:1:2')
    current = path.read_bytes()
    reader.retrieve_relevant('喜欢什么颜色')
    assert path.read_bytes() == current


def test_max_facts_and_source_time_ttl_are_honored(tmp_path):
    memory = _memory(tmp_path, max_facts=2)
    source_time = datetime.now(timezone.utc) - timedelta(days=10)
    memory.merge_and_save([_fact(str(i), f'preference:{i}', event=f's:{i}', at=source_time)
                           for i in range(4)], source_batch_id='s:0:4')
    assert len(memory.facts) == 2
    expiry = datetime.fromisoformat(memory.facts[0].expires_at)
    assert abs((expiry - source_time).total_seconds() - 90 * 86400) < 1


def test_corrupt_authoritative_json_is_not_overwritten(tmp_path):
    path = tmp_path / 'items' / 'user.items.json'
    path.parent.mkdir()
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        _memory(tmp_path).merge_and_save([_fact()], source_batch_id='s:0:1')
    assert path.read_text(encoding='utf-8') == '{broken'


def test_json_user_path_cannot_escape_memory_directory(tmp_path):
    with pytest.raises(ValueError, match='user path'):
        _memory(tmp_path, '../another-user').load()


@pytest.mark.parametrize('user', ['', '../other', '..\\other', 'user:stream', '/absolute', 'bad\x00name'])
def test_item_repository_rejects_paths_and_alternate_streams(tmp_path, user):
    from app.memory.items import JsonMemoryItemRepository
    with pytest.raises(ValueError, match='user path'):
        JsonMemoryItemRepository(str(tmp_path)).list_versions(user)


def test_structured_scope_and_owner_override_legacy_metadata(tmp_path):
    memory = _memory(tmp_path)
    items = [
        MemoryItem('case', 'user', 'case', 'case-1', 'fact', 'preference', 'case only',
                   metadata={'scope': 'user_preference'}),
        MemoryItem('session', 'user', 'session', 'session-1', 'fact', 'preference', 'session only',
                   metadata={'scope': 'user_profile'}),
        MemoryItem('foreign-owner', 'other', 'user', 'user', 'fact', 'preference', 'foreign owner'),
        MemoryItem('foreign-scope', 'user', 'user', 'other', 'fact', 'preference', 'foreign scope'),
        MemoryItem('invalid-metadata', 'user', 'user', 'user', 'fact', 'preference', 'invalid',
                   metadata={'scope': 'case_progress'}),
        MemoryItem('valid', 'user', 'user', 'user', 'fact', 'preference', 'profile',
                   metadata={'scope': 'user_profile'}),
    ]
    memory._load_from_items(items)
    assert [(fact.content, fact.scope) for fact in memory.facts] == [
        ('session only', 'session:session-1'), ('profile', 'user_profile'),
    ]


def test_retired_case_scope_cannot_be_written_as_user_memory(tmp_path):
    memory = _memory(tmp_path)
    fact = _fact()
    fact.scope = 'case:old-case'
    with pytest.raises(ValueError, match='Retired Case scope'):
        memory._fact_to_item(fact)


class _Cursor:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args=()):
        self.sql.append((sql, args))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _fake_pg(monkeypatch, rows):
    repository = object.__new__(PostgresMemoryItemRepository)
    cursor = _Cursor(rows)

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(repository, '_connect', connection)
    monkeypatch.setattr(repository, 'transaction', lambda user: nullcontext())
    return repository, cursor


def test_postgres_column_mapping_preserves_category_and_memory_key(monkeypatch):
    row = ['id', 'user', 'user', 'user', 'fact', 'preference', 'preference:color',
           '喜欢黑色', 'user_explicit', 's', 's:1', '喜欢黑色', 0.9, 'active', '', '',
           '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', '', [], {}]
    repository, _ = _fake_pg(monkeypatch, [row])
    item = repository.list_active('user')[0]
    assert item.category == 'preference'
    assert item.memory_key == 'preference:color'


def test_postgres_rejects_foreign_memory_id_before_deleting_active_set(monkeypatch):
    repository, cursor = _fake_pg(monkeypatch, [('other-user',)])
    item = MemoryItem('foreign-id', 'user', 'user', 'user', 'fact', 'preference', 'black')
    with pytest.raises(ValueError, match='another user'):
        repository.replace_user_items('user', [item])
    assert not any('DELETE' in sql for sql, _ in cursor.sql)
