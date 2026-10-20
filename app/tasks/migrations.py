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

