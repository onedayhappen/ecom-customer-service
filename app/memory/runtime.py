"""Durable incremental extraction from authenticated public user events.

Conversation jobs and their checkpoints live in the support database. Memory
stores commit an idempotent source_batch_id before this checkpoint advances, so
an interruption between the two databases is safe to replay.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from uuid import uuid4

from app.config.settings import settings
from app.memory.extraction import MemoryExtractionError, build_extraction_messages, extract_long_term_facts
from app.session.context_budget import estimate_request_tokens
from app.session.request_budget import request_input_limit


class MemoryLeaseLost(RuntimeError):
    pass


SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS support_memory_jobs (
        session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        requested_sequence INTEGER NOT NULL, through_sequence INTEGER NOT NULL DEFAULT 0,
        batch_through_sequence INTEGER,
        version INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
        last_error TEXT, updated_at TEXT NOT NULL)''',
    '''CREATE INDEX IF NOT EXISTS support_memory_jobs_due
        ON support_memory_jobs(available_at,updated_at)''',
)


def _now():
    return datetime.now(timezone.utc)


def enqueue_memory_job(tx, control, payload):
    """Called by the public-event hook in the event's own transaction."""
    if not settings.memory_enabled or payload.get('role') != 'user':
        return
    timestamp = _now()
    available = timestamp + timedelta(seconds=settings.memory_extraction_debounce_seconds)
    tx.execute('''INSERT INTO support_memory_jobs
        (session_id,user_id,requested_sequence,available_at,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
        requested_sequence=CASE WHEN excluded.requested_sequence > support_memory_jobs.requested_sequence
            THEN excluded.requested_sequence ELSE support_memory_jobs.requested_sequence END,
        available_at=CASE WHEN support_memory_jobs.attempts=0 THEN excluded.available_at
            ELSE support_memory_jobs.available_at END, updated_at=excluded.updated_at''',
        (control['session_id'], control['user_id'], control['sequence'], available.isoformat(), timestamp.isoformat()))


def build_memory_manager(user_id):
    """Create a fresh manager for the authenticated user, without a model call."""
    from app.memory.ltm_manager import LongTermMemoryManager

    return LongTermMemoryManager(
        client=None, model=settings.model_name, user_id=user_id,
        memory_dir=settings.memory_dir, memory_enabled=settings.memory_enabled,
        max_ltm_facts=settings.max_ltm_facts, preference_ttl_days=settings.preference_memory_ttl_days,
        retrieval_enabled=settings.memory_retrieval_enabled,
        retrieval_top_k=settings.memory_retrieval_top_k,
        retrieval_min_score=settings.memory_retrieval_min_score,
        retrieval_model_path=settings.local_embedding_model_path,
        storage_backend=settings.memory_storage_backend, postgres_dsn=settings.memory_postgres_dsn,
        redis_url=settings.memory_redis_url, cache_ttl_seconds=settings.memory_cache_ttl_seconds,
        cache_key_prefix=settings.memory_cache_key_prefix,
        semantic_retrieval_top_k=settings.semantic_memory_retrieval_top_k,
        prompt_tokens=settings.memory_context_tokens,
    )


def _claim(store, candidate):
    token, now = str(uuid4()), _now()
    with store.transaction() as tx:
        saved = tx.execute('''UPDATE support_memory_jobs SET lease_token=?,lease_until=?,version=version+1
            WHERE session_id=? AND version=? AND requested_sequence>through_sequence
            AND available_at<=? AND (lease_until IS NULL OR lease_until<=?)''',
            (token, (now + timedelta(seconds=180)).isoformat(), candidate['session_id'], candidate['version'],
             now.isoformat(), now.isoformat())).rowcount
        if not saved:
            return None
        return tx.one('SELECT * FROM support_memory_jobs WHERE session_id=? AND lease_token=?',
                      (candidate['session_id'], token))


def _read_messages(store, job):
    with store.transaction(read_only=True) as tx:
        control = store._control(tx, job['session_id'], job['user_id'])
        ceiling = min(control['sequence'], job['requested_sequence'],
                      job.get('batch_through_sequence') or job['requested_sequence'])
        field = "payload::jsonb->>'role'" if tx.postgres else "json_extract(payload,'$.role')"
        rows = tx.all(f'''SELECT sequence,event_key,payload,created_at FROM support_events
            WHERE session_id=? AND sequence>? AND sequence<=? AND kind='message'
            AND visibility='public' AND actor=? AND {field}='user'
            ORDER BY sequence LIMIT ?''', (job['session_id'], job['through_sequence'], ceiling,
                                          job['user_id'], 128 if job.get('batch_through_sequence')
                                          else settings.memory_extraction_batch_messages))
    messages = []
    for row in rows:
        content = json.loads(row['payload']).get('content')
        if not isinstance(content, str):
            raise MemoryExtractionError('Source event has invalid content')
        messages.append(dict(role='user', content=content, sequence=row['sequence'],
                             event_key=row['event_key'], created_at=row['created_at']))
    return messages, ceiling


def _bounded_batch(messages, existing, *, allow_segments=False):
    # Existing facts improve duplicate handling, but cannot crowd new evidence
    # out of the extraction request. Consolidation reloads the full active set.
    from app.memory.atomic import query_slots
    predicates, hints = query_slots('\n'.join(message['content'][:4000] for message in messages))
    related = [fact for fact in existing
               if getattr(fact, 'predicate', '') in predicates or hints and all(
                   getattr(fact, 'qualifiers', {}).get(key) == value for key, value in hints.items())]
    bounded_existing = []
    for fact in related:
        proposed = bounded_existing + [fact]
        if _input_cost([], proposed) > 1800:
            break
        bounded_existing = proposed
    limit = min(settings.memory_extraction_input_tokens,
                request_input_limit(settings.chat_completion_options))
    batch = []
    for message in messages:
        proposed = batch + [message]
        cost = _input_cost(proposed, bounded_existing)
        if cost > limit:
            if not batch:
                # A single message can use the whole configured request budget.
                # Drop duplicate-avoidance hints first; never slice source text.
                bounded_existing = []
                cost = _input_cost(proposed, [])
                if cost <= request_input_limit(settings.chat_completion_options):
                    batch = proposed
                elif allow_segments:
                    batch = proposed
                else:
                    raise MemoryExtractionError('Source event exceeds extraction request capacity')
            break
        batch = proposed
    return batch, bounded_existing


def _input_cost(messages, existing):
    return estimate_request_tokens(dict(settings.chat_completion_options,
        messages=build_extraction_messages(messages, None, existing, user_sources_only=True, schema_version=2)))


def _extraction_segments(messages, existing_facts):
    """Cover every source character; overlapping windows keep local grammar."""
    limit = min(settings.memory_extraction_input_tokens, request_input_limit(settings.chat_completion_options))
    indexed = [dict(message, _batch_index=index, _offset=0, _full_content=message['content'])
               for index, message in enumerate(messages)]
    if _input_cost(indexed, existing_facts) <= limit:
        return [(indexed, existing_facts)]
    result = []
    for message in indexed:
        content, start = message['content'], 0
        if _input_cost([message], []) <= limit:
            result.append(([message], []))
            continue
        while start < len(content):
            low, high = start + 1, len(content)
            end = start
            while low <= high:
                midpoint = (low + high) // 2
                part = dict(message, content=content[start:midpoint], _offset=start)
                if _input_cost([part], []) <= limit:
                    end, low = midpoint, midpoint + 1
                else:
                    high = midpoint - 1
            if end == start:
                raise MemoryExtractionError('Atomic extraction instructions exceed request capacity')
            result.append(([dict(message, content=content[start:end], _offset=start)], []))
            if len(result) > 64:
                raise MemoryExtractionError('Source exceeds bounded extraction segment count')
            if end == len(content):
                break
            start = end - min(160, max(0, (end-start)//4))
    return result


def _extract(messages, existing_facts, *, on_segment=None):
    from openai import OpenAI
    from app.observability.llm_client import LlmClient

    segments = _extraction_segments(messages, existing_facts)
    facts = []
    with OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                timeout=45, max_retries=0) as raw:
        client = LlmClient(raw, settings.model_name, default_scene='long_term_extraction')
        seen = set()
        for part, existing in segments:
            if on_segment:
                on_segment()
            extracted = extract_long_term_facts(client, settings.model_name, part, None, existing,
                                                strict=True, user_sources_only=True, schema_version=2)
            for fact in extracted:
                source = part[fact.metadata['source_index']]
                fact.metadata['source_index'] = source['_batch_index']
                fact.metadata['quote_start'] = source['_offset'] + source['content'].find(fact.source_span)
                fact.metadata['quote_end'] = fact.metadata['quote_start'] + len(fact.source_span)
                key = (source['_batch_index'], fact.memory_key, json.dumps(fact.value, sort_keys=True),
                       fact.polarity, fact.operation, fact.source_span)
                if key not in seen:
                    facts.append(fact)
                    seen.add(key)
    return facts


def _ground_facts(facts, messages, sid):
    """Only server-matched user evidence may supply identity and timestamps."""
    from app.memory.long_term import MemoryFact
    from app.memory.validator import MemoryValidator

    if not isinstance(facts, list) or any(not isinstance(fact, MemoryFact) for fact in facts):
        raise MemoryExtractionError('Extractor must return a list of MemoryFact')
    result = []
    for candidate in facts:
        if candidate.category not in {'identity', 'preference', 'behavior', 'other'}:
            continue
        if candidate.scope not in {'user_preference', 'user_profile'} or candidate.source_type != 'user_explicit':
            continue
        if not math.isfinite(candidate.confidence) or not MemoryValidator().validate(candidate).accepted:
            continue
        atomic = getattr(candidate, 'schema_version', 1) == 2
        if atomic:
            index = candidate.metadata.get('source_index')
            if type(index) is not int or not 0 <= index < len(messages):
                raise MemoryExtractionError('Atomic source index is invalid')
            source = messages[index]
            if not candidate.source_span.strip() or candidate.source_span not in source['content']:
                raise MemoryExtractionError('Atomic quote has no exact source')
        else:
            source = next((message for message in reversed(messages)
                           if candidate.source_span.strip() and candidate.source_span in message['content']), None)
        if source is None:
            raise MemoryExtractionError('Candidate has no exact public user source')
        fact = deepcopy(candidate)
        timestamp = datetime.fromisoformat(source['created_at'])
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        fact.created_at = fact.updated_at = timestamp.astimezone(timezone.utc).isoformat()
        fact.source_session = sid
        fact.source_event_id = f"{sid}:{source['sequence']}"
        fact.scope = 'user_profile' if fact.category == 'identity' else 'user_preference'
        fact.memory_id = ''
        fact.status = 'active'
        fact.expires_at = ''
        fact.allowed_agents = []
        fact.supersedes_memory_id = fact.superseded_by_memory_id = ''
        if atomic:
            from app.memory.atomic import canonical_memory_key, ground_time, normalize_candidate

            candidate_item = dict(predicate=fact.predicate, value=fact.value, qualifiers=fact.qualifiers,
                polarity=fact.polarity, operation=fact.operation, source_index=index,
                source_quote=fact.source_span, confidence=fact.confidence,
                valid_time_quote=fact.metadata.get('valid_time_quote', ''))
            normalized = normalize_candidate(candidate_item, messages)
            if normalized is None:
                continue
            fact.predicate, fact.value, fact.qualifiers = normalized['predicate'], normalized['value'], normalized['qualifiers']
            fact.content, fact.memory_key = normalized['content'], canonical_memory_key(fact.predicate, fact.qualifiers)
            fact.observed_at = fact.created_at
            fact.recorded_at = _now().isoformat()
            temporal = ground_time(fact.metadata.get('valid_time_quote') or fact.source_span, source['created_at'])
            fact.valid_from, fact.valid_to = temporal.get('valid_from', ''), temporal.get('valid_to', '')
            start = fact.metadata.get('quote_start', source['content'].find(fact.source_span))
            end = start + len(fact.source_span)
            if type(start) is not int or start < 0 or source['content'][start:end] != fact.source_span:
                raise MemoryExtractionError('Atomic quote offsets do not match source')
            fact.metadata = temporal.get('metadata', {})
            fact.evidence = [dict(source_event_id=fact.source_event_id, source_session=sid,
                                  source_span=fact.source_span, observed_at=fact.observed_at,
                                  start_offset=start, end_offset=end)]
        result.append(fact)
    return result


def process_memory_jobs(store, extract_fn=None, limit=20):
    """Run bounded jobs; extract_fn(messages, existing_facts) supports offline QA."""
    counts = dict(processed=0, failed=0, stale=0, deferred=0, facts=0)
    if not settings.memory_enabled:
        return counts
    if extract_fn is None and not settings.openai_api_key:
        counts['deferred'] = 1
        return counts
    use_atomic_model = extract_fn is None
    extract_fn = extract_fn or _extract
    timestamp = _now().isoformat()
    with store.transaction(read_only=True) as tx:
        candidates = tx.all('''SELECT * FROM support_memory_jobs
            WHERE requested_sequence>through_sequence AND available_at<=?
            AND (lease_until IS NULL OR lease_until<=?) ORDER BY available_at,updated_at LIMIT ?''',
            (timestamp, timestamp, max(0, min(limit, 100))))
    for candidate in candidates:
        job = _claim(store, candidate)
        if job is None:
            counts['stale'] += 1
            continue
        try:
            messages, ceiling = _read_messages(store, job)
            manager = build_memory_manager(job['user_id'])
            if job.get('batch_through_sequence'):
                # A previously started batch keeps the same identity through a
                # retry, even if existing memory or batch settings have changed.
                batch, existing = messages, []
                if not use_atomic_model and _input_cost(batch, existing) > request_input_limit(settings.chat_completion_options):
                    raise MemoryExtractionError('Pending extraction batch exceeds request capacity')
            else:
                batch, existing = _bounded_batch(messages, manager.ltm.facts, allow_segments=use_atomic_model)
            through = batch[-1]['sequence'] if batch else ceiling
            with store.transaction() as tx:
                pinned = tx.execute('''UPDATE support_memory_jobs SET batch_through_sequence=?
                    WHERE session_id=? AND lease_token=? AND version=? AND through_sequence=?''',
                    (through, job['session_id'], job['lease_token'], job['version'], job['through_sequence'])).rowcount
            if not pinned:
                counts['stale'] += 1
                continue
            def renew_lease():
                with store.transaction() as tx:
                    kept = tx.execute('''UPDATE support_memory_jobs SET lease_until=?
                        WHERE session_id=? AND lease_token=? AND version=? AND through_sequence=?''',
                        ((_now()+timedelta(seconds=180)).isoformat(), job['session_id'], job['lease_token'],
                         job['version'], job['through_sequence'])).rowcount
                if not kept:
                    raise MemoryLeaseLost('Extraction worker no longer owns its lease')

            extracted = (extract_fn(batch, existing, on_segment=renew_lease) if use_atomic_model else extract_fn(batch, existing)) if batch else []
            facts = _ground_facts(extracted, batch, job['session_id'])
            # Renew the ownership fence before crossing into the memory store.
            with store.transaction() as tx:
                owned = tx.execute('''UPDATE support_memory_jobs SET lease_until=?
                    WHERE session_id=? AND lease_token=? AND version=? AND through_sequence=?''',
                    ((_now() + timedelta(seconds=180)).isoformat(), job['session_id'], job['lease_token'],
                     job['version'], job['through_sequence'])).rowcount
            if not owned:
                counts['stale'] += 1
                continue
            manager.ltm.merge_and_save(facts, source_batch_id=f"{job['session_id']}:{job['through_sequence']}:{through}")
            with store.transaction() as tx:
                saved = tx.execute('''UPDATE support_memory_jobs SET through_sequence=?,attempts=0,last_error=NULL,
                    batch_through_sequence=NULL,lease_token=NULL,lease_until=NULL,available_at=? WHERE session_id=? AND lease_token=?
                    AND version=? AND through_sequence=?''',
                    (through, _now().isoformat(), job['session_id'], job['lease_token'], job['version'],
                     job['through_sequence'])).rowcount
            counts['processed' if saved else 'stale'] += 1
            counts['facts'] += len(facts) if saved else 0
        except MemoryLeaseLost:
            counts['stale'] += 1
        except Exception as error:
            delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
            with store.transaction() as tx:
                tx.execute('''UPDATE support_memory_jobs SET attempts=attempts+1,last_error=?,available_at=?,
                    lease_token=NULL,lease_until=NULL WHERE session_id=? AND lease_token=? AND version=?''',
                    (type(error).__name__, (_now() + timedelta(seconds=delay)).isoformat(),
                     job['session_id'], job['lease_token'], job['version']))
            counts['failed'] += 1
    return counts
