SCHEMA = (
    """CREATE TABLE IF NOT EXISTS media_assets (
        attachment_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, origin_session_id TEXT NOT NULL,
        upload_request_id TEXT NOT NULL, storage_key TEXT NOT NULL, sha256 TEXT NOT NULL,
        mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
        variants TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
        created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
        UNIQUE(user_id,upload_request_id))""",
    """CREATE TABLE IF NOT EXISTS message_attachments (
        user_id TEXT NOT NULL, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        attachment_id TEXT NOT NULL, ordinal INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(session_id,sequence,attachment_id),
        UNIQUE(session_id,sequence,ordinal))""",
    """CREATE TABLE IF NOT EXISTS image_observations (
        observation_id TEXT PRIMARY KEY, attachment_id TEXT NOT NULL, generation INTEGER NOT NULL,
        user_id TEXT NOT NULL, task_id TEXT, input_hash TEXT NOT NULL, purpose TEXT NOT NULL,
        detail TEXT NOT NULL, question TEXT NOT NULL, context_sources TEXT NOT NULL,
        context_digest TEXT NOT NULL, model_revision TEXT NOT NULL, prompt_version TEXT NOT NULL,
        result TEXT NOT NULL, reuse_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'valid',
        supersedes_id TEXT, created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS image_jobs (
        job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        attachment_id TEXT NOT NULL, generation INTEGER NOT NULL, request_key TEXT NOT NULL UNIQUE,
        input_snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', lease_owner TEXT,
        lease_until DOUBLE PRECISION NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        deadline_at DOUBLE PRECISION NOT NULL, next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        observation_id TEXT, error_code TEXT)""",
    """CREATE INDEX IF NOT EXISTS image_job_poll ON image_jobs(status,next_attempt_at,deadline_at)""",
    """CREATE INDEX IF NOT EXISTS image_job_message ON image_jobs(session_id,sequence)""",
    """CREATE TABLE IF NOT EXISTS task_attachments (
        task_id TEXT NOT NULL, attachment_id TEXT NOT NULL, source_session_id TEXT NOT NULL,
        source_sequence INTEGER NOT NULL, decision_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(task_id,attachment_id,source_session_id,source_sequence,decision_id))""",
)
