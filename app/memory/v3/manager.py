"""Compatibility facade: customer-service callers keep their existing read API."""
from app.config.settings import settings
from app.memory.ltm_manager import LongTermMemoryManager
from app.memory.query_router import MemoryQueryRouter
from app.memory.retrieval import MemoryRetriever
from app.memory.v3.repository import Repository


class Facts:
    def __init__(self, repository):
        self.repository = repository
        self.user_id = repository.user
        self.semantic_retrieval_top_k = settings.semantic_memory_retrieval_top_k
        self.retriever = MemoryRetriever(settings.local_embedding_model_path,
            enabled=settings.memory_retrieval_enabled, min_score=settings.memory_retrieval_min_score)
        self.facts = []
        self.load()

    def load(self):
        self.facts = self.repository.read()

    def history_facts(self):
        return self.repository.read(history=True)


class Manager(LongTermMemoryManager):
    def __init__(self, repository):
        self.repository = repository
        self.client, self.model = None, settings.model_name
        self.memory_enabled = settings.memory_enabled
        self.source_policy = repository.policy
        self.prompt_tokens = settings.memory_context_tokens
        self.query_router = MemoryQueryRouter()
        self._injected_ids, self._visible_auto_ids = set(), set()
        self._context_session_id = ''
        self._context_sources, self._visible_source_quotes = {}, {}
        self._visible_user_quotes = []
        self.derived_views = {'profile': {'refs': []}, 'topics': {}}
        self.ltm = Facts(repository)

    def processing_state(self):
        with self.repository.store.transaction(read_only=True) as tx:
            rows = tx.all('''SELECT status,requested,through_sequence FROM memory_v3_jobs
                WHERE user_id=? AND (requested>through_sequence OR status='completed_with_issues')''', (self.repository.user,))
        return dict(pending=any(r['requested'] > r['through_sequence'] for r in rows),
                    needs_attention=any(r['status'] in {'failed', 'completed_with_issues'} for r in rows))

    def consolidate_to_long_term(self, messages, summary, source_session=''):
        # Unauthenticated caller-supplied transcripts cannot write v3 memory.
        from app.memory.v3.jobs import archive
        return archive(self.repository.store, self.repository.user, source_session)

    def prune_expired_long_term_memory(self):
        # Explicit facts have no arbitrary 90-day expiry. Their validity is
        # changed by new evidence or an explicit time interval.
        self.repository.activate_due()
        return 0

    def reset_all(self):
        from dataclasses import asdict
        from uuid import uuid4
        from app.memory.v3.repository import now
        from app.memory.v3.validation import encoded, family
        with self.repository.store.transaction() as tx:
            self.repository.lock(tx)
            rows = tx.all('SELECT payload FROM memory_v3_records WHERE user_id=?', (self.repository.user,))
            import json
            from app.memory.long_term import MemoryFact
            facts = [MemoryFact(**json.loads(r['payload'])) for r in rows]
            for fact in facts:
                fact.status = 'forgotten'
                fact.metadata['forgotten'] = True
                tombstone = dict(asdict(fact), value=None, observed_at=now())
                tx.execute('INSERT INTO memory_v3_forgets VALUES (?,?,?,?,?,?)',
                    (self.repository.user, str(uuid4()), family(fact.predicate, fact.qualifiers), 'null', now(), encoded(tombstone)))
            self.repository.write(tx, facts)
            tx.execute('UPDATE memory_v3_users SET revision=revision+1 WHERE user_id=?', (self.repository.user,))
        self.ltm.load()

    def invalidate_sources(self):
        import json
        from app.memory.long_term import MemoryFact
        from app.memory.v3.validation import sources_valid
        with self.repository.store.transaction() as tx:
            self.repository.lock(tx)
            rows = tx.all("SELECT payload FROM memory_v3_records WHERE user_id=? AND status!='forgotten'", (self.repository.user,))
            obsolete = []
            for row in rows:
                fact = MemoryFact(**json.loads(row['payload']))
                if not sources_valid(self.source_policy, fact, tx):
                    fact.status = 'deleted'
                    fact.metadata['reset'] = True
                    obsolete.append(fact)
            self.repository.write(tx, obsolete)
            if obsolete:
                tx.execute('UPDATE memory_v3_users SET revision=revision+1 WHERE user_id=?', (self.repository.user,))
        return len(obsolete)


def build_manager(user, store):
    from app.memory.runtime import _build_legacy_memory_manager
    repository = Repository(store, user)
    repository.migrate(lambda: _build_legacy_memory_manager(user, store=store))
    return Manager(repository)
