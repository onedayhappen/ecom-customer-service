"""Execution-local working view; durable truth remains in tasks and receipts."""
from copy import deepcopy

from app.memory.source_policy import safe_context


class WorkingContext:
    """Keep one view per execution, reset when the ReAct transcript is rebuilt."""

    def __init__(self):
        self.shown = {}

    def initial(self, view):
        self.shown = deepcopy(safe_context(view))
        return deepcopy(self.shown)

    def changes(self, view):
        current = safe_context(view)
        changed = {key: value for key, value in current.items()
                   if key not in self.shown or self.shown[key] != value}
        self.shown = deepcopy(current)
        return deepcopy(changed)


def evidence_refs(receipts):
    """Reference results already present in tool messages, without copying them."""
    return [dict(event_id=e['event_id'], tool=e['tool']) for e in receipts
            if e['result'].get('success') and e['tool'] != 'read_task_context']
