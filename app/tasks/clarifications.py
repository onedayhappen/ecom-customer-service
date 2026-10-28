"""One version contract for publishing and answering association questions."""
from dataclasses import dataclass
import json
from uuid import uuid4

from app.support.store import now


PUBLICATION_FIELDS = ("clarification_id", "clarification_version")


def publication(row):
    return dict(zip(PUBLICATION_FIELDS, (row["clarification_id"], row["version"])))


def published_event(tx, row, *, before_sequence=None):
    field = "payload::jsonb->>'clarification_id'" if tx.postgres else "json_extract(payload,'$.clarification_id')"
    role = "payload::jsonb->>'role'" if tx.postgres else "json_extract(payload,'$.role')"
    before, args = (" AND sequence<?", [before_sequence]) if before_sequence is not None else ("", [])
    return tx.one(f"""SELECT payload,sequence,created_at FROM support_events
        WHERE session_id=? AND kind='message' AND visibility='public'
        AND {field}=? AND {role} IN ('assistant','agent'){before}
        ORDER BY sequence DESC LIMIT 1""", (row["session_id"], row["clarification_id"], *args))


@dataclass(frozen=True)
class ClarificationState:
    published: bool
    candidates_current: bool
    status: str  # pending, active, invalid, closed

    @property
    def valid(self):
        return self.status == "active"


def assess(row, versions, event):
    options = json.loads(row["candidates"])
    current = all(versions.get(o["task_id"]) == o["version"] for o in options)
    payload = json.loads(event["payload"]) if event else {}
    published = (payload.get("role") in {"assistant", "agent"}
                 and all(payload.get(k) == v for k, v in publication(row).items()))
    if row["status"] != "open":
        status = "closed"
    elif not current or (event is not None and not published):
        status = "invalid"
    else:
        status = "active" if published else "pending"
    return ClarificationState(published, current, status)


def refresh_needed(tx, row, records, event, received):
    """Do not revive an idle chat's menu for changes made in another chat.

    Keep unresolved sources and reject the stale choice as before. When the
    customer returns, the new input permits one replacement with current versions.
    Legacy publication repairs and changes in this conversation still recover.
    """
    if event is None or received > event["sequence"]:
        return True
    changed = [o for o in json.loads(row["candidates"])
               if o["task_id"] not in records or records[o["task_id"]].version != o["version"]]
    if not changed:
        return True  # Repair missing/mismatched publication metadata.
    for option in changed:
        record = records.get(option["task_id"])
        if record is None:
            return True
        change = tx.one("""SELECT session_id FROM task_changes
            WHERE user_id=? AND task_id=? AND version=?""",
            (row["user_id"], record.task_id, record.version))
        if change is None or change["session_id"] == row["session_id"]:
            return True
    return False


def replace_invalid(tx, row, records):
    """Retire an unusable menu without discarding its unresolved user input.

    The caller holds this conversation's control lock and the candidate locks.
    A new ID means neither an old choice nor the old reply ledger can authorize
    or suppress the replacement. Historical public messages remain untouched.
    """
    sources = set(json.loads(row["sources"]))
    placeholders = ",".join("?" for _ in sources)
    pending = tx.all(f"""SELECT sequence FROM message_intake_items
        WHERE session_id=? AND user_id=? AND association_status='clarification'
        AND sequence IN ({placeholders})""", (row["session_id"], row["user_id"], *sources)) if sources else []
    sources.intersection_update(r["sequence"] for r in pending)
    tx.execute("UPDATE association_clarifications SET status='superseded' WHERE clarification_id=? AND status='open'",
               (row["clarification_id"],))
    if not sources:
        return None
    options = [dict(task_id=o["task_id"], version=records[o["task_id"]].version,
                    label=records[o["task_id"]].goal)
               for o in json.loads(row["candidates"]) if o["task_id"] in records]
    replacement = dict(row, clarification_id=str(uuid4()), version=1,
                       candidates=json.dumps(options, ensure_ascii=False), sources=json.dumps(sorted(sources)),
                       status="open", answer_decision_id=None, created_at=now())
    tx.execute("""INSERT INTO association_clarifications
        (clarification_id,user_id,session_id,question,candidates,sources,created_at) VALUES (?,?,?,?,?,?,?)""",
        tuple(replacement[k] for k in ("clarification_id", "user_id", "session_id", "question", "candidates", "sources", "created_at")))
    return replacement
