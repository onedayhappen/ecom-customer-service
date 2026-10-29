"""Task context isolation and removal of the retired Case store."""
import json

import pytest

from app.support.store import SCHEMA_VERSION, SupportError, SupportStore
from app.tasks.evidence import find_evidence
from app.tasks.repository import TaskRepository
from tests.test_task_candidates import task
from tests.test_message_association import store
from tests.test_task_storage import empty_store, message


def test_removed_tools_cannot_be_called_through_registry():
    from app.tools.registry import TOOL_DEFINITIONS, execute_tool
    names = {d['function']['name'] for d in TOOL_DEFINITIONS}
    assert len(names) == 54 and {'read_task_context', 'inspect_image', 'recall_user_memory'} <= names
    for name in ('read_case_memory', 'save_case_context', 'text_to_sql'):
        assert name not in names
        assert json.loads(execute_tool(name, {}))['error_code'] == 'validation_error'


def test_task_evidence_lookup_is_scoped_by_task_user_and_session(store):
    first, _ = task(store, 's')
    second, _ = task(store, 's', details={'order_id': 'ORD-B'})
    with store.transaction() as tx:
        control = store._control(tx, 's', 'alice')
        store._event(tx, control, 'task_tool_result', 'system', dict(event_id='proof',
            task_id=first.task_id, tool='query_order', result={'success': True}),
            private=True, key='task-tool:proof')
        assert find_evidence(tx, 'bob', 'proof') is None
        assert find_evidence(tx, 'alice', 'proof', session_id='old') is None
        assert find_evidence(tx, 'alice', 'proof', task_id=second.task_id) is None
        assert find_evidence(tx, 'alice', 'proof', task_id=first.task_id) is not None
    repo = TaskRepository(store, 'alice')
    source = dict(kind='tool_result', session_id='s', event_id='proof')
    updated = repo.update(first.task_id, 's', expected_version=1, change_id='right',
                          source=source, patch={'status': 'waiting_external'})
    assert updated.version == 2


@pytest.mark.parametrize('kind,actor,private', [
    ('message', 'alice', False), ('task_tool_result', 'system', False),
    ('task_tool_result', 'alice', True),
])
def test_public_or_user_authored_content_cannot_become_tool_evidence(store, kind, actor, private):
    record, _ = task(store, 's')
    with store.transaction() as tx:
        control = store._control(tx, 's', 'alice')
        store._event(tx, control, kind, actor, dict(event_id='forged', task_id=record.task_id,
            role='user', content='claimed refund', result={'success': True}),
            private=private, key='task-tool:forged')
        assert find_evidence(tx, 'alice', 'forged', task_id=record.task_id) is None


def test_upgrade_discards_case_tables_and_keeps_task_history(store):
    record, _ = task(store, 's')
    history = store.public_history('s', 'alice')
    with store.transaction() as tx:
        tx.execute('DELETE FROM support_schema_versions WHERE version>20')
        tx.execute("INSERT INTO support_schema_versions VALUES (20,'old') ON CONFLICT DO NOTHING")
        tx.execute('CREATE TABLE case_snapshots (user_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        tx.execute('CREATE TABLE case_archives (case_id TEXT PRIMARY KEY,user_id TEXT,payload TEXT)')
        tx.execute('INSERT INTO case_snapshots VALUES (?,?)', ('alice', '{"old":"discard"}'))
        tx.execute('INSERT INTO case_archives VALUES (?,?,?)', ('old', 'alice', '{}'))
    upgraded = SupportStore(store.dsn)
    upgraded.initialize()
    assert TaskRepository(upgraded, 'alice').get(record.task_id) == record
    assert upgraded.public_history('s', 'alice') == history
    with upgraded.transaction(read_only=True) as tx:
        if tx.postgres:
            tables = tx.all("SELECT tablename AS name FROM pg_tables WHERE schemaname=current_schema()")
        else:
            tables = tx.all("SELECT name FROM sqlite_master WHERE type='table'")
        assert not {'case_snapshots', 'case_archives'} & {r['name'] for r in tables}
        assert tx.one('SELECT MAX(version) AS v FROM support_schema_versions')['v'] == SCHEMA_VERSION
    SupportStore(store.dsn).initialize()


def test_retirement_stops_only_legacy_jobs_and_preserves_receipts(empty_store):
    store = empty_store
    store.ensure('old', 'alice')
    store.ensure('current', 'alice')
    with store.transaction() as tx:
        tx.execute('DELETE FROM support_schema_versions WHERE version=?', (SCHEMA_VERSION,))
        tx.execute("INSERT INTO support_schema_versions VALUES (21,'prior') ON CONFLICT DO NOTHING")
        for jid, sid, kind in [('legacy-job', 'old', 'conversation'), ('task-job', 'current', 'task_execution')]:
            tx.execute("""INSERT INTO runtime_jobs(job_id,session_id,user_id,control_version,
                payload,status,created_at,queue_deadline,run_timeout,next_delivery,kind)
                VALUES (?,?,'alice',0,'{}','running',0,9999999999,60,0,?)""", (jid,sid,kind))
        tx.execute("UPDATE support_conversations SET bot_job_id='legacy-job' WHERE session_id='old'")
        tx.execute("""INSERT INTO support_operations
            (operation_id,session_id,control_version,tool,status,created_at,result)
            VALUES ('receipt','old',0,'apply_refund','unknown','prior','{}')""")
    upgraded = SupportStore(store.dsn)
    upgraded.initialize()
    with upgraded.transaction(read_only=True) as tx:
        assert tx.one("SELECT status FROM runtime_jobs WHERE job_id='legacy-job'")['status'] == 'interrupted'
        assert tx.one("SELECT status FROM runtime_jobs WHERE job_id='task-job'")['status'] == 'running'
        control = tx.one("SELECT * FROM support_conversations WHERE session_id='old'")
        assert control['bot_job_id'] is None and control['control_version'] == 1
        assert tx.one("SELECT status FROM support_operations WHERE operation_id='receipt'")['status'] == 'unknown'
