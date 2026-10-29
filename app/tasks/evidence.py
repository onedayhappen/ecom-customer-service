"""Read server-recorded task evidence from the conversation event log."""
import json


def find_evidence(tx, user, event_id, *, session_id=None, task_id=None, kind=None):
    conditions = ["c.user_id=?", "e.event_key=?",
                  "e.kind IN ('task_tool_result','task_operation_result','task_external_result')",
                  "e.visibility='internal'", "e.actor='system'"]
    args = [user, 'task-tool:' + event_id]
    if kind is not None:
        conditions.append('e.kind=?')
        args.append(kind)
    if session_id is not None:
        conditions.append('e.session_id=?')
        args.append(session_id)
    rows = tx.all('SELECT e.session_id,e.sequence,e.payload FROM support_events e '
                  'JOIN support_conversations c ON c.session_id=e.session_id WHERE '
                  + ' AND '.join(conditions), args)
    matches = []
    for row in rows:
        payload = json.loads(row['payload'])
        if payload.get('event_id') != event_id:
            continue
        if task_id is not None and payload.get('task_id') != task_id:
            continue
        matches.append({**row, 'payload': payload})
    # An ambiguous identifier is never sufficient authority.
    return matches[0] if len(matches) == 1 else None
