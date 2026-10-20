"""Dependency envelopes for new-path memory, summaries and retrieval caches."""
from copy import deepcopy

from app.tasks.provenance import ProvenanceGuard


class DerivedData:
    """A cache hit has to pass the same check as a newly built result."""
    def __init__(self, store, authenticated_user_id):
        self.guard = ProvenanceGuard(store, authenticated_user_id)

    def begin(self, task_ids=(), decision_ids=()):
        return self.guard.capture(task_ids, decision_ids)

    def finish(self, stamp, payload):
        # Do not capture a fresh stamp for output computed from stale inputs.
        self.guard.require_current(stamp)
        return {"provenance": deepcopy(stamp), "payload": deepcopy(payload)}

    def read(self, envelope):
        if not isinstance(envelope, dict) or not self.guard.valid(envelope.get("provenance")):
            return None
        return deepcopy(envelope.get("payload"))
