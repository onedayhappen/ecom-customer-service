"""Task transitions use the same conversation transaction as message admission."""
import json
from uuid import uuid4

from app.tasks.routing import LABELS, ORDER, effective_text, parse, select_option, is_new_request, task_matches

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

    def ask_target(self, tx, c, tasks, action, text, actor):
        question = {'question_id': str(uuid4()), 'field': 'task_choice', 'prompt': '这条消息要处理哪个待办？',
                    'options': [{'value': t['task_id'], 'label': f"{t['title']}：{t['request_text'][:60]}", 'version': t['version']} for t in tasks],
                    'action': action, 'original_text': text}
        tx.execute('INSERT INTO conversation_task_choices VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload',
                   (c['session_id'], json.dumps(question, ensure_ascii=False)))
        self.store._event(tx, c, 'task_choice', actor, {'question': question})
        return {'reply': question['prompt'], 'background_control': True}

    def route(self, tx, c, text, actor, data):
        from app.config.settings import settings
        if settings.assistant_just_react and not data.get('task_action'):
            return self.conversation_route(tx, c, text, actor, data)
        return self.legacy_route(tx, c, text, actor, data)

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

    def legacy_route(self, tx, c, text, actor, data):
        from app.support.store import SupportError
        if c['mode'] != 'bot':
            if data.get('task_action'):
                raise SupportError('人工服务期间不能由机器人推进待办')
            return {}
        tasks = self.rows(tx, c['session_id'])
        pending = [t for t in tasks if t['status'] not in TERMINAL]
        active = next((t for t in pending if t['status'] in ACTIVE), None)
        command = parse(text)
        action, specs = command['action'], command['specs']
        target = None
        explicit = data.get('task_action')
        if command['keep'] and not specs and not action and not explicit:
            return {'reply': '好的，保留现有任务，不执行取消或暂停。', 'background_control': True}
        choice = tx.one('SELECT payload FROM conversation_task_choices WHERE session_id=?', (c['session_id'],))
        if choice and (data.get('question_id') or not specs and not explicit and not action):
            question = json.loads(choice['payload'])
            if data.get('question_id') and data['question_id'] != question['question_id']:
                raise SupportError('问题已更新，请使用最新卡片')
            options = question['options']
            option = select_option(options, text)
            if not option:
                return {'reply': '请选择卡片中的待办，或说明新的诉求。'}
            target = next((t for t in tasks if t['task_id'] == option['value'] and t['version'] == option['version']), None)
            if not target:
                raise SupportError('待办已变化，请重新选择')
            action, text, specs = question['action'], question['original_text'], []
            if action in {'cancel', 'pause', 'amend'}:
                command = parse(text)
                specs = command['specs']
            tx.execute('DELETE FROM conversation_task_choices WHERE session_id=?', (c['session_id'],))
            self.store._event(tx, c, 'task_choice', actor, {'question': None})
        elif explicit:
            target = next((t for t in tasks if t['task_id'] == data.get('task_id')), None)
            if not target:
                raise SupportError('任务不存在', 404)
            if data.get('task_version') != target['version']:
                raise SupportError('任务已更新，请使用最新卡片')
            action, specs = explicit, []
            if action == 'amend':
                specs = command['specs']
            if action == 'answer':
                question = target['question']
                if target['status'] != 'waiting_input' or not question or question['question_id'] != data.get('question_id'):
                    raise SupportError('问题已失效，请使用最新卡片')
        elif action:
            candidates = [t for t in pending if not command['target'] or task_matches(t, command['target'])]
            if active and active in candidates:
                target = active
            elif len(candidates) == 1:
                target = candidates[0]
            elif len(candidates) > 1:
                return self.ask_target(tx, c, candidates, action, text, actor)
        elif active and active['status'] == 'waiting_input' and (not specs or not is_new_request(text) or len(specs) == 1 and task_matches(active, specs[0]['intent'])):
            target, action, specs = active, 'answer', []
        elif data.get('confirmation_token'):
            if not active or active['status'] != 'waiting_confirmation':
                raise SupportError('确认对应的任务已失效，请重新核对')
            target, action, specs = active, 'confirm', []
        elif not specs and pending and not active:
            if len(pending) > 1:
                return self.ask_target(tx, c, pending, 'answer', text, actor)
            target, action = pending[0], 'answer'
        elif not specs and active and active['status'] == 'waiting_confirmation':
            if text.strip().strip('。！!') in {'确认', '确认执行', '是的', '同意'}:
                return {'reply': '请使用确认按钮提交业务操作；页面刷新后可点击待办“继续”重新核对。'}
            target, action = active, 'amend'

        if target and target['status'] in TERMINAL:
            raise SupportError('任务已结束；已提交业务的撤销需要另外核实，不能取消待办来撤回')
        if action == 'amend' and target and target['question'] and not specs:
            action, text = 'answer', command.get('replacement') or text
        if action and not target and not specs:
            return {'reply': '没有找到对应的待办，请从任务列表选择。'}
        retiring = int(bool(target and specs and action in {'cancel', 'amend'}))
        if len(specs) > 5 or len(pending) + len(specs) - retiring > 20:
            raise SupportError('一次最多新增 5 个诉求，会话最多保留 20 个未完成任务', 422)

        # Do not treat a supplementary sentence about the same task as a new todo.
        if not action and len(specs) == 1 and active and task_matches(active, specs[0]['intent']):
            old_order = ORDER.search(effective_text(active))
            new_order = ORDER.search(text)
            if not (old_order and new_order and old_order.group().upper() != new_order.group().upper()):
                target, action, specs = active, 'amend', []

        intervenes = action in {'cancel', 'pause', 'resume', 'amend', 'answer'} or bool(specs)
        background_control = bool(target and action in {'cancel', 'pause'} and not specs
                                  and target['status'] not in ACTIVE)
        if background_control:
            target['status'] = 'cancelled' if action == 'cancel' else 'paused'
            if action == 'cancel':
                target['question'] = None
            self.save(tx, c, target, actor)
            return {'reply': f"已{'取消' if action == 'cancel' else '暂停'}“{target['title']}”；当前任务继续处理。",
                    'background_control': True, 'control_task_id': target['task_id']}
        if intervenes:
            previous_version = c['control_version']
            self.invalidate(tx, c)
            inflight = tx.one("SELECT operation_id FROM support_operations WHERE session_id=? AND status IN ('running','unknown') LIMIT 1", (c['session_id'],))
            if inflight:
                for spec in specs:
                    self.create(tx, c, spec, actor)
                self.store._handoff(tx, c, 'business_result_unknown', actor)
                self.store._event(tx, c, 'message', 'system', {'role': 'assistant', 'content': '业务操作已开始或结果待核实，无法保证撤回。已暂停后续任务并转人工核实。'})
                return {}
            submitted = tx.one("SELECT operation_id FROM support_operations WHERE session_id=? AND control_version=? AND status='succeeded' LIMIT 1", (c['session_id'], previous_version))
            if submitted and target and target['status'] == 'running':
                target['status'], target['question'] = 'completed', None
                self.save(tx, c, target, actor)
                self.store._event(tx, c, 'message', 'system', {'role': 'assistant', 'content': '原任务的业务操作已经提交成功，不能通过取消待办撤回。后续处理已停止，如需撤销业务请另行提出。'})
                if not specs:
                    return {'reply': '已保留成功的业务结果；如需撤销，请说明具体业务申请。'}
                target = None
                active = None
            if active and (not target or active['task_id'] != target['task_id']):
                active['status'] = 'paused'
                self.save(tx, c, active, actor)

        if target and action in {'cancel', 'pause'}:
            target['status'] = 'cancelled' if action == 'cancel' else 'paused'
            if action == 'cancel':
                target['question'] = None
            self.save(tx, c, target, actor)
            if not specs:
                return {'reply': f"已{'取消' if action == 'cancel' else '暂停'}“{target['title']}”的后续处理。"}
            target = None
        if specs:
            if target and action == 'amend':
                target['status'], target['question'] = 'cancelled', None
                self.save(tx, c, target, actor)
            created = [self.create(tx, c, spec, actor) for spec in specs]
            target = created[0]
        if not target:
            return {}
        if action == 'amend' and not specs:
            if parse(text)['specs']:
                target['request_text'] = text
            else:
                target['slots']['details'] = text
            orders = ORDER.findall(command.get('replacement') or '')
            if len(orders) == 1:
                target['slots']['order_id'] = orders[0].upper()
            target['question'] = None
        if action == 'answer':
            question = target['question']
            field = question['field'] if question else 'details'
            value = text.strip()
            options = question.get('options', []) if question else []
            selected = select_option(options, value)
            if selected:
                value = selected['value']
            if field == 'order_id':
                matches = ORDER.findall(value)
                if len(matches) != 1:
                    return {'reply': '请提供完整订单号，或点击订单选项。'}
                value = matches[0].upper()
            target['slots'][field] = value
            if field == 'details' and question and question.get('previous_details'):
                target['slots'][field] = question['previous_details'] + '\n' + value
            if len(target['slots'][field]) > 8000:
                raise SupportError('补充信息过长，请简要描述', 422)
            target['question'] = None
        if action == 'resume' and target['question']:
            target['status'] = 'waiting_input'
            target['question']['question_id'] = str(uuid4())
            self.save(tx, c, target, actor)
            return {'reply': target['question']['prompt']}
        target['status'] = 'ready'
        self.save(tx, c, target, actor)
        return {'task_id': target['task_id'], 'task_version': target['version'], 'intent': target['intent'],
                'message': effective_text(target), 'confirmation': action == 'confirm'}

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

    def adopt(self, sid, user, version, intent, message):
        """A classified business intent not recognized by the ingress rules becomes a task too."""
        from app.support.store import SupportError
        if intent not in LABELS or intent in {'greetings', 'closure', 'others'}:
            return None
        with self.store.transaction() as tx:
            c = self.store._check(tx, sid, user, version)
            tasks = self.rows(tx, sid)
            if any(t['status'] in ACTIVE for t in tasks):
                return None
            if sum(t['status'] not in TERMINAL for t in tasks) >= 20:
                raise SupportError('会话未完成任务已达上限', 422)
            task = self.create(tx, c, {'intent': intent, 'text': message}, user)
            task['status'] = 'running'
            self.save(tx, c, task, 'bot')
            route = {'task_id': task['task_id'], 'task_version': task['version'], 'intent': intent, 'message': message}
            tx.execute('INSERT INTO conversation_task_turns VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET route=excluded.route',
                       (c['bot_job_id'], sid, json.dumps(route, ensure_ascii=False)))
            # Ingress did not yet know this task. Associate the original public
            # event without rewriting the immutable transcript payload.
            tx.execute('''INSERT INTO support_task_messages(task_id,session_id,sequence)
                SELECT ?,session_id,sequence FROM support_events
                WHERE session_id=? AND event_key=? AND kind='message' AND visibility='public'
                ON CONFLICT DO NOTHING''', (task['task_id'], sid, f"user:{c['bot_job_id']}"))
            from app.config.settings import settings
            if settings.short_term_memory_enabled:
                from app.session.short_term import enqueue_context_job
                enqueue_context_job(tx, c, task['task_id'])
            return task
