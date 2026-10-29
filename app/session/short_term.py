"""Bounded support context and durable, evidence checked conversation summaries.

Raw events are never changed. Task membership comes from support_task_messages,
including messages associated with a task after their initial admission.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from app.config.settings import settings
from app.session.context_budget import estimate_message_tokens, estimate_text_tokens
from app.memory.source_policy import memory_safe, message_content
from app.prompts.context import RECALL_NOTE, SUMMARY_NOTE, TASK_STATE_NOTE


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS support_context_snapshots (
        session_id TEXT NOT NULL, scope_key TEXT NOT NULL,
        through_sequence INTEGER NOT NULL, version INTEGER NOT NULL,
        payload TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(session_id,scope_key))""",
    """CREATE TABLE IF NOT EXISTS support_context_jobs (
        session_id TEXT NOT NULL, scope_key TEXT NOT NULL,
        requested_sequence INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        available_at TEXT NOT NULL, last_error TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(session_id,scope_key))""",
    """CREATE INDEX IF NOT EXISTS support_context_jobs_due
        ON support_context_jobs(available_at,updated_at)""",
)

KINDS = {'user_request', 'confirmed_details', 'agent_commitments', 'open_questions'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _scope(task_id):
    return f'task:{task_id}' if task_id else 'session'


def enqueue_context_job(tx, control, task_id=None):
    """Coalesce jobs inside the same transaction that writes a public message."""
    for scope_key in dict.fromkeys(['session', _scope(task_id)]):
        timestamp = _now()
        tx.execute('''INSERT INTO support_context_jobs
            (session_id,scope_key,requested_sequence,attempts,available_at,updated_at)
            VALUES (?,?,?,0,?,?) ON CONFLICT(session_id,scope_key) DO UPDATE SET
            requested_sequence=CASE WHEN excluded.requested_sequence > support_context_jobs.requested_sequence
                THEN excluded.requested_sequence ELSE support_context_jobs.requested_sequence END,
            updated_at=excluded.updated_at''',
            (control['session_id'], scope_key, control['sequence'], timestamp, timestamp))


def raw_context(messages):
    """Only recent original dialogue is eligible for intent/entity rewriting."""
    return [dict(role=m['role'], content=m['content']) for m in messages
            if m.get('role') in {'user', 'assistant'}]


def _source(task_id):
    if task_id:
        return ('''support_events e JOIN support_task_messages m
            ON m.session_id=e.session_id AND m.sequence=e.sequence''', ' AND m.task_id=?')
    return 'support_events e', ''


def _messages(tx, sid, task_id, *, ceiling, after=0, before=None, limit=128, descending=False):
    from app.tasks.provenance import context_event_valid
    source, task_filter = _source(task_id)
    args = [sid, ceiling, after]
    extra = ''
    if before is not None:
        extra += ' AND e.sequence<?'
        args.append(before)
    extra += task_filter
    if task_id:
        args.append(task_id)
    args.append(limit)
    rows = tx.all(f'''SELECT e.sequence,e.payload FROM {source}
        WHERE e.session_id=? AND e.sequence<=? AND e.sequence>?
        AND e.kind='message' AND e.visibility='public'{extra}
        ORDER BY e.sequence {'DESC' if descending else 'ASC'} LIMIT ?''', tuple(args))
    result = []
    for row in rows:
        if not context_event_valid(tx, sid, row['sequence']):
            continue
        value = json.loads(row['payload'])
        if value.get('role') in {'user', 'assistant', 'agent'} and memory_safe(message_content(value)):
            result.append({'sequence': row['sequence'], 'role': 'assistant' if value['role'] == 'agent' else value['role'],
                           'content': message_content(value)})
    if descending:
        result.reverse()
        if len(rows) == limit and result and result[0]['role'] == 'assistant':
            # The fixed-size read may begin halfway through a long old turn.
            first_user = next((i for i, item in enumerate(result) if item['role'] == 'user'), len(result))
            result = result[first_user:]
    return result


def _turns(messages):
    groups = []
    for message in messages:
        if message['role'] == 'user' or not groups:
            groups.append([])
        groups[-1].append(message)
    return groups


def _retained_groups(messages):
    """Keep recent exchanges; consecutive unanswered messages are bounded too."""
    groups = _turns(messages)
    completed = 0
    retained = []
    max_groups = settings.short_term_recent_turns + bool(groups and groups[-1][-1]['role'] == 'user')
    for group in reversed(groups):
        retained.append(group)
        if any(m['role'] == 'assistant' for m in group):
            completed += 1
        if completed >= settings.short_term_recent_turns or len(retained) >= max_groups:
            break
    return list(reversed(retained))


def _recent(messages):
    selected = []
    total = 0
    for group in reversed(_retained_groups(messages)):
        cost = estimate_message_tokens(group)
        # Never slice a turn's raw text. The final request guard handles a
        # single oversize turn explicitly after the current input is appended.
        if selected and total + cost > settings.short_term_recent_tokens:
            break
        selected.append(group)
        total += cost
    return [m for group in reversed(selected) for m in group]


def _named(name, note, items, budget):
    kept = []
    omitted = []
    for item in items:
        message = {'role': 'system', 'name': name, 'content': note + _json({'items': kept + [item]})}
        if estimate_message_tokens([message]) <= budget:
            kept.append(item)
        elif isinstance(item, dict) and type(item.get('sequence')) is int:
            omitted.append(item['sequence'])
    if not kept and omitted:
        return [{'role': 'system', 'name': name, 'content': note + _json({
            'items': [], 'omitted_sequences': omitted[:10],
            'note': '相关完整原话超出当前预算，未截取局部内容；需要缩小查询范围或向用户核实。'})}]
    return ([{'role': 'system', 'name': name, 'content': note + _json({'items': kept})}]
            if kept else [])


def _query_terms(query):
    # Exact entities have priority; Chinese bigrams/trigrams give useful
    # lexical recall without requiring an external tokenizer or vector DB.
    entities = re.findall(r'[A-Za-z0-9][A-Za-z0-9_-]{2,}', query)
    phrases = re.findall(r'[\u3400-\u9fff]{2,}', query)
    stop = {'什么', '怎么', '之前', '那个', '这个', '一下', '是否', '可以', '帮我', '我的', '的是', '了吗'}
    fragments = [part[i:i+n] for n in (3, 2) for part in phrases for i in range(len(part)-n+1)
                 if part[i:i+n] not in stop]
    return list(dict.fromkeys(entities + fragments))[:40]


def _recall(tx, sid, task_id, query, cutoff):
    from app.tasks.provenance import context_event_valid
    terms = _query_terms(query)
    if not terms or cutoff <= 1:
        return []
    source, task_filter = _source(task_id)
    # Query the content field, not the JSON encoding (where metadata may match).
    field = "e.payload::jsonb->>'content'" if tx.postgres else "json_extract(e.payload,'$.content')"
    patterns = ['%' + term.lower().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%' for term in terms]
    matches = ' OR '.join(f"LOWER({field}) LIKE ? ESCAPE '!'" for _ in patterns)
    args = [sid, cutoff]
    if task_id:
        args.append(task_id)
    rows = tx.all(f'''SELECT e.sequence,e.payload FROM {source}
        WHERE e.session_id=? AND e.sequence<? AND e.kind='message'
        AND e.visibility='public'{task_filter} AND ({matches})
        ORDER BY e.sequence DESC LIMIT 96''', (*args, *patterns))
    ranked = []
    for row in rows:
        if not context_event_valid(tx, sid, row['sequence']):
            continue
        message = json.loads(row['payload'])
        content = message_content(message)
        if message.get('role') not in {'user', 'assistant', 'agent'} or not isinstance(content, str):
            continue
        score = sum((8 if re.search('[A-Za-z0-9]', term) else len(term))
                    for term in terms if term.casefold() in content.casefold())
        if not score:
            continue
        if not memory_safe(content):
            continue
        # A local hit can omit a distant condition or negation. Return the
        # complete message or omit the whole optional recall item by budget.
        quote = content
        ranked.append((score, row['sequence'], {'sequence': row['sequence'],
            'role': 'assistant' if message['role'] == 'agent' else message['role'], 'quote': quote}))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in ranked[:3]]


def _task_state(row):
    if row.get('storage_mode') == 'durable':
        from app.tasks.migrations import json_value
        state = {key: row[key] for key in ('task_id', 'goal', 'status', 'version')}
        state.update(details=json_value(row['details'], {}), waiting_for=json_value(row['waiting_for']))
        return {'role': 'system', 'name': 'short_term_state',
                'content': TASK_STATE_NOTE + _json(state)}
    slots = json.loads(row['slots'])
    question = json.loads(row['question']) if row['question'] else None
    # Slots are answers supplied by the user, not proof of a business action.
    allowed_slots = {'order_id', 'product_id', 'sku_id', 'details', 'reason', 'email', 'invoice_title', 'tax_id'}
    state = {key: row[key] for key in ('task_id', 'intent', 'request_text', 'status', 'version')}
    state['slots'] = {k: v for k, v in slots.items() if k in allowed_slots}
    state['question'] = ({k: question[k] for k in ('field', 'prompt') if k in question} if question else None)
    return {'role': 'system', 'name': 'short_term_state', 'content':
            TASK_STATE_NOTE + _json(state)}


class ContextService:
    def __init__(self, store):
        self.store = store

    def build(self, sid, user, task_id=None, query=''):
        from app.support.store import SupportError

        with self.store.transaction(read_only=True) as tx:
            control = self.store._control(tx, sid, user)
            task = None
            if task_id:
                task = tx.one('SELECT * FROM conversation_tasks WHERE user_id=? AND task_id=?', (user, task_id))
                if not task:
                    raise SupportError('任务不存在', 404)
            ceiling = control['sequence']
            recent = _recent(_messages(tx, sid, task_id, ceiling=ceiling, descending=True))
            result = [_task_state(task)] if task else []
            snapshot = tx.one('''SELECT * FROM support_context_snapshots
                WHERE session_id=? AND scope_key=? AND through_sequence<=?''', (sid, _scope(task_id), ceiling))
            if snapshot:
                try:
                    payload = json.loads(snapshot['payload'])
                    result += _named('short_term_summary', SUMMARY_NOTE,
                                     _current_summary(tx, sid, payload['items']), settings.short_term_summary_tokens)
                except (ValueError, KeyError, TypeError):
                    # Derived cache failures cannot make raw conversation unreadable.
                    pass
            cutoff = recent[0]['sequence'] if recent else ceiling + 1
            result += _named('short_term_recall', RECALL_NOTE,
                             _recall(tx, sid, task_id, query, cutoff), settings.short_term_recall_tokens)
            result += raw_context(recent)
            from app.media.context import message_images, task_images
            visual = (task_images(tx, user, task_id) if task_id else
                      [image for message in recent for image in message_images(tx, user, sid, message['sequence'])])
            result += _named('visual_context', '图片观察仅作线索，不是用户原话或业务授权。',
                             visual, settings.short_term_summary_tokens)
            return result


def _current_summary(tx, sid, items, *, expand=True):
    """A saved summary is an index into originals, not independent evidence."""
    from app.tasks.provenance import context_event_valid
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sequence = item.get('sequence')
        if type(sequence) is not int or not context_event_valid(tx, sid, sequence):
            continue
        row = tx.one("""SELECT payload FROM support_events WHERE session_id=? AND sequence=?
            AND kind='message' AND visibility='public'""", (sid, sequence))
        p = json.loads(row['payload']) if row else {}
        content = message_content(p)
        role = 'assistant' if p.get('role') == 'agent' else p.get('role')
        if (memory_safe(content) and role == item.get('role') and item.get('quote')
                and item['quote'] in content):
            result.append({**item, 'quote': content if expand else item['quote']})
    return result


def _validate_summary(value, previous, messages):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value['items'], list):
        raise ValueError('Summary must contain only an items list')
    sources = {(m['sequence'], m['role']): [m['content']] for m in messages}
    for item in previous.get('items', []):
        key = (item['sequence'], item['role'])
        sources.setdefault(key, []).append(item['quote'])
    clean = []
    seen = set()
    for item in value['items']:
        if not isinstance(item, dict) or set(item) != {'sequence', 'role', 'kind', 'quote'}:
            raise ValueError('Invalid summary item fields')
        seq, role, kind, quote = (item[k] for k in ('sequence', 'role', 'kind', 'quote'))
        if type(seq) is not int or role not in {'user', 'assistant'} or kind not in KINDS:
            raise ValueError('Invalid summary source or kind')
        if (kind == 'agent_commitments' and role != 'assistant') or (kind == 'user_request' and role != 'user'):
            raise ValueError('Summary kind contradicts source role')
        if not memory_safe(quote) or not quote.strip() or not any(
                quote in source for source in sources.get((seq, role), [])):
            raise ValueError('Summary quote has no exact public source')
        key = (seq, role, kind, quote)
        if key not in seen:
            clean.append(item)
            seen.add(key)
    if not clean and (previous.get('items') or any(m['content'].strip() for m in messages)):
        raise ValueError('Empty summary would discard nonempty evidence')
    payload = {'items': clean}
    if estimate_message_tokens([{'role': 'system', 'content': SUMMARY_NOTE + _json(payload)}]) > settings.short_term_summary_tokens:
        raise ValueError('Summary exceeds configured budget')
    return payload


def _summarize(previous, messages):
    from openai import OpenAI
    from app.observability.llm_client import LlmClient

    from app.prompts.summarizer import QUOTED_SUMMARY_PROMPT
    prompt = QUOTED_SUMMARY_PROMPT + f'整个输出最多约 {max(64, settings.short_term_summary_tokens-180)} token。'
    with OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                timeout=45, max_retries=0) as raw_client:
        client = LlmClient(raw_client=raw_client, model=settings.model_name, default_scene='short_term_summary')
        result = client.generate(messages=[{'role': 'system', 'content': prompt},
                                          {'role': 'user', 'content': _json({'previous': previous, 'messages': messages})}],
                                 response_format={'type': 'json_object'}, max_tokens=settings.short_term_summary_tokens)
    return json.loads(result.content)


def _read_work(store, job):
    from app.tasks.provenance import revision
    sid, scope_key = job['session_id'], job['scope_key']
    task_id = scope_key[5:] if scope_key.startswith('task:') else None
    with store.transaction(read_only=True) as tx:
        control = store._control(tx, sid)
        correction_revision = revision(tx, control['user_id'])
        if task_id and not tx.one('SELECT task_id FROM conversation_tasks WHERE user_id=? AND task_id=?', (control['user_id'], task_id)):
            return None
        ceiling = min(control['sequence'], job['requested_sequence'])
        snapshot = tx.one('SELECT * FROM support_context_snapshots WHERE session_id=? AND scope_key=?', (sid, scope_key))
        previous = json.loads(snapshot['payload']) if snapshot else {'items': []}
        previous = {'items': _current_summary(tx, sid, previous.get('items', []), expand=False)}
        through = snapshot['through_sequence'] if snapshot else 0
        recent = _messages(tx, sid, task_id, ceiling=ceiling, descending=True)
        retained = _recent(recent)
        if not retained:
            return None
        cutoff = retained[0]['sequence']
        rows = _messages(tx, sid, task_id, ceiling=ceiling, after=through, before=cutoff,
                         limit=settings.short_term_compaction_batch_messages + 1)
        if not rows:
            return None
        # The batch itself is an LLM request: a count bound alone is not enough
        # for large messages. Reserve space for the extraction prompt and old
        # summary; select complete turns where possible. A single large turn
        # may be processed message by message, without skipping any source.
        from app.session.request_budget import request_input_limit
        hard_budget = request_input_limit({'max_tokens': settings.short_term_summary_tokens}) - 800
        soft_budget = min(7000, hard_budget)
        candidates = rows[:settings.short_term_compaction_batch_messages]
        batch = []
        for group in _turns(candidates):
            cost = estimate_text_tokens(_json({'previous': previous, 'messages': batch + group}))
            if cost > soft_budget:
                if batch:
                    break
                for message in group:
                    cost = estimate_text_tokens(_json({'previous': previous, 'messages': batch + [message]}))
                    if cost > hard_budget:
                        break
                    batch.append(message)
                break
            batch.extend(group)
        if not batch:
            raise ValueError('One source message exceeds summary request capacity')
        # If the count limit cut an ordinary final turn, defer that whole turn
        # to the next batch. Never drop it by advancing past the batch.
        if len(rows) > len(batch) and rows[len(batch)]['role'] == 'assistant':
            final_turn = _turns(batch)[-1]
            if len(final_turn) < len(batch):
                batch = batch[:-len(final_turn)]
        return dict(previous=previous, messages=batch, through=batch[-1]['sequence'],
                    version=snapshot['version'] if snapshot else 0, previous_through=through,
                    more=len(rows) > len(batch), user_id=control['user_id'],
                    correction_revision=correction_revision)


def process_context_jobs(store, summarize_fn=None, limit=20):
    """Process bounded durable jobs, with model calls outside DB transactions.

    summarize_fn(previous_payload, messages) is injectable for offline tests.
    Optimistic snapshot versions reject duplicate/stale workers; a new message
    arriving during inference preserves the coalesced job for a later pass.
    """
    counts = dict(processed=0, skipped=0, failed=0, stale=0, deferred=0)
    if not settings.short_term_memory_enabled:
        return counts
    if summarize_fn is None and not settings.openai_api_key:
        counts['deferred'] = 1
        return counts
    summarize_fn = summarize_fn or _summarize
    with store.transaction(read_only=True) as tx:
        jobs = tx.all('''SELECT * FROM support_context_jobs WHERE available_at<=?
            ORDER BY available_at,updated_at LIMIT ?''', (_now(), max(0, limit)))
    for job in jobs:
        try:
            work = _read_work(store, job)
            if work is None:
                with store.transaction() as tx:
                    tx.execute('''DELETE FROM support_context_jobs WHERE session_id=? AND scope_key=?
                        AND requested_sequence=?''', (job['session_id'], job['scope_key'], job['requested_sequence']))
                counts['skipped'] += 1
                continue
            payload = _validate_summary(summarize_fn(work['previous'], work['messages']),
                                        work['previous'], work['messages'])
            with store.transaction() as tx:
                from app.tasks.provenance import revision
                if revision(tx, work['user_id'], lock=True) != work['correction_revision']:
                    counts['stale'] += 1
                    continue
                if work['version']:
                    saved = tx.execute('''UPDATE support_context_snapshots SET through_sequence=?,version=version+1,
                        payload=?,updated_at=? WHERE session_id=? AND scope_key=? AND version=? AND through_sequence=?''',
                        (work['through'], _json(payload), _now(), job['session_id'], job['scope_key'],
                         work['version'], work['previous_through'])).rowcount
                else:
                    saved = tx.execute('''INSERT INTO support_context_snapshots VALUES (?,?,?,1,?,?)
                        ON CONFLICT(session_id,scope_key) DO NOTHING''',
                        (job['session_id'], job['scope_key'], work['through'], _json(payload), _now())).rowcount
                if not saved:
                    counts['stale'] += 1
                    continue
                counts['processed'] += 1
                if not work['more']:
                    tx.execute('''DELETE FROM support_context_jobs WHERE session_id=? AND scope_key=?
                        AND requested_sequence=?''', (job['session_id'], job['scope_key'], job['requested_sequence']))
                else:
                    tx.execute('''UPDATE support_context_jobs SET attempts=0,available_at=?,last_error=NULL
                        WHERE session_id=? AND scope_key=?''', (_now(), job['session_id'], job['scope_key']))
        except Exception as exc:
            # Do not store exception bodies: model/API errors may include input.
            delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
            available = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            with store.transaction() as tx:
                tx.execute('''UPDATE support_context_jobs SET attempts=attempts+1,available_at=?,last_error=?
                    WHERE session_id=? AND scope_key=? AND requested_sequence=?''',
                    (available, type(exc).__name__, job['session_id'], job['scope_key'], job['requested_sequence']))
            counts['failed'] += 1
    return counts
