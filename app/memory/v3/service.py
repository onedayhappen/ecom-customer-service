"""Validate independent groups then commit a bounded plan atomically."""
from app.memory.v3.models import Plan, EvidenceRejected
from app.memory.v3.repository import Repository
from app.memory.v3.validation import validate, encoded, order


class MemoryService:
    def __init__(self, store, user):
        self.repository = Repository(store, user)

    def apply(self, plan, messages, **commit):
        plan = plan if isinstance(plan, Plan) else Plan.model_validate(plan)
        validated, outcomes, blocked = [], [], set()
        for change in plan.changes:
            if change.action in {'NOOP', 'NEEDS_CLARIFICATION'}:
                status = 'noop' if change.action == 'NOOP' else 'needs_clarification'
                outcomes.append(dict(group_id=change.group_id, status=status, reason='model_decision'))
                if status != 'noop':
                    blocked.add(change.group_id)
                continue
            try:
                validated.append(validate(change, messages))
            except (EvidenceRejected, ValueError) as error:
                blocked.add(change.group_id)
                outcomes.append(dict(group_id=change.group_id, status='needs_clarification', reason=str(error)))
        seen = {}
        for item in validated:
            if item.change.action == 'FORGET':
                continue
            key = (item.family, order(item.fact), item.fact.valid_from)
            previous = seen.get(key)
            if previous and encoded([previous.fact.value, previous.fact.polarity]) != encoded([item.fact.value, item.fact.polarity]):
                for group in {previous.change.group_id, item.change.group_id}:
                    blocked.add(group)
                    outcomes.append(dict(group_id=group, status='needs_clarification', reason='conflicting_same_source'))
            seen[key] = item
        accepted = [v for v in validated if v.change.group_id not in blocked]
        # Optional local embeddings are prepared before opening a transaction.
        # Their absence never prevents lexical retrieval or a durable commit.
        from app.config.settings import settings
        from app.memory.retrieval import MemoryRetriever
        retriever = MemoryRetriever(settings.local_embedding_model_path,
            enabled=settings.memory_retrieval_enabled, min_score=settings.memory_retrieval_min_score)
        retriever.prepare_embeddings([v.fact for v in accepted if v.change.action != 'FORGET'])
        return self.repository.commit(accepted, outcomes, **commit)
