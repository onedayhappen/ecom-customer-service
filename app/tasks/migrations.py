"""Version 10: extend the existing task table, without migrating live workflows."""
import json


def json_value(value, fallback=None):
    if value is None:
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def migrate_pipeline_rollout(tx):
    from app.tasks.rollout import RELEASE
    from app.support.store import now
    tx.execute('''CREATE TABLE IF NOT EXISTS task_pipeline_state (
        singleton INTEGER PRIMARY KEY, release TEXT NOT NULL, mode TEXT NOT NULL,
        version INTEGER NOT NULL, updated_at TEXT NOT NULL)''')
    # An existing installation starts stopped. A new empty installation has no
    # old writers or customer state to migrate.
    populated = tx.one('SELECT COUNT(*) AS n FROM support_conversations')['n']
    tx.execute('INSERT INTO task_pipeline_state VALUES (1,?,?,1,?) ON CONFLICT DO NOTHING',
               (RELEASE, 'stopped' if populated else 'all', now()))
    tx.execute('''CREATE TABLE IF NOT EXISTS task_pipeline_users (
        user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL)''')
    tx.execute('''CREATE TABLE IF NOT EXISTS task_rollout_audit (
        event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, actor TEXT NOT NULL,
        user_id TEXT, mode TEXT NOT NULL, reason TEXT NOT NULL, version INTEGER NOT NULL)''')
    tx.execute('''CREATE TABLE IF NOT EXISTS task_legacy_imports (
        legacy_task_id TEXT PRIMARY KEY, legacy_version INTEGER NOT NULL, user_id TEXT NOT NULL,
        task_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)''')


def encoded(tx, value):
    """Use native JSONB in PostgreSQL and canonical JSON in the SQLite adapter."""
    if tx.postgres:
        from psycopg.types.json import Jsonb
        return Jsonb(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def migrate_task_storage(tx):
    json_type = "JSONB" if tx.postgres else "TEXT"
    if tx.postgres:
        columns = {r["column_name"] for r in tx.all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='conversation_tasks'")}
    else:
        columns = {r["name"] for r in tx.all("PRAGMA table_info(conversation_tasks)")}
    for name, definition in (
        ("user_id", "TEXT"),
        ("goal", "TEXT"),
        ("details", f"{json_type} NOT NULL DEFAULT '{{}}'"),
        ("waiting_for", json_type),
        ("storage_mode", "TEXT NOT NULL DEFAULT 'legacy'"),
    ):
        if name not in columns:
            tx.execute(f"ALTER TABLE conversation_tasks ADD COLUMN {name} {definition}")
    tx.execute("DROP INDEX IF EXISTS conversation_one_active_task")
    tx.execute("""CREATE INDEX IF NOT EXISTS conversation_tasks_user
        ON conversation_tasks(user_id,updated_at,task_id)""")
    tx.execute("""CREATE TABLE IF NOT EXISTS task_sessions (
        task_id TEXT NOT NULL, session_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(task_id,session_id))""")
    tx.execute("""CREATE TABLE IF NOT EXISTS task_changes (
        user_id TEXT NOT NULL, change_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        task_id TEXT NOT NULL, session_id TEXT NOT NULL, sequence INTEGER,
        version INTEGER NOT NULL, PRIMARY KEY(user_id,change_id))""")
    tx.execute("""CREATE INDEX IF NOT EXISTS task_changes_history ON task_changes(task_id,version)""")

    # A sequence is local to a conversation, not globally unique. Preserve all
    # old links while replacing the legacy two-column primary key.
    tx.execute("""CREATE TABLE support_task_messages_v10 (
        task_id TEXT NOT NULL, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        PRIMARY KEY(task_id,session_id,sequence))""")
    tx.execute("""INSERT INTO support_task_messages_v10(task_id,session_id,sequence)
        SELECT task_id,session_id,sequence FROM support_task_messages""")
    tx.execute("DROP TABLE support_task_messages")
    tx.execute("ALTER TABLE support_task_messages_v10 RENAME TO support_task_messages")
    tx.execute("""CREATE INDEX support_task_messages_session
        ON support_task_messages(session_id,sequence)""")

    # No inferred owner: orphaned historical records stay inaccessible.
    tx.execute("""UPDATE conversation_tasks SET user_id=(
        SELECT c.user_id FROM support_conversations c WHERE c.session_id=conversation_tasks.session_id)
        WHERE user_id IS NULL""")
    for row in tx.all("SELECT * FROM conversation_tasks WHERE goal IS NULL"):
        details = json_value(row["slots"], {})
        tx.execute("UPDATE conversation_tasks SET goal=?,details=?,waiting_for=? WHERE task_id=?",
                   (row["request_text"] or row["title"], encoded(tx, details),
                    encoded(tx, json_value(row["question"])), row["task_id"]))
    tx.execute("""INSERT INTO task_sessions(task_id,session_id,created_at)
        SELECT task_id,session_id,created_at FROM conversation_tasks WHERE user_id IS NOT NULL
        ON CONFLICT DO NOTHING""")


def migrate_message_intake(tx):
    tx.execute("ALTER TABLE runtime_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'conversation'")
    tx.execute("""CREATE INDEX runtime_jobs_kind ON runtime_jobs(kind,status,created_at)""")
    tx.execute("""CREATE TABLE message_intake_sessions (
        session_id TEXT PRIMARY KEY, received_sequence INTEGER NOT NULL DEFAULT 0,
        associated_sequence INTEGER NOT NULL DEFAULT 0,
        active_job_id TEXT, lease_owner TEXT)""")
    tx.execute("""CREATE TABLE message_intake_items (
        user_id TEXT NOT NULL, request_id TEXT NOT NULL, session_id TEXT NOT NULL,
        sequence INTEGER NOT NULL, request_hash TEXT NOT NULL,
        association_status TEXT NOT NULL DEFAULT 'pending', decision_id TEXT,
        PRIMARY KEY(user_id,request_id), UNIQUE(session_id,sequence))""")
    tx.execute("""CREATE INDEX message_intake_pending
        ON message_intake_items(session_id,sequence) WHERE association_status='pending'""")


def migrate_association(tx):
    tx.execute("""CREATE TABLE association_runs (
        job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        proposal_hash TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)""")
    tx.execute("""CREATE TABLE association_decisions (
        decision_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, user_id TEXT NOT NULL,
        session_id TEXT NOT NULL, task_id TEXT, event_sequence INTEGER NOT NULL,
        change_id TEXT, action TEXT NOT NULL)""")
    tx.execute("""CREATE INDEX association_task_decisions ON association_decisions(user_id,task_id)""")
    tx.execute("""CREATE TABLE association_sources (
        decision_id TEXT NOT NULL, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        PRIMARY KEY(decision_id,session_id,sequence))""")
    tx.execute("""CREATE TABLE association_clarifications (
        clarification_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        question TEXT NOT NULL, candidates TEXT NOT NULL, sources TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', version INTEGER NOT NULL DEFAULT 1,
        answer_decision_id TEXT, created_at TEXT NOT NULL)""")
    tx.execute("""CREATE INDEX association_open_questions ON association_clarifications(user_id,session_id,status)""")


def migrate_corrections(tx):
    tx.execute("""CREATE TABLE task_corrections (
        user_id TEXT NOT NULL, correction_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        decision_id TEXT NOT NULL, session_id TEXT NOT NULL, result TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(user_id,correction_id))""")
    tx.execute("""CREATE TABLE task_invalid_sources (
        user_id TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL,
        correction_id TEXT NOT NULL, PRIMARY KEY(user_id,kind,source_id))""")
    tx.execute("""CREATE TABLE task_provenance_users (
        user_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)""")
    tx.execute("""CREATE TABLE support_operation_tasks (
        operation_id TEXT NOT NULL, task_id TEXT NOT NULL,
        PRIMARY KEY(operation_id,task_id))""")


def migrate_task_execution(tx):
    task = "(payload::jsonb->>'task_id')" if tx.postgres else "json_extract(payload,'$.task_id')"
    tx.execute(f"""CREATE UNIQUE INDEX runtime_one_task_execution ON runtime_jobs({task})
        WHERE kind='task_execution' AND status='running'""")


def migrate_task_scheduling(tx):
    # Requests and dependencies remain in the existing Outbox. This index also
    # covers pending versions; the running-only unique index still fences workers.
    task = "(payload::jsonb->>'task_id')" if tx.postgres else "json_extract(payload,'$.task_id')"
    tx.execute(f"""CREATE INDEX runtime_task_pending ON runtime_jobs({task},created_at)
        WHERE kind='task_execution' AND status IN ('pending','dispatched')""")


def migrate_coordinated_replies(tx):
    tx.execute("""CREATE TABLE task_reply_items (
        user_id TEXT NOT NULL, item_key TEXT NOT NULL, session_id TEXT NOT NULL,
        sequence INTEGER, disposition TEXT NOT NULL, created_at TEXT NOT NULL,
        checked_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(user_id,item_key))""")
    tx.execute("""CREATE TABLE task_reply_sessions (
        session_id TEXT PRIMARY KEY, checked_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        pending_notice INTEGER NOT NULL DEFAULT 0)""")
    tx.execute("""CREATE TABLE task_execution_wakes (
        task_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
        job_id TEXT, retry_at DOUBLE PRECISION NOT NULL DEFAULT 0)""")
    tx.execute("""INSERT INTO task_execution_wakes(task_id)
        SELECT task_id FROM conversation_tasks WHERE storage_mode='durable'""")
    tx.execute("""INSERT INTO task_reply_sessions(session_id) SELECT session_id FROM message_intake_sessions""")
    tx.execute("""CREATE INDEX task_reply_session_scan ON task_reply_sessions(checked_at,session_id)""")
    tx.execute("""CREATE INDEX runtime_task_reply_scan ON runtime_jobs(user_id,session_id,created_at)
        WHERE kind='task_execution' AND status IN ('completed','interrupted','expired')""")


def migrate_task_operations(tx):
    # Execution status/results stay in support_operations. This table holds
    # only the immutable proposal and its authorization dependencies.
    tx.execute("""CREATE TABLE task_operation_proposals (
        operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, task_id TEXT NOT NULL,
        task_version INTEGER NOT NULL, payload TEXT NOT NULL, token_hash TEXT NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL, run_deadline DOUBLE PRECISION,
        created_at TEXT NOT NULL)""")
    tx.execute("""CREATE INDEX task_operation_owner ON task_operation_proposals(user_id,task_id,created_at)""")
    tx.execute("""CREATE INDEX task_operation_recovery ON task_operation_proposals(run_deadline)
        WHERE run_deadline IS NOT NULL""")
    tx.execute("""CREATE TABLE task_operation_resources (
        resource_key TEXT PRIMARY KEY, operation_id TEXT NOT NULL)""")


def migrate_task_handoffs(tx):
    tx.execute("""CREATE TABLE task_handoffs (
        ticket_id TEXT NOT NULL, task_id TEXT NOT NULL, user_id TEXT NOT NULL,
        task_version INTEGER NOT NULL, snapshot TEXT NOT NULL,
        acquired_at TEXT NOT NULL, released_at TEXT,
        PRIMARY KEY(ticket_id,task_id))""")
    tx.execute("""CREATE INDEX task_handoff_active ON task_handoffs(user_id,task_id)
        WHERE released_at IS NULL""")
    # Existing non-bot conversations must retain their constraints after upgrade.
    from app.support.store import now
    rows = tx.all("""SELECT c.ticket_id,t.task_id,t.user_id,t.version FROM conversation_tasks t
        JOIN task_sessions s ON s.task_id=t.task_id
        JOIN support_conversations c ON c.session_id=s.session_id
        WHERE t.storage_mode='durable' AND c.mode<>'bot' AND c.ticket_id IS NOT NULL
        AND t.status NOT IN ('completed','cancelled')""")
    for row in rows:
        tx.execute('INSERT INTO task_handoffs VALUES (?,?,?,?,?,?,NULL) ON CONFLICT DO NOTHING',
            (row['ticket_id'], row['task_id'], row['user_id'], row['version'], '{}', now()))


def migrate_external_progress(tx):
    tx.execute('''CREATE TABLE task_external_waits (
        task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        task_version INTEGER NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_check DOUBLE PRECISION NOT NULL,
        lease_owner TEXT, lease_until DOUBLE PRECISION,
        last_observation TEXT, last_error TEXT)''')
    tx.execute('''CREATE INDEX task_external_due ON task_external_waits(state,next_check)''')
    tx.execute('''CREATE TABLE task_external_receipts (
        task_id TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(task_id,event_id))''')

