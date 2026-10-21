"""Task transitions use the same conversation transaction as message admission."""
import json
from uuid import uuid4

from app.tasks.routing import LABELS

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
ACTIVE = {'ready', 'running', 'waiting_input', 'waiting_confirmation'}
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

    def create(self, tx, c, spec, actor):
        from app.support.store import now
        timestamp = now()
        task = dict(task_id=str(uuid4()), session_id=c['session_id'], intent=spec['intent'],
                    title=LABELS[spec['intent']], request_text=spec['text'], slots={},
                    status='queued', version=0, question=None, created_at=timestamp, updated_at=timestamp, created_sequence=c['sequence'] + 1)
        tx.execute('''INSERT INTO conversation_tasks
            (task_id,session_id,intent,title,request_text,slots,status,version,question,
             created_at,updated_at,created_sequence,user_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (task['task_id'], c['session_id'], task['intent'], task['title'], task['request_text'], '{}', 'queued', 0, None, timestamp, timestamp, task['created_sequence'], c['user_id']))
        tx.execute('INSERT INTO task_sessions VALUES (?,?,?)', (task['task_id'], c['session_id'], timestamp))
        self.save(tx, c, task, actor)
        return task

    def invalidate(self, tx, c):
        c['control_version'] += 1
        c['bot_job_id'] = None
        self.store.jobs.supersede(tx, c)
        tx.execute('UPDATE support_conversations SET control_version=?,bot_job_id=NULL WHERE session_id=?', (c['control_version'], c['session_id']))
        tx.execute('DELETE FROM conversation_task_choices WHERE session_id=?', (c['session_id'],))
        self.store._event(tx, c, 'task_choice', 'system', {'question': None})


    def route(self, tx, c, text, actor, data):
        if data.get('task_action'):
            from app.support.store import SupportError
            raise SupportError('旧待办控制入口已退役', 410)
        return self.conversation_route(tx, c, text, actor, data)

    def conversation_route(self, tx, c, text, actor, data):
        """Admit the original message, without splitting tasks or filling slots.

        Only explicit UI actions use the legacy task API. Every new conversational
        turn fences old jobs/proposals; a confirmation keeps the exact issuing epoch.
        """
        from app.support.store import SupportError
        if c['mode'] != 'bot':
            return {}
        if c['bot_job_id']:
            previous = tx.one("SELECT payload FROM support_events WHERE session_id=? AND event_key=?",
                              (c['session_id'], f"user:{c['bot_job_id']}"))
            if previous and json.loads(previous['payload']).get('content') == text:
                raise SupportError('相同消息正在处理中，请等待结果或使用原请求 ID 查询')
        confirmation = (data.get('confirmation_token') and
                        text.strip().strip('。！! ') in {'确认', '确认执行', '是', '是的', '同意'})
        if confirmation:
            if c['bot_job_id']:
                raise SupportError('操作正在处理中，请等待结果')
            return {'confirmation': True, 'message': text}
        self.invalidate(tx, c)
        inflight = tx.one("SELECT operation_id FROM support_operations WHERE session_id=? AND status IN ('running','unknown') LIMIT 1", (c['session_id'],))
        if inflight:
            self.store._handoff(tx, c, 'business_result_unknown', actor)
            self.store._event(tx, c, 'message', 'system', {'role': 'assistant',
                'content': '已有业务操作开始执行或结果待核实，无法保证撤回。已停止后续执行并转人工核实。'})
            return {}
        # Preserve pre-migration tasks as history, never force their questions
        # onto the new message or infer completion from an ordinary answer.
        for task in self.rows(tx, c['session_id']):
            if task['status'] in ACTIVE:
                task['status'] = 'paused'
                self.save(tx, c, task, actor)
        return {'message': text}


    def receive(self, tx, c, text, actor, job_id, data):
        route = self.route(tx, c, text, actor, data)
        tx.execute('INSERT INTO conversation_task_turns VALUES (?,?,?)', (job_id, c['session_id'], json.dumps(route, ensure_ascii=False)))
        return route

    def turn(self, sid, user, job_id):
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, user)
            row = tx.one('SELECT route FROM conversation_task_turns WHERE session_id=? AND job_id=?', (sid, job_id))
            return json.loads(row['route']) if row else {}

    def message_task_id(self, tx, job_id):
        row = tx.one('SELECT route FROM conversation_task_turns WHERE job_id=?', (job_id,))
        return json.loads(row['route']).get('task_id') if row else None

    def context(self, sid, user, tid, *, query='', exclude_latest_user=False):
        from app.config.settings import settings
        if settings.short_term_memory_enabled:
            from app.session.short_term import ContextService
            return ContextService(self.store).build(sid, user, task_id=tid, query=query,
                                                   exclude_latest_user=exclude_latest_user)
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, user)
            rows = tx.all("""SELECT e.payload FROM support_task_messages m JOIN support_events e
                ON e.session_id=m.session_id AND e.sequence=m.sequence
                WHERE m.session_id=? AND m.task_id=? AND e.visibility='public'
                ORDER BY m.sequence DESC LIMIT 40""", (sid, tid))
            messages = [json.loads(row['payload']) for row in rows]
            result = [{'role': m['role'] if m['role'] == 'user' else 'assistant', 'content': m['content']}
                      for m in reversed(messages[:40])]
            return result[:-1] if exclude_latest_user and result and result[-1]['role'] == 'user' else result

    def started(self, tx, c, job_id):
        row = tx.one('SELECT route FROM conversation_task_turns WHERE job_id=?', (job_id,))
        route = json.loads(row['route']) if row else {}
        if route.get('task_id'):
            task = self.decode(tx.one('SELECT * FROM conversation_tasks WHERE task_id=?', (route['task_id'],)))
            task['status'] = 'running'
            self.save(tx, c, task, 'bot')

    def failed(self, tx, c, job_id):
        row = tx.one('SELECT route FROM conversation_task_turns WHERE job_id=?', (job_id,))
        route = json.loads(row['route']) if row else {}
        if route.get('task_id'):
            task = self.decode(tx.one('SELECT * FROM conversation_tasks WHERE task_id=?', (route['task_id'],)))
            if task['status'] in {'ready', 'running'}:
                task['status'] = 'paused'
                self.save(tx, c, task, 'system')

    def finish(self, tx, c, job_id, response):
        row = tx.one('SELECT route FROM conversation_task_turns WHERE job_id=?', (job_id,))
        route = json.loads(row['route']) if row else {}
        if not route.get('task_id'):
            return
        task = self.decode(tx.one('SELECT * FROM conversation_tasks WHERE task_id=?', (route['task_id'],)))
        question = response.get('question')
        if question:
            task['status'] = 'waiting_input'
            task['question'] = {**question, 'question_id': str(uuid4())}
            if question['field'] == 'details':
                task['question']['previous_details'] = task['slots'].get('details', '')
        elif response.get('confirmation_token'):
            task['status'], task['question'] = 'waiting_confirmation', None
        else:
            task['status'], task['question'] = response.get('task_outcome') or 'completed', None
        self.save(tx, c, task, 'bot')

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
