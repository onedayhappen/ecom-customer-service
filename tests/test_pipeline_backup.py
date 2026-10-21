"""Restore a real PostgreSQL dump into a new disposable database, never in place."""
import os
import subprocess
from uuid import uuid4

import pytest

from app.support.store import SupportStore
from app.tasks.repository import TaskRepository
from app.tasks.rollout import Rollout
from tests.test_task_candidates import store
from tests.test_task_storage import empty_store, DSN
from tests.test_task_operations import flags, ready, confirm
from tests.test_pipeline_rollout import seed_legacy, switch


def test_preupgrade_backup_restores_original_messages_and_business_receipts(store, tmp_path):
    service, task, _, card = ready(store)
    assert confirm(service, card)['status'] == 'succeeded'
    seed_legacy(store)
    switch(store, 'stopped')
    with store.transaction() as tx:
        for table in ('task_legacy_imports', 'task_rollout_audit', 'task_pipeline_users', 'task_pipeline_state'):
            tx.execute('DROP TABLE ' + table)
        tx.execute('DELETE FROM support_schema_versions WHERE version=20')
        tx.execute("INSERT INTO support_schema_versions VALUES (19,'before-upgrade') ON CONFLICT DO NOTHING")
    before = store.public_history('s', 'alice')
    target_database = 'rollout_restore_' + uuid4().hex
    restored = None
    if not store.postgres:
        import sqlite3
        backup = tmp_path / 'preupgrade.sqlite'
        with sqlite3.connect(store.dsn[len('sqlite:///'):]) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        restored = SupportStore('sqlite:///' + backup.as_posix())
    else:
        import psycopg
        from psycopg import sql
        with store._connection(read_only=True) as tx:
            schema = tx.one('SELECT current_schema() AS s')['s']
        container = 'ecom-refactor-tests-postgres-1'
        docker = ['docker', 'exec', '-i', container]
        # This test accepts only the fixed isolated source, not an arbitrary DSN.
        assert store.dsn.startswith(DSN + '?') and schema.startswith('task_storage_')
        backup = tmp_path / 'preupgrade.dump'
        with backup.open('wb') as stream:
            subprocess.run([*docker, 'pg_dump', '-U', 'test_runner', '-d', 'ecom_concurrency_test',
                            '--schema', schema, '--format=custom'], stdout=stream, stderr=subprocess.PIPE,
                           check=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target_database)))
        restored_dsn = DSN.rsplit('/', 1)[0] + '/' + target_database + '?options=-csearch_path%3D' + schema
        restored = SupportStore(restored_dsn)
    try:
        if store.postgres:
            with backup.open('rb') as stream:
                subprocess.run([*docker, 'pg_restore', '-U', 'test_runner', '-d', target_database,
                                '--single-transaction', '--no-owner', '--no-acl'], stdin=stream,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # Original installation upgrades and imports; restored backup remains a
        # separate environment and upgrades to STOPPED, never auto-replays writes.
        upgraded = SupportStore(store.dsn)
        assert Rollout(upgraded).status()['state']['mode'] == 'stopped'
        migrated = Rollout(upgraded).import_legacy('legacy', user='alice', expected_version=3,
                                                  actor='operator', reason='restore verified')
        assert migrated.status == 'paused'
        assert Rollout(restored).status()['state']['mode'] == 'stopped'
        assert restored.public_history('s', 'alice') == before
        old = TaskRepository(restored, 'alice').get(task.task_id)
        assert old.waiting_for['operation_id'] == card['operation_id']
        with restored.transaction(read_only=True) as tx:
            assert tx.one('SELECT status FROM support_operations WHERE operation_id=?', (card['operation_id'],))['status'] == 'succeeded'
            assert not tx.one('SELECT * FROM task_legacy_imports')
        assert service.business.writes == [card['operation_id']]
    finally:
        if store.postgres:
            from app.db import get_connection_pool
            get_connection_pool(restored.dsn).close()
            assert target_database.startswith('rollout_restore_')
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(target_database)))
