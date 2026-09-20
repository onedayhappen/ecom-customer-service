import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.db import ensure_schema_once, get_connection_pool

SESSION_VERSION = 5

#: 本地 json 会话目录。固定到项目根目录，避免服务从不同 CWD 启动时读写不同数据。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
JSON_SESSION_DIR = PROJECT_ROOT / "data" / "sessions" / "sessions"


class SessionConflict(RuntimeError):
    """会话被其他请求更新，调用方应重新读取后重试。"""


def _json_session_path(session_id: str) -> Path:
    """本地 json 会话文件路径：项目根目录下 data/sessions/sessions/{session_id}.json。"""
    return JSON_SESSION_DIR / f"{session_id}.json"


class PostgresSessionRepository:
    """API 会话的 PostgreSQL 主存储：状态一行、每条原始消息一行。

    连接复用进程级共享连接池（:mod:`app.db`），避免每次读写都新建连接；
    建表 DDL 由进程级守卫保证只执行一次，避免高并发下反复拿元数据锁。
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._ensure_tables()

    def _connect(self):
        pool = get_connection_pool(self.dsn)
        if pool is not None:
            return pool.connection()
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("未安装 psycopg，请执行 pip install 'psycopg[binary]'") from error
        return psycopg.connect(self.dsn)

    def _ensure_tables(self) -> None:
        ensure_schema_once(self.dsn, "agent_sessions", self._schema_ddl)

    @staticmethod
    def _schema_ddl(cur) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id UUID PRIMARY KEY,
                user_id TEXT NOT NULL,
                summary TEXT,
                metadata JSONB NOT NULL,
                version BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # 兼容已经存在的旧表；DDL 在每个进程只执行一次，但迁移语句本身幂等。
        cur.execute(
            "ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_session_messages (
                session_id UUID NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
                sequence_no INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence_no)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS agent_sessions_user_updated_idx "
            "ON agent_sessions (user_id, updated_at DESC)"
        )

    def load(self, session_id: str, user_id: str | None = None) -> Optional[dict]:
        query = "SELECT summary, metadata, version FROM agent_sessions WHERE session_id = %s"
        params: tuple = (session_id,)
        if user_id is not None:
            query += " AND user_id = %s"
            params = (session_id, user_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT role, content FROM agent_session_messages "
                "WHERE session_id = %s ORDER BY sequence_no",
                (session_id,),
            )
            messages = [{"role": role, "content": content} for role, content in cur.fetchall()]
        summary, metadata, version = row
        return {
            "summary": summary,
            "messages": messages,
            "metadata": _normalise_metadata(metadata),
            "state_version": int(version or 0),
        }

    def save(
        self,
        session_id: str,
        messages: list[dict],
        summary: Optional[str],
        metadata: dict,
        *,
        expected_version: int | None = None,
    ) -> int:
        user_id = metadata.get("owner_user_id")
        if not user_id:
            raise ValueError("PostgreSQL 会话必须包含 owner_user_id")
        serialized_metadata = json.dumps(metadata, ensure_ascii=False)
        with self._connect() as conn, conn.cursor() as cur:
            if expected_version is None:
                cur.execute(
                    """
                    INSERT INTO agent_sessions (session_id, user_id, summary, metadata, version)
                    VALUES (%s, %s, %s, %s::jsonb, 1)
                    ON CONFLICT (session_id) DO UPDATE SET
                        summary = EXCLUDED.summary,
                        metadata = EXCLUDED.metadata,
                        version = agent_sessions.version + 1,
                        updated_at = NOW()
                    WHERE agent_sessions.user_id = EXCLUDED.user_id
                    RETURNING version
                    """,
                    (session_id, user_id, summary, serialized_metadata),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO agent_sessions (session_id, user_id, summary, metadata, version)
                    VALUES (%s, %s, %s, %s::jsonb, 1)
                    ON CONFLICT (session_id) DO UPDATE SET
                        summary = EXCLUDED.summary,
                        metadata = EXCLUDED.metadata,
                        version = agent_sessions.version + 1,
                        updated_at = NOW()
                    WHERE agent_sessions.user_id = EXCLUDED.user_id
                      AND agent_sessions.version = %s
                    RETURNING version
                    """,
                    (session_id, user_id, summary, serialized_metadata, expected_version),
                )
            row = cur.fetchone()
            if row is None:
                # 区分“会话不存在/归属错误”和“版本冲突”，便于上层决定重试。
                cur.execute("SELECT user_id, version FROM agent_sessions WHERE session_id = %s", (session_id,))
                current = cur.fetchone()
                if current is None or current[0] != user_id:
                    raise PermissionError("会话不属于当前用户")
                raise SessionConflict(
                    f"会话版本冲突: expected={expected_version}, actual={current[1]}"
                )
            new_version = int(row[0])
            if expected_version is not None and new_version != expected_version + 1:
                raise SessionConflict("会话版本更新失败")
            # 编排器会压缩上下文；这里同步当前可恢复上下文，消息本身保持逐条存储。
            cur.execute("DELETE FROM agent_session_messages WHERE session_id = %s", (session_id,))
            cur.executemany(
                "INSERT INTO agent_session_messages (session_id, sequence_no, role, content) VALUES (%s, %s, %s, %s)",
                [(session_id, index, message.get("role", ""), str(message.get("content", "")))
                 for index, message in enumerate(messages)],
            )
            return new_version

    def update_metadata(self, session_id: str, user_id: str, **changes) -> Optional[dict]:
        loaded = self.load(session_id, user_id)
        if not loaded:
            return None
        metadata = loaded["metadata"]
        metadata.update(changes)
        self.save(
            session_id, loaded["messages"], loaded["summary"], metadata,
            expected_version=loaded.get("state_version", 0),
        )
        return metadata

    def delete(self, session_id: str, user_id: str | None = None) -> None:
        query = "DELETE FROM agent_sessions WHERE session_id = %s"
        params: tuple = (session_id,)
        if user_id is not None:
            query += " AND user_id = %s"
            params = (session_id, user_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, params)

    def list_for_user(self, user_id: str) -> list[tuple[str, dict]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT session_id::text FROM agent_sessions WHERE user_id = %s", (user_id,))
            session_ids = [row[0] for row in cur.fetchall()]
        return [(session_id, self.load(session_id, user_id)) for session_id in session_ids]

    def list_all(self) -> list[tuple[str, str]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT session_id::text, user_id FROM agent_sessions")
            return [(row[0], row[1]) for row in cur.fetchall()]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_session_metadata(owner_user_id: str | None = None) -> dict:
    """新会话的生命周期元数据。"""
    return {
        "status": "active",  # active / inactive / closed
        "owner_user_id": owner_user_id,
        "last_active_at": _now(),
        "closed_at": None,
        # explicit 表示用户主动结束；idle_timeout 才应在回访时提示恢复。
        "close_reason": None,
        # 已沉淀到 LTM 的 raw_messages 数量，恢复会话后只处理新增消息。
        "consolidated_message_count": 0,
    }


def _normalise_metadata(data: dict | None) -> dict:
    metadata = default_session_metadata()
    if isinstance(data, dict):
        metadata.update({key: value for key, value in data.items() if key in metadata})
    return metadata


def _write_session(file_path: Path, payload: dict) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file_path)


def save_session(
    path: str,
    messages: list[dict],
    summary: Optional[str],
    metadata: Optional[dict] = None,
    storage_backend: str = "json",
    postgres_dsn: str = "",
    expected_state_version: int | None = None,
) -> int:
    """把对话状态原子写入 JSON 文件。

    messages 只包含尚未压缩的原始条目（不含 system / summary）。
    会话短期状态由 summary 和 messages 组成。
    """
    normalised_metadata = _normalise_metadata(metadata)
    if storage_backend == "postgres":
        if not postgres_dsn:
            raise RuntimeError("API_SESSION_POSTGRES_DSN 未配置，无法保存 PostgreSQL 会话")
        return PostgresSessionRepository(postgres_dsn).save(
            path, messages, summary, normalised_metadata,
            expected_version=expected_state_version,
        )
    if storage_backend != "json":
        raise ValueError("session storage backend 仅支持 postgres 或 json")
    file_path = _json_session_path(path)
    payload = {
        "version": SESSION_VERSION,
        "updated_at": _now(),
        "summary": summary,
        "messages": messages,
        "metadata": normalised_metadata,
        "state_version": (expected_state_version + 1) if expected_state_version is not None else 1,
    }
    if expected_state_version is not None and file_path.exists():
        try:
            current = json.loads(file_path.read_text(encoding="utf-8"))
            actual = int(current.get("state_version", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            actual = 0
        if actual != expected_state_version:
            raise SessionConflict(
                f"会话版本冲突: expected={expected_state_version}, actual={actual}"
            )
    # 先写临时文件再原子替换，避免进程中断留下半截会话 JSON。
    _write_session(file_path, payload)
    return int(payload["state_version"])


def load_session(
    path: str, storage_backend: str = "json", postgres_dsn: str = "", user_id: str | None = None,
) -> Optional[dict]:
    """读取会话文件。不存在或损坏都返回 None（降级为新会话）。"""
    if storage_backend == "postgres":
        if not postgres_dsn:
            raise RuntimeError("API_SESSION_POSTGRES_DSN 未配置，无法读取 PostgreSQL 会话")
        return PostgresSessionRepository(postgres_dsn).load(path, user_id)
    if storage_backend != "json":
        raise ValueError("session storage backend 仅支持 postgres 或 json")
    file_path = _json_session_path(path)
    if not file_path.exists():
        return None

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  会话文件损坏，已忽略（{e}）")
        return None

    if not isinstance(data, dict) or "messages" not in data:
        print("⚠️  会话文件格式不识别，已忽略")
        return None

    return {
        "summary": data.get("summary"),
        "messages": data.get("messages", []),
        "metadata": _normalise_metadata(data.get("metadata")),
        "state_version": int(data.get("state_version", 0) or 0),
    }


def update_session_metadata(
    path: str, storage_backend: str = "json", postgres_dsn: str = "", user_id: str | None = None,
    **changes,
) -> Optional[dict]:
    """仅更新会话生命周期元数据，不改变消息与摘要。"""
    if storage_backend == "postgres":
        if not postgres_dsn or not user_id:
            raise RuntimeError("PostgreSQL 会话元数据更新需要 DSN 和 user_id")
        return PostgresSessionRepository(postgres_dsn).update_metadata(path, user_id, **changes)
    if storage_backend != "json":
        raise ValueError("session storage backend 仅支持 postgres 或 json")
    from app.memory.file_lock import ProcessFileLock
    file_path = _json_session_path(path)
    with ProcessFileLock(file_path.with_suffix(".lock")):
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict) or "messages" not in data:
            return None

        metadata = _normalise_metadata(data.get("metadata"))
        metadata.update(changes)
        data["version"] = SESSION_VERSION
        data["state_version"] = int(data.get("state_version", 0) or 0) + 1
        data["updated_at"] = _now()
        data["metadata"] = metadata
        _write_session(file_path, data)
        return metadata


def delete_session(
    path: str, storage_backend: str = "json", postgres_dsn: str = "", user_id: str | None = None,
) -> None:
    """删除会话文件，不存在时静默。"""
    if storage_backend == "postgres":
        if not postgres_dsn:
            raise RuntimeError("API_SESSION_POSTGRES_DSN 未配置，无法删除 PostgreSQL 会话")
        PostgresSessionRepository(postgres_dsn).delete(path, user_id)
        return
    if storage_backend != "json":
        raise ValueError("session storage backend 仅支持 postgres 或 json")
    file_path = _json_session_path(path)
    from app.memory.file_lock import ProcessFileLock
    with ProcessFileLock(file_path.with_suffix(".lock")):
        if file_path.exists():
            file_path.unlink()
