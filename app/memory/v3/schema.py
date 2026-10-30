# 文件说明：定义与支持库共享的 v3 记忆事实、版本、任务及归档表结构。
"""Shared support database: SQLite tests and PostgreSQL deployment."""
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS memory_v3_users (
        user_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
        migrated INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS memory_v3_records (
        user_id TEXT NOT NULL, memory_id TEXT NOT NULL, family TEXT NOT NULL,
        status TEXT NOT NULL, observed_at TEXT NOT NULL, valid_from TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(user_id,memory_id))''',
    '''CREATE INDEX IF NOT EXISTS memory_v3_current ON memory_v3_records(user_id,status,observed_at)''',
    '''CREATE INDEX IF NOT EXISTS memory_v3_family ON memory_v3_records(user_id,family)''',
    '''CREATE INDEX IF NOT EXISTS memory_v3_scheduled ON memory_v3_records(user_id,status,valid_from)''',
    '''CREATE TABLE IF NOT EXISTS memory_v3_forgets (
        user_id TEXT NOT NULL, command_id TEXT NOT NULL, family TEXT NOT NULL,
        value_json TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(user_id,command_id))''',
    '''CREATE INDEX IF NOT EXISTS memory_v3_forget_scope ON memory_v3_forgets(user_id,family)''',
    '''CREATE TABLE IF NOT EXISTS memory_v3_receipts (
        user_id TEXT NOT NULL, batch_id TEXT NOT NULL, session_id TEXT NOT NULL,
        through_sequence INTEGER NOT NULL, revision INTEGER NOT NULL,
        status TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(user_id,batch_id))''',
    '''CREATE TABLE IF NOT EXISTS memory_v3_jobs (
        session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, requested INTEGER NOT NULL,
        through_sequence INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, available_at DOUBLE PRECISION NOT NULL,
        lease_token TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        last_error TEXT)''',
    '''CREATE INDEX IF NOT EXISTS memory_v3_due ON memory_v3_jobs(status,available_at)''',
    '''CREATE TABLE IF NOT EXISTS memory_v3_archives (
        archive_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        through_sequence INTEGER NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(user_id,session_id,through_sequence))''',
)
