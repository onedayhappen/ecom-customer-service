"""Historical task views for audit and migration; new work uses TaskRepository."""
import json


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS conversation_tasks (
        task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, intent TEXT NOT NULL,
        title TEXT NOT NULL, request_text TEXT NOT NULL, slots TEXT NOT NULL,
        status TEXT NOT NULL, version INTEGER NOT NULL, question TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_sequence INTEGER NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS conversation_tasks_session ON conversation_tasks(session_id,created_at)""",
    """CREATE TABLE IF NOT EXISTS conversation_task_choices (
        session_id TEXT PRIMARY KEY, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS conversation_task_turns (
        job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, route TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS conversation_tasks_pending ON conversation_tasks(session_id,created_sequence)
       WHERE status NOT IN ('completed','cancelled')""",
    """CREATE INDEX IF NOT EXISTS conversation_tasks_history ON conversation_tasks(session_id,created_sequence DESC)
       WHERE status IN ('completed','cancelled')""",
    """CREATE TABLE IF NOT EXISTS support_ticket_task_snapshots (
        ticket_id TEXT PRIMARY KEY, ceiling INTEGER NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS support_task_messages (
        task_id TEXT NOT NULL, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        PRIMARY KEY(task_id,session_id,sequence))""",
)
TERMINAL = {'completed', 'cancelled'}


class TaskService:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def decode(row):
        if row:
            row['slots'] = json.loads(row['slots'])
            row['question'] = json.loads(row['question']) if row['question'] else None
        return row

    def rows(self, tx, sid, history=False):
        rows = tx.all("SELECT * FROM conversation_tasks WHERE session_id=? AND storage_mode='legacy' AND status NOT IN ('completed','cancelled') ORDER BY created_sequence", (sid,))
        if history:
            rows += tx.all("SELECT * FROM conversation_tasks WHERE session_id=? AND storage_mode='legacy' AND status IN ('completed','cancelled') ORDER BY created_sequence DESC LIMIT 80", (sid,))
        return sorted([self.decode(r) for r in rows], key=lambda r: r['created_sequence'])

    def save(self, tx, c, task, actor):
        from app.support.store import now
        task['version'] += 1
        task['updated_at'] = now()
        if task['question']:
            task['question']['task_version'] = task['version']
        tx.execute('''UPDATE conversation_tasks SET request_text=?,slots=?,status=?,version=?,question=?,updated_at=? WHERE task_id=?''',
                   (task['request_text'], json.dumps(task['slots'], ensure_ascii=False), task['status'], task['version'],
                    json.dumps(task['question'], ensure_ascii=False) if task['question'] else None, task['updated_at'], task['task_id']))
        self.store._event(tx, c, 'task_state', actor, {'task': task})
        if task['status'] in {'paused', 'completed', 'cancelled', 'needs_human'}:
            from app.config.settings import settings
            if settings.short_term_memory_enabled:
                from app.session.short_term import enqueue_context_job
                enqueue_context_job(tx, c, task['task_id'])


    def freeze(self, tx, c, actor):
        tasks = self.rows(tx, c['session_id'])
        for task in tasks:
            if task['status'] not in TERMINAL:
                task['status'] = 'needs_human'
                self.save(tx, c, task, actor)
        tx.execute('DELETE FROM conversation_task_choices WHERE session_id=?', (c['session_id'],))
        if tasks:
            self.store._event(tx, c, 'task_choice', actor, {'question': None})

    def snapshot(self, tx, c, ceiling=None):
        if ceiling is None:
            tasks = self.rows(tx, c['session_id'], history=True)
            choice = tx.one('SELECT payload FROM conversation_task_choices WHERE session_id=?', (c['session_id'],))
            retained = [t for t in tasks if t['status'] not in TERMINAL] + [t for t in tasks if t['status'] in TERMINAL][-80:]
            return {'tasks': sorted(retained, key=lambda t: t['created_sequence']), 'question': json.loads(choice['payload']) if choice else None}
        rows = tx.all("SELECT payload FROM support_events WHERE session_id=? AND sequence<=? AND kind='task_state' ORDER BY sequence", (c['session_id'], ceiling))
        tasks = {}
        for row in rows:
            task = json.loads(row['payload'])['task']
            tasks[task['task_id']] = task
        values = list(tasks.values())
        retained = [t for t in values if t['status'] not in TERMINAL] + [t for t in values if t['status'] in TERMINAL][-80:]
        return {'tasks': sorted(retained, key=lambda t: t['created_sequence']), 'question': None}

    def archive_ticket(self, tx, c, tid, ceiling):
        payload = self.snapshot(tx, c)
        tx.execute('INSERT INTO support_ticket_task_snapshots VALUES (?,?,?) ON CONFLICT(ticket_id) DO NOTHING',
                   (tid, ceiling, json.dumps(payload, ensure_ascii=False)))

    def ticket_snapshot(self, tx, c, tid, ceiling):
        row = tx.one('SELECT payload FROM support_ticket_task_snapshots WHERE ticket_id=? AND ceiling=?', (tid, ceiling))
        return json.loads(row['payload']) if row else self.snapshot(tx, c, ceiling)

    def list(self, sid, user):
        with self.store.transaction(read_only=True) as tx:
            c = self.store._control(tx, sid, user)
            return {**self.snapshot(tx, c), 'mode': c['mode'], 'control_version': c['control_version']}

    def history(self, sid, user, before=None, limit=30):
        """Stable keyset pagination; new completions do not shift older pages."""
        from app.support.store import SupportError
        if not 1 <= limit <= 100 or before is not None and before < 1:
            raise SupportError('历史分页参数不合法', 422)
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, user)
            rows = tx.all("SELECT * FROM conversation_tasks WHERE session_id=? AND storage_mode='legacy' AND status IN ('completed','cancelled') " +
                          ('AND created_sequence<? ' if before is not None else '') +
                          'ORDER BY created_sequence DESC LIMIT ?',
                          (sid, before, limit + 1) if before is not None else (sid, limit + 1))
            page = rows[:limit]
            return {'tasks': [self.decode(row) for row in page],
                    'next_before': page[-1]['created_sequence'] if len(rows) > limit else None}

    def get(self, sid, user, tid):
        from app.support.store import SupportError
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, user)
            row = tx.one("SELECT * FROM conversation_tasks WHERE session_id=? AND task_id=? AND storage_mode='legacy'", (sid, tid))
            if not row:
                raise SupportError('任务不存在', 404)
            return self.decode(row)
