# 文件说明：将持久诊断事件投影为每次交互一条的脱敏 JSONL 日志。
"""Durable diagnostic journal projected into one JSONL row per interaction.

The support event transaction defines message/reply boundaries. Model and tool
observations use a separate journal, never changing customer event cursors.
Files are recoverable snapshots, atomically replaced after database commit.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from functools import wraps
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

from app.config.settings import settings

log = logging.getLogger(__name__)
_binding = ContextVar("interaction_diagnostics", default=None)
_span_id = ContextVar("interaction_call_id", default=None)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS interaction_trace_sessions (
        session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
        exported_revision INTEGER NOT NULL DEFAULT -1, pending INTEGER NOT NULL DEFAULT 1,
        exported_at DOUBLE PRECISION NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS interaction_trace_events (
        event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, anchor_sequence INTEGER NOT NULL,
        created_at TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
        job_id TEXT, task_id TEXT, payload TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS interaction_trace_order
        ON interaction_trace_events(session_id,anchor_sequence,created_at,event_id)""",
)

_PRIVATE = {"password", "secret", "token", "confirmation_token", "access_token", "refresh_token",
            "api_key", "authorization", "cookie", "set-cookie", "business_data_keys", "sealed",
            "phone", "mobile", "telephone", "address", "email", "tax_id", "tax_number", "id_card", "invoice_title",
            "手机号", "电话", "姓名", "地址", "身份证", "密码", "验证码"}


def sanitize(value):
    """Also decode JSON embedded in tool arguments and model message content."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        if value.get('source_type') == 'image_observation' or value.get('kind') == 'image_observation':
            value = {k: '[REDACTED]' if k in {'result', 'text', 'selection_hint', 'context_sources'} else v
                     for k, v in value.items()}
        sensitive_detail = value.get("field") in _PRIVATE
        return {str(k): "[REDACTED]" if (str(k).lower() in _PRIVATE
                or str(k).lower().endswith(("_token", "_password", "_secret", "_api_key", "_phone", "_address"))
                or (sensitive_detail and k in {"value", "evidence"})) else sanitize(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            return json.dumps(sanitize(decoded), ensure_ascii=False)
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE]", value)
        value = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "[ID]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        value = re.sub(r'''(?i)(\b(?:confirmation_token|access_token|refresh_token|api_key|password|secret)\b["']?\s*[:=]\s*["']?)[^\s,}"']+''',
                       r"\1[REDACTED]", value)
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def enabled():
    return settings.observability_enabled and settings.interaction_trace_enabled


@contextmanager
def bind(store, session_id, *, job_id=None, task_id=None):
    token = _binding.set(dict(store=store, session_id=session_id, job_id=job_id, task_id=task_id))
    try:
        yield
    finally:
        _binding.reset(token)


def record(stage, *, status="ok", store=None, session_id=None, tx=None,
           job_id=None, task_id=None, anchor=None, event_id=None, **data):
    if not enabled():
        return
    binding = _binding.get() or {}
    store, session_id = store or binding.get("store"), session_id or binding.get("session_id")
    if session_id is None or (tx is None and store is None):
        return
    job_id, task_id = job_id or binding.get("job_id"), task_id or binding.get("task_id")
    event_id = event_id or str(uuid4())
    try:
        if _span_id.get() is not None:
            data.setdefault("call_id", _span_id.get())
        payload = json.dumps(sanitize(data), ensure_ascii=False, allow_nan=False)
        if tx is None:
            with store.transaction() as connection:
                _insert(connection, session_id, stage, status, job_id, task_id, anchor, event_id, payload)
        else:
            # A logging failure must not poison the caller's PostgreSQL transaction.
            tx.execute("SAVEPOINT interaction_observation")
            try:
                _insert(tx, session_id, stage, status, job_id, task_id, anchor, event_id, payload)
            except Exception:
                tx.execute("ROLLBACK TO SAVEPOINT interaction_observation")
                raise
            finally:
                tx.execute("RELEASE SAVEPOINT interaction_observation")
    except Exception:
        log.warning("Interaction journal write failed for stage %s", stage, exc_info=True)


def _insert(tx, sid, stage, status, job, task, anchor, eid, payload):
    if anchor is None:
        row = tx.one("SELECT sequence FROM support_conversations WHERE session_id=?", (sid,))
        if row is None:
            return
        anchor = row["sequence"]
    inserted = tx.execute("""INSERT INTO interaction_trace_events VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(event_id) DO NOTHING""", (eid, sid, anchor, datetime.now(timezone.utc).isoformat(),
                                              stage, status, job, task, payload)).rowcount
    if inserted:
        tx.execute("""INSERT INTO interaction_trace_sessions(session_id,revision) VALUES (?,1)
            ON CONFLICT(session_id) DO UPDATE SET revision=interaction_trace_sessions.revision+1""", (sid,))
        if not hasattr(tx, "trace_sessions"):
            tx.trace_sessions = set()
        tx.trace_sessions.add(sid)


def support_event(tx, control, kind, payload, private):
    role = payload.get("role") if kind == "message" and not private else None
    stage = "input.received" if role == "user" else "reply.published" if role in {"assistant", "agent"} else kind
    record(stage, tx=tx, session_id=control["session_id"], anchor=control["sequence"],
           event_id=f"support:{control['session_id']}:{control['sequence']}",
           job_id=payload.get("job_id"), task_id=payload.get("task_id"),
           output=payload, source_sequence=control["sequence"], visibility="internal" if private else "public")


@contextmanager
def span(stage, **inputs):
    call_id, start = str(uuid4()), time.perf_counter()
    record(stage + ".started", status="running", call_id=call_id, parent_call_id=_span_id.get(), input=inputs)
    token = _span_id.set(call_id)
    result = {}
    try:
        yield result
    except BaseException as exc:
        record(stage + ".finished", status="error", call_id=call_id,
               duration_ms=round((time.perf_counter() - start) * 1000, 2),
               error={"type": type(exc).__name__, "message": str(exc)})
        raise
    else:
        record(stage + ".finished", call_id=call_id,
               duration_ms=round((time.perf_counter() - start) * 1000, 2), **result)
    finally:
        _span_id.reset(token)


def model_output(response):
    # Capture public model output and tool requests, never hidden reasoning fields.
    choices = getattr(response, "choices", []) or []
    return dict(model=getattr(response, "model", None), choices=[{
        "content": getattr(choice.message, "content", None),
        "finish_reason": getattr(choice, "finish_reason", None),
        "tool_calls": [{"id": getattr(call, "id", None), "name": call.function.name,
                        "arguments": call.function.arguments}
                       for call in (getattr(choice.message, "tool_calls", None) or [])],
    } for choice in choices], usage={key: getattr(getattr(response, "usage", None), key, None)
                                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")})


def observed_model(function):
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(self, params):
            with span("model", request=params) as result:
                response = await function(self, params)
                result["output"] = model_output(response)
                return response
        return asynchronous
    @wraps(function)
    def synchronous(self, params):
        with span("model", request=params) as result:
            response = function(self, params)
            result["output"] = model_output(response)
            return response
    return synchronous


def call_model(client, params):
    from app.observability.llm_client import LlmClient
    if isinstance(client, LlmClient):
        return client.chat.completions.create(**params)
    with span("model", request=params) as result:
        record("model.request", input=params)
        response = client.chat.completions.create(**params)
        result["output"] = model_output(response)
        return response


def observed_association(function):
    @wraps(function)
    def wrapped(self, batch, engine):
        with bind(self.store, batch.session_id, job_id=batch.job_id):
            with span("association", messages=batch.messages) as result:
                output = function(self, batch, engine)
                result["output"] = output
                return output
    return wrapped


def observed_tool(function):
    @wraps(function)
    def wrapped(self, name, arguments):
        with span("tool", tool=name, arguments=arguments) as result:
            output = function(self, name, arguments)
            decoded = json.loads(output) if isinstance(output, str) else output
            result["output"] = decoded
            if isinstance(decoded, dict) and decoded.get("success") is False:
                result["status"] = "error"
            return output
    return wrapped


def observed_execution(function):
    @wraps(function)
    def wrapped(self, request, engine, reservation):
        with bind(self.runs.store, request.session_id, job_id=request.job_id, task_id=request.task_id):
            with span("execution", request=request, task=reservation["record"]) as result:
                output = function(self, request, engine, reservation)
                result["output"] = output
                if output.get("state") in {"interrupted", "deferred", "expired"}:
                    result["status"] = "error"
                return output
    return wrapped


def assemble(sid, events, *, at=None):
    """Formal replies close rows; progress notices and worker batches do not.

    A second reply without new input is a follow-up row. Explicit job references
    copy causal stages from earlier rows, so a split/late reply remains inspectable.
    """
    at = at or time.time()
    rows, current, history = [], [], []
    def finish(items, reply=None):
        first = items[0]
        jobs = {item["job_id"] for item in items if item["job_id"]}
        if reply:
            jobs.update(key[4:] for key in reply["data"]["output"].get("result_ids", []) if key.startswith("job:"))
        related = [item for item in history if item not in items and item["job_id"] in jobs
                   and item["stage"] not in {"input.received", "reply.published"}]
        steps = related + [item for item in items if item["stage"] != "input.received"]
        last = datetime.fromisoformat(items[-1]["created_at"]).timestamp()
        state = "completed" if reply else "processing"
        lifecycle = next((item for item in reversed(items) if item["stage"] in {
            "execution.started", "execution.finished", "execution.saved", "association.started",
            "association.finished", "schedule.enqueued", "schedule.expired"}), None)
        # A timed-out SDK thread can return later. That is useful evidence, but
        # must not change the failed execution into a healthy pending turn.
        if not reply and (items[-1]["status"] == "error" or (lifecycle and lifecycle["status"] == "error")):
            state = "failed"
        elif not reply and at - last > settings.interaction_trace_stale_seconds:
            state = "interrupted"
        return dict(schema_version=1, session_id=sid,
                    interaction_id=hashlib.sha256((sid + ":" + first["event_id"]).encode()).hexdigest()[:24],
                    status=state, started_at=first["created_at"],
                    finished_at=reply["created_at"] if reply else None,
                    duration_ms=round((datetime.fromisoformat(items[-1]["created_at"]).timestamp()
                                       - datetime.fromisoformat(first["created_at"]).timestamp()) * 1000, 2),
                    user_messages=[dict(sequence=item["data"]["source_sequence"], **item["data"]["output"])
                                   for item in items if item["stage"] == "input.received"],
                    assistant_reply=reply["data"]["output"] if reply else None,
                    job_ids=sorted(jobs), task_ids=sorted({item["task_id"] for item in steps if item["task_id"]}),
                    steps=steps, errors=[item for item in steps if item["status"] == "error"])
    for event in events:
        event = {**event, "data": json.loads(event["payload"])}
        del event["payload"]
        current.append(event)
        if event["stage"] == "reply.published":
            rows.append(finish(current, event))
            history.extend(current)
            current = []
    if current:
        if rows and not any(item["stage"] == "input.received" for item in current):
            # Post-publication bookkeeping is not a new customer interaction.
            rows[-1]["steps"].extend(current)
            rows[-1]["errors"].extend(item for item in current if item["status"] == "error")
        else:
            rows.append(finish(current))
    return rows


def trace_path(sid):
    safe = sid if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sid) else hashlib.sha256(sid.encode()).hexdigest()
    return Path(settings.observability_log_dir) / "conversations" / ("session-" + safe + ".jsonl")


class ExportBusy(Exception):
    """Another process is already writing this session snapshot."""


@contextmanager
def _file_lock(path):
    """Cross-process exclusion, including API/worker exporters on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                import errno
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ExportBusy from exc
                raise
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExportBusy from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def export_session(store, sid):
    if not enabled():
        return None
    path, temporary = trace_path(sid), None
    try:
        with _file_lock(path.parent / ".locks" / (path.stem + ".lock")):
            # Read after taking the file lock: an older snapshot cannot overwrite a newer one.
            with store.transaction(read_only=True) as tx:
                revision = tx.one("SELECT revision FROM interaction_trace_sessions WHERE session_id=?", (sid,))
                if revision is None:
                    return None
                events = tx.all("""SELECT * FROM interaction_trace_events WHERE session_id=?
                    ORDER BY anchor_sequence,created_at,event_id""", (sid,))
            rows = assemble(sid, events)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.stem + ".", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            with store.transaction() as tx:
                tx.execute("""UPDATE interaction_trace_sessions SET exported_revision=?,exported_at=?,pending=?
                    WHERE session_id=?""", (revision["revision"], time.time(),
                                           int(bool(rows and rows[-1]["assistant_reply"] is None)), sid))
        return path
    except ExportBusy:
        return None  # The unexported revision remains discoverable by the worker.
    except Exception:
        log.warning("Interaction log export deferred; journal retained for recovery", exc_info=True)
        return None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def flush_pending(store, limit=32):
    if not enabled():
        return
    try:
        with store.transaction(read_only=True) as tx:
            rows = tx.all("""SELECT session_id FROM interaction_trace_sessions
                WHERE revision<>exported_revision OR (pending=1 AND exported_at<?)
                ORDER BY exported_at LIMIT ?""", (time.time() - 60, limit))
        for row in rows:
            export_session(store, row["session_id"])
    except Exception:
        log.warning("Interaction log recovery deferred", exc_info=True)
