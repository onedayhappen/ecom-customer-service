"""Bounded incremental evidence with authenticated previous user messages."""
import json

from app.config.settings import settings
from app.memory.source_policy import SourcePolicy
from app.memory.v3.extractor import request
from app.session.context_budget import estimate_request_tokens
from app.session.request_budget import request_input_limit


def wire(messages):
    return [{k: m[k] for k in ('message_id', 'role', 'content', 'is_new')} for m in messages]


def load(store, job, existing):
    policy = SourcePolicy(store, job['user_id'])
    with store.transaction(read_only=True) as tx:
        rows = tx.all('''SELECT sequence,payload,created_at FROM support_events WHERE session_id=?
            AND sequence>? AND sequence<=? AND kind='message' AND actor=? AND visibility='public'
            ORDER BY sequence LIMIT ?''', (job['session_id'], job['through_sequence'], job['requested'],
                                         job['user_id'], settings.memory_extraction_batch_messages))
        previous = tx.all('''SELECT sequence,payload,created_at FROM support_events WHERE session_id=?
            AND sequence<=? AND kind='message' AND actor=? AND visibility='public'
            ORDER BY sequence DESC LIMIT 12''', (job['session_id'], job['through_sequence'], job['user_id']))
        messages = []
        # Bring back a few authenticated source messages for cross-session
        # references. A stored summary is never substituted for original text.
        known = {f"{job['session_id']}:{r['sequence']}" for r in [*previous, *rows]}
        for fact in existing[:8]:
            event = fact['source_event_id']
            sid, _, sequence = event.rpartition(':')
            if event in known or not sequence.isdecimal():
                continue
            source = policy.source(tx, sid, int(sequence))
            row = tx.one('SELECT created_at FROM support_events WHERE session_id=? AND sequence=?', (sid, int(sequence)))
            if source and row:
                messages.append(dict(message_id=event, role='user', content=source['content'], is_new=False,
                    sequence=int(sequence), session_id=sid, created_at=row['created_at'], proof=source['proof']))
                known.add(event)
        for row in [*reversed(previous), *rows]:
            source = policy.source(tx, job['session_id'], row['sequence'])
            if source is None:
                continue
            messages.append(dict(message_id=f"{job['session_id']}:{row['sequence']}", role='user',
                content=source['content'], is_new=row['sequence'] > job['through_sequence'], sequence=row['sequence'],
                session_id=job['session_id'], created_at=row['created_at'], proof=source['proof']))
    through = rows[-1]['sequence'] if rows else job['requested']
    limit = min(settings.memory_extraction_input_tokens, request_input_limit(settings.chat_completion_options))
    existing = existing[:32]
    def cost():
        return estimate_request_tokens(dict(settings.chat_completion_options, messages=request(wire(messages), existing)))
    # Only whole messages are removed. Dropped new events remain pending.
    while cost() > limit and existing:
        existing.pop()
    while cost() > limit and messages and not messages[0]['is_new']:
        messages.pop(0)
    while cost() > limit and sum(m['is_new'] for m in messages) > 1:
        messages.pop()
        through = max(m['sequence'] for m in messages if m['is_new'])
    if cost() > request_input_limit(settings.chat_completion_options):
        raise ValueError('memory_source_exceeds_capacity')
    return messages, existing, through
