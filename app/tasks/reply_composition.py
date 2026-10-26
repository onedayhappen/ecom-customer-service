"""Immutable reply inputs and their publication fence; no model or DB writes."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import settings
from app.memory.source_policy import message_content
from app.tasks.provenance import context_event_valid, revision


@dataclass(frozen=True)
class ReplyDraft:
    context: dict
    fingerprint: str
    fallback: str


def snapshot(coordinator, tx, control, pieces, received):
    sid, user = control["session_id"], coordinator.user
    rows = tx.all("""SELECT sequence,payload,created_at FROM support_events
        WHERE session_id=? AND kind='message' AND visibility='public'
        ORDER BY sequence DESC LIMIT 80""", (sid,))
    messages = []
    for row in reversed(rows):
        if not context_event_valid(tx, sid, row["sequence"]):
            continue
        p = json.loads(row["payload"])
        if p.get("role") in {"user", "assistant", "agent"}:
            messages.append(dict(sequence=row["sequence"], role=p["role"],
                                 content=message_content(p), created_at=row["created_at"]))
    # Include the latest user burst even when one matter has already replied.
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
    start = last_user
    while start > 0 and messages[start - 1]["role"] == "user":
        start -= 1
    current = messages[start:last_user + 1] if last_user >= 0 else []
    history = messages[:start] + messages[last_user + 1:] if last_user >= 0 else messages
    matters, notices, fallbacks = [], [], []
    for piece in pieces:
        fixed = any(piece.get(k) for k in ("question_id", "clarification_id", "operation_id"))
        if fixed:
            notices.append(piece["content"])
        for tid in piece["task_versions"]:
            record = coordinator.runs.repo._record(coordinator.runs.repo._row(tx, tid))
            matters.append(dict(task_id=tid, goal=record.goal, status=record.status,
                                order_id=record.details.get("order_id"),
                                execution_state=piece.get("execution_state"),
                                verified_facts="" if fixed else piece["content"],
                                evidence_ids=piece.get("evidence_ids", [])))
        if not fixed:
            fallbacks.append(piece.get("fallback", piece["content"]))
    selected = {tid for piece in pieces for tid in piece["task_versions"]}
    task_field = "j.payload::jsonb->>'task_id'" if tx.postgres else "json_extract(j.payload,'$.task_id')"
    other = tx.all(f"""SELECT t.task_id,t.goal,t.status,t.version FROM conversation_tasks t
        JOIN task_sessions s ON s.task_id=t.task_id WHERE t.user_id=? AND s.session_id=?
        AND t.status IN ('in_progress','waiting_external')
        AND (t.status='waiting_external' OR EXISTS(SELECT 1 FROM runtime_jobs j
            WHERE j.user_id=t.user_id AND j.kind='task_execution' AND {task_field}=t.task_id
            AND j.status IN ('pending','dispatched','running')))
        ORDER BY t.task_id LIMIT 13""", (user, sid))
    other = [r for r in other if r["task_id"] not in selected]
    try:
        zone = ZoneInfo(settings.memory_timezone)
    except ZoneInfoNotFoundError:
        if settings.memory_timezone != "Asia/Shanghai":
            raise
        zone = timezone(timedelta(hours=8))
    context = dict(today=datetime.now(zone).date().isoformat(), timezone=settings.memory_timezone,
                   current_messages=current, recent_messages=history[-20:],
                   current_messages_incomplete=bool(current and start == 0 and len(rows) == 80),
                   history_omitted=len(history) > 20 or len(rows) == 80, matters=matters,
                   required_notices=notices, other_matters=other[:12], other_matters_omitted=len(other) > 12)
    # All session inputs fence cross-session corrections even if already associated.
    inputs = tx.all("""SELECT i.session_id,i.received_sequence FROM message_intake_sessions i
        JOIN support_conversations c ON c.session_id=i.session_id
        WHERE c.user_id=? ORDER BY i.session_id""", (user,))
    fence = dict(context=context, pieces=sorted(pieces, key=lambda p: p["key"]),
                 received=received, inputs=inputs, control_version=control["control_version"],
                 revision=revision(tx, user))
    fingerprint = sha256(json.dumps(fence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return ReplyDraft(context, fingerprint, "\n\n".join(fallbacks + notices))
