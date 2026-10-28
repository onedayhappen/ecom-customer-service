"""Durable user-event extraction and archive barriers; never silently skip failures."""
from time import time
from uuid import uuid4
import json

from app.config.settings import settings
from app.memory.v3.repository import now


def enqueue(tx, control, payload):
    if not settings.memory_enabled or payload.get('role') != 'user':
        return
    # Priority affects scheduling only; it never authorizes a mutation.
    urgent = any(w in payload.get('content', '') for w in ('忘掉', '忘记', '改成', '以后', '重新记'))
    due = time() + (0 if urgent else settings.memory_extraction_debounce_seconds)
    tx.execute('''INSERT INTO memory_v3_jobs(session_id,user_id,requested,available_at) VALUES (?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET requested=excluded.requested,
        status=CASE WHEN memory_v3_jobs.status='running' THEN 'running' ELSE 'pending' END,
        attempts=CASE WHEN memory_v3_jobs.status='failed' THEN 0 ELSE memory_v3_jobs.attempts END,
        available_at=excluded.available_at''', (control['session_id'], control['user_id'], control['sequence'], due))


def process(store, extract_fn=None, limit=20):
    from app.memory.v3.context import load, wire
    from app.memory.v3.extractor import extract
    from app.memory.v3.service import MemoryService
    from app.memory.v3.manager import build_manager
    counts = dict(processed=0, failed=0, stale=0, deferred=0, facts=0, partial=0)
    if not settings.memory_enabled:
        return counts
    if extract_fn is None and not settings.openai_api_key:
        counts['deferred'] = 1
        return counts
    # Pending v2 messages are replayed with original event times after cutover.
    # Existing v3 checkpoints always win; never reset one during startup.
    if settings.memory_engine == 'v3':
        with store.transaction() as tx:
            tx.execute('''INSERT INTO memory_v3_jobs(session_id,user_id,requested,available_at)
                SELECT session_id,user_id,requested_sequence,0 FROM support_memory_jobs
                WHERE requested_sequence>through_sequence ON CONFLICT(session_id) DO NOTHING''')
    with store.transaction(read_only=True) as tx:
        jobs = tx.all('''SELECT * FROM memory_v3_jobs WHERE requested>through_sequence
            AND status IN ('pending','retrying','running') AND available_at<=? AND lease_until<=?
            ORDER BY available_at LIMIT ?''', (time(), time(), min(max(limit, 0), 100)))
    for candidate in jobs:
        token = str(uuid4())
        with store.transaction() as tx:
            claimed = tx.execute('''UPDATE memory_v3_jobs SET lease_token=?,lease_until=?,status='running'
                WHERE session_id=? AND lease_until<=? AND requested>through_sequence AND status!='failed' ''',
                (token, time()+120, candidate['session_id'], time())).rowcount
            job = tx.one('SELECT * FROM memory_v3_jobs WHERE session_id=?', (candidate['session_id'],))
        if not claimed:
            counts['stale'] += 1
            continue
        try:
            manager = build_manager(job['user_id'], store)
            revision = manager.repository.revision()
            existing = [manager.fact_record(f) for f in manager.ltm.facts]
            messages, existing, through = load(store, job, existing)
            plan = (extract_fn or extract)(wire(messages), existing) if any(m['is_new'] for m in messages) else dict(schema_version=3, changes=[])
            result = MemoryService(store, job['user_id']).apply(plan, messages,
                batch_id=f"{job['session_id']}:{job['through_sequence']}:{through}", sid=job['session_id'],
                through=through, expected_revision=revision, job=job)
            counts['processed'] += 1
            counts['facts'] += result['applied']
            counts['partial'] += result['status'] == 'completed_with_issues'
        except Exception as error:
            attempts = job['attempts'] + 1
            with store.transaction() as tx:
                tx.execute('''UPDATE memory_v3_jobs SET status=?,attempts=?,available_at=?,last_error=?,
                    lease_token=NULL,lease_until=0 WHERE session_id=? AND lease_token=?''',
                    ('failed' if attempts >= 3 else 'retrying', attempts, time()+min(300, 5*2**attempts),
                     type(error).__name__, job['session_id'], token))
            counts['failed'] += 1
    return counts


def archive(store, user, sid):
    from app.support.store import SupportError
    if not settings.memory_enabled:
        raise SupportError('长期记忆未启用', 409)
    with store.transaction() as tx:
        control = store._control(tx, sid, user)
        cutoff = control['sequence']
        old = tx.one('SELECT archive_id FROM memory_v3_archives WHERE user_id=? AND session_id=? AND through_sequence=?',
                     (user, sid, cutoff))
        archive_id = old['archive_id'] if old else str(uuid4())
        tx.execute('INSERT INTO memory_v3_archives VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING',
                   (archive_id, user, sid, cutoff, now()))
        tx.execute('''INSERT INTO memory_v3_jobs(session_id,user_id,requested,available_at) VALUES (?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET requested=excluded.requested,available_at=excluded.available_at,
            status=CASE WHEN memory_v3_jobs.status='running' THEN 'running' ELSE 'pending' END,attempts=0''',
            (sid, user, cutoff, time()))
    return archive_status(store, user, archive_id)


def archive_status(store, user, archive_id):
    from app.support.store import SupportError
    with store.transaction(read_only=True) as tx:
        record = tx.one('SELECT * FROM memory_v3_archives WHERE archive_id=? AND user_id=?', (archive_id, user))
        if not record:
            raise SupportError('归档记录不存在', 404)
        job = tx.one('SELECT * FROM memory_v3_jobs WHERE session_id=?', (record['session_id'],))
        # Include a receipt crossing the barrier (new input may arrive while
        # the archive waits); otherwise a partial result could appear successful.
        receipts = tx.all('''SELECT result FROM memory_v3_receipts WHERE user_id=? AND session_id=?
            ORDER BY through_sequence''', (user, record['session_id']))
    results = []
    for row in receipts:
        result = json.loads(row['result'])
        results.append(result)
        if result['through_sequence'] >= record['through_sequence']:
            break
    complete = job and job['through_sequence'] >= record['through_sequence']
    status = ('completed_with_issues' if any(r['status'] == 'completed_with_issues' for r in results) else 'completed') if complete else job['status'] if job else 'pending'
    return dict(archive_id=archive_id, session_id=record['session_id'], through_sequence=record['through_sequence'],
                processed_sequence=job['through_sequence'] if job else 0, status=status,
                outcomes=[o for r in results for o in r['outcomes']], last_error=job['last_error'] if job else None)
