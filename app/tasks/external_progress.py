"""Finite, durable business polling on the maintenance process, without an LLM."""
import json
import re
from types import SimpleNamespace
from uuid import uuid4

from app.config.settings import settings
from app.harness.bounded_call import bounded_calls
from app.runtime.outbox import clock
from app.services.commerce.common import BusinessError
from app.support.store import SupportError, now
from app.tasks.external_evidence import BusinessProgress, ExternalEvidenceError, finished, submission_only, timestamp, warehouse_goal
from app.tasks.execution_store import TaskRuns
from app.tasks.provenance import lock_input_user, revision, context_event_valid
from app.tools.request_context import set_current_user_id, reset_current_user_id


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def original_requests(tx, user, task_id):
    sources = tx.all('''SELECT e.session_id,e.sequence,e.payload FROM support_task_messages m
        JOIN support_events e ON e.session_id=m.session_id AND e.sequence=m.sequence
        WHERE m.task_id=? AND e.kind='message' AND e.actor=? AND e.visibility='public'
        ORDER BY e.created_at DESC,e.sequence DESC LIMIT 12''', (task_id, user))
    return [json.loads(r['payload'])['content'] for r in sources
            if context_event_valid(tx, r['session_id'], r['sequence'])]


def only_submission_requested(tx, record):
    if not submission_only(record.goal):
        return False
    for text in original_requests(tx, record.user_id, record.task_id):
        if submission_only(text):
            return True
        if re.search(r'不只|不仅|到账|后续|跟进|结果|不要.{0,8}提交', text):
            return False
    return False


def binding_for(tx, runs, record):
    waiting = record.waiting_for
    if not isinstance(waiting, dict) or not waiting.get('evidence_id'):
        raise ExternalEvidenceError('等待进度缺少业务来源')
    from app.tasks.evidence import find_evidence
    stored = find_evidence(tx, runs.user, waiting['evidence_id'], task_id=record.task_id)
    evidence = stored['payload'] if stored else {}
    if evidence.get('result', {}).get('success') is not True:
        raise ExternalEvidenceError('外部等待来源尚未核实')
    details = record.details
    # New contracts bind follow-up to the actual confirmed receipt, including
    # cases where the model did not copy the selected order into task details.
    if waiting.get('operation_id'):
        op=tx.one('''SELECT p.payload,o.tool,o.status,o.result FROM task_operation_proposals p
            JOIN support_operations o ON o.operation_id=p.operation_id
            WHERE p.user_id=? AND p.task_id=? AND p.operation_id=?''',(runs.user,record.task_id,waiting['operation_id']))
        if op and op['tool'] in {'prepare_after_sale','prepare_invoice','prepare_exchange','prepare_repair','prepare_price_protection','prepare_invoice_reissue','prepare_order_cancel'}:
            receipt=json.loads(op['result'] or '{}'); prepared=json.loads(op['payload'])['prepared']
            if op['status']!='succeeded' or receipt.get('success') is not True or receipt.get('request_id')!=waiting['operation_id']:
                raise ExternalEvidenceError('业务操作尚未确认登记')
            if any(details.get(k) and details[k]!=prepared['arguments'][k] for k in ('order_id','platform_code')):
                raise ExternalEvidenceError('事项对象已改变')
            kind='commerce_invoice' if receipt.get('invoice_id') else 'commerce_sale'
            return dict(kind=kind,order_id=prepared['arguments']['order_id'],platform_code=prepared['arguments']['platform_code'],
                operation_id=waiting['operation_id'],after_sale_id=receipt.get('after_sale_id'),invoice_id=receipt.get('invoice_id'),
                is_demo=receipt.get('is_demo') is True,
                completion='submission' if only_submission_requested(tx,record) else 'commerce_result')
    if not waiting.get('operation_id'):
        from app.tasks.external_queries import query_source, query_binding, validate_binding
        current = tx.one('SELECT binding FROM task_external_waits WHERE task_id=?', (record.task_id,))
        saved = json.loads(current['binding']) if current else {}
        if saved.get('contract') == 2 and saved.get('evidence_id') == waiting['evidence_id']:
            validate_binding(tx, runs, record, saved)
            return saved
        evidence = query_source(tx, runs, record, fresh=True)
        binding = query_binding(evidence, record, original_requests(tx, runs.user, record.task_id))
        if binding['kind'] == 'query_sale' and only_submission_requested(tx, record):
            binding['completion'] = 'submission'
        return binding
    if not details.get('order_id'):
        raise ExternalEvidenceError('外部等待没有明确订单')
    binding = {k: details[k] for k in ('order_id', 'platform_code', 'store_id', 'item_id', 'item_name', 'sku_id', 'product_id',
        'quantity', 'after_sale_id', 'tracking_number') if details.get(k) is not None}
    binding['completion'] = 'refund_settled'
    if waiting.get('operation_id'):
        op = tx.one('''SELECT p.payload,o.tool,o.status,o.result FROM task_operation_proposals p
            JOIN support_operations o ON o.operation_id=p.operation_id
            WHERE p.user_id=? AND p.task_id=? AND p.operation_id=?''',
            (runs.user, record.task_id, waiting['operation_id']))
        if not op or op['status'] != 'succeeded':
            raise ExternalEvidenceError('业务操作尚未确认受理')
        prepared = json.loads(op['payload'])['prepared']
        if prepared['arguments']['order_id'] != details['order_id']:
            raise ExternalEvidenceError('事项对象已改变，不能移用原操作')
        binding['operation_id'] = waiting['operation_id']
        binding['is_demo'] = prepared['review'].get('is_demo') is True
        if op['tool'] == 'apply_refund':
            binding['kind'] = 'refund'
        elif op['tool'] in {'issue_invoice', 'reissue_invoice'}:
            invoice = json.loads(op['result'] or '{}').get('invoice', {})
            if not invoice.get('invoice_id') or invoice.get('order_id') != binding['order_id']:
                raise ExternalEvidenceError('没有可跟进的本地发票编号')
            binding.update(kind='invoice', invoice_id=invoice['invoice_id'],
                completion='local_invoice' if '演示' in record.goal or '本地' in record.goal else 'manual')
        else:
            raise ExternalEvidenceError('业务操作尚无可跟进的查询接口')
    # A model-produced goal alone cannot turn an end-to-end refund into a
    # completed submission. Require the explicit same-task original request.
    originals = original_requests(tx, runs.user, record.task_id)
    if only_submission_requested(tx, record) and binding['kind'] in {'refund', 'return'}:
        binding['completion'] = 'submission'
    if binding['kind'] == 'logistics' and any('未收到' in t or '没收到' in t for t in originals):
        binding['completion'] = 'manual'
    if binding['kind'] == 'return' and warehouse_goal(record.goal):
        for text in originals:
            if re.search(r'退款|到账|不只|不仅', text):
                break
            if warehouse_goal(text):
                binding['completion'] = 'warehouse_received'
                break
    return binding


class ExternalProgress:
    def __init__(self, store, *, business=None):
        self.store, self.business = store, business or BusinessProgress()

    def _eligible(self, tx, runs, record, sid):
        runs._control(tx, SimpleNamespace(session_id=sid, task_id=record.task_id))
        if record.status != 'waiting_external':
            raise SupportError('事项不处于外部等待')
        if tx.one("""SELECT i.request_id FROM message_intake_items i JOIN support_conversations c
                ON c.session_id=i.session_id WHERE i.user_id=? AND i.association_status='pending'
                AND c.mode='bot' LIMIT 1""", (runs.user,)):
            raise SupportError('先理解用户补充，再更新办理进度')
        if tx.one("""SELECT a.clarification_id FROM association_clarifications a JOIN support_conversations c
                ON c.session_id=a.session_id WHERE a.user_id=? AND a.status='open' AND c.mode='bot'
                AND (a.candidates='[]' OR a.candidates LIKE ?) LIMIT 1""", (runs.user, '%' + record.task_id + '%')):
            raise SupportError('事项归属仍待澄清')

    def discover(self, limit):
        with self.store.transaction(read_only=True) as tx:
            rows = tx.all('''SELECT t.task_id,t.user_id FROM conversation_tasks t
                LEFT JOIN task_external_waits w ON w.task_id=t.task_id
                WHERE t.storage_mode='durable' AND t.status='waiting_external'
                AND (w.task_id IS NULL OR w.task_version<>t.version)
                AND NOT EXISTS(SELECT 1 FROM task_handoffs h WHERE h.task_id=t.task_id AND h.released_at IS NULL)
                AND NOT EXISTS(SELECT 1 FROM task_sessions s JOIN support_conversations c ON c.session_id=s.session_id
                    WHERE s.task_id=t.task_id AND c.mode<>'bot')
                AND NOT EXISTS(SELECT 1 FROM message_intake_items i JOIN support_conversations c ON c.session_id=i.session_id
                    WHERE i.user_id=t.user_id AND i.association_status='pending' AND c.mode='bot')
                AND NOT EXISTS(SELECT 1 FROM association_clarifications a JOIN support_conversations c ON c.session_id=a.session_id
                    WHERE a.user_id=t.user_id AND a.status='open' AND c.mode='bot'
                    AND (a.candidates='[]' OR a.candidates LIKE ? || t.task_id || ?))
                ORDER BY t.updated_at,t.task_id LIMIT ?''', ('%', '%', limit))
        for row in rows:
            runs = TaskRuns(self.store, row['user_id'])
            with self.store.transaction() as tx:
                lock_input_user(tx, runs.user)
                linked = tx.one('''SELECT e.session_id FROM support_task_messages s JOIN support_events e
                    ON e.session_id=s.session_id AND e.sequence=s.sequence
                    WHERE s.task_id=? AND e.kind='message' AND e.actor=? AND e.visibility='public'
                    ORDER BY e.created_at DESC,e.sequence DESC LIMIT 1''', (row['task_id'], runs.user))
                if not linked:
                    continue
                control = self.store._control(tx, linked['session_id'], runs.user)
                revision(tx, runs.user, lock=True)
                record = runs.repo._record(runs.repo._row(tx, row['task_id'], lock=True))
                try:
                    self._eligible(tx, runs, record, control['session_id'])
                except SupportError:
                    continue
                try:
                    binding = binding_for(tx, runs, record)
                except (ExternalEvidenceError, BusinessError, ValueError, KeyError, TypeError):
                    self.store._handoff(tx, control, 'external_scope_unverified', 'system')
                    continue
                current = tx.one('SELECT * FROM task_external_waits WHERE task_id=?', (record.task_id,))
                encoded = encode(binding)
                if current and current['binding'] == encoded:
                    tx.execute('''UPDATE task_external_waits SET task_version=?,session_id=?,lease_owner=NULL,lease_until=NULL
                        WHERE task_id=?''', (record.version, control['session_id'], record.task_id))
                    if current['state'] == 'human':
                        task_field = "e.payload::jsonb->>'task_id'" if tx.postgres else "json_extract(e.payload,'$.task_id')"
                        version_field = "(e.payload::jsonb->>'previous_version')::integer" if tx.postgres else "json_extract(e.payload,'$.previous_version')"
                        resumed = tx.one(f'''SELECT e.sequence FROM support_events e JOIN support_conversations c
                            ON c.session_id=e.session_id WHERE c.user_id=? AND e.kind='task_resumed'
                            AND {task_field}=? AND {version_field}>=? LIMIT 1''',
                            (runs.user, record.task_id, current['task_version']))
                        if resumed:
                            tx.execute("UPDATE task_external_waits SET state='waiting',attempts=0,next_check=? WHERE task_id=?",
                                       (clock(tx), record.task_id))
                else:
                    tx.execute('''INSERT INTO task_external_waits
                        (task_id,user_id,session_id,task_version,binding,state,next_check)
                        VALUES (?,?,?,?,?,'waiting',?) ON CONFLICT(task_id) DO UPDATE SET
                        session_id=excluded.session_id,task_version=excluded.task_version,binding=excluded.binding,
                        state='waiting',attempts=0,next_check=excluded.next_check,lease_owner=NULL,lease_until=NULL,
                        last_observation=NULL,last_error=NULL''',
                        (record.task_id, runs.user, control['session_id'], record.version, encoded, clock(tx)))

    def _query_current(self, tx, runs, record, saved, control):
        binding = json.loads(saved['binding'])
        if record.waiting_for.get('operation_id'):
            return True
        from app.tasks.external_queries import validate_binding
        try:
            validate_binding(tx, runs, record, binding)
        except (ExternalEvidenceError, BusinessError, ValueError, KeyError, TypeError):
            tx.execute("UPDATE task_external_waits SET state='human',last_error='external_scope_unverified',lease_owner=NULL,lease_until=NULL WHERE task_id=?", (record.task_id,))
            self.store._handoff(tx, control, 'external_scope_unverified', 'system')
            return False
        return True

    def claim(self, task_id, user):
        runs = TaskRuns(self.store, user)
        with self.store.transaction() as tx:
            lock_input_user(tx, user)
            saved = tx.one('SELECT * FROM task_external_waits WHERE task_id=? AND user_id=?', (task_id, user))
            if not saved:
                return None
            control = self.store._control(tx, saved['session_id'], user)
            revision(tx, user, lock=True)
            record = runs.repo._record(runs.repo._row(tx, task_id, lock=True))
            saved = tx.one('SELECT * FROM task_external_waits WHERE task_id=?', (task_id,))
            at = clock(tx)
            if (saved['state'] != 'waiting' or saved['task_version'] != record.version or saved['next_check'] > at
                    or saved['lease_owner'] and saved['lease_until'] > at):
                return None
            try:
                self._eligible(tx, runs, record, saved['session_id'])
            except SupportError:
                tx.execute('UPDATE task_external_waits SET next_check=? WHERE task_id=?',
                    (at + settings.task_external_interval_seconds, task_id))
                return None
            if not self._query_current(tx, runs, record, saved, control):
                return None
            if saved['attempts'] >= settings.task_external_max_attempts:
                tx.execute("UPDATE task_external_waits SET state='human',lease_owner=NULL,lease_until=NULL WHERE task_id=?", (task_id,))
                self.store._handoff(tx, control, 'external_check_limit', 'system')
                return None
            owner = str(uuid4())
            tx.execute('''UPDATE task_external_waits SET attempts=attempts+1,lease_owner=?,lease_until=? WHERE task_id=?''',
                (owner, at + settings.task_external_timeout_seconds + 5, task_id))
            return {**dict(saved), 'lease_owner': owner, 'attempts': saved['attempts'] + 1}

    def apply(self, lease, value=None, error=None):
        user, tid = lease['user_id'], lease['task_id']
        runs = TaskRuns(self.store, user)
        with self.store.transaction() as tx:
            lock_input_user(tx, user)
            control = self.store._control(tx, lease['session_id'], user)
            revision(tx, user, lock=True)
            record = runs.repo._record(runs.repo._row(tx, tid, lock=True))
            current = tx.one('SELECT * FROM task_external_waits WHERE task_id=?', (tid,))
            if not current or current['lease_owner'] != lease['lease_owner']:
                return 'stale'
            at = clock(tx)
            tx.execute('''UPDATE task_external_waits SET lease_owner=NULL,lease_until=NULL,next_check=?,last_error=?
                WHERE task_id=?''', (at + settings.task_external_interval_seconds, error, tid))
            try:
                self._eligible(tx, runs, record, lease['session_id'])
                if record.version != lease['task_version'] or current['lease_until'] < at:
                    return 'stale'
            except SupportError:
                return 'stale'
            if not self._query_current(tx, runs, record, current, control):
                return 'stale'
            if error:
                return 'retry'
            binding = json.loads(lease['binding'])
            if not value or value.get('binding') != binding:
                raise ExternalEvidenceError('外部结果对象不一致')
            previous = json.loads(current['last_observation']) if current['last_observation'] else None
            if previous:
                if (timestamp(value['observed_at']) < timestamp(previous['observed_at']) or value['rank'] < previous['rank']
                        or (value['reference'] != previous['reference'] and previous['source'] != 'mysql_order_fields')
                        or timestamp(value['observed_at']) == timestamp(previous['observed_at'])
                        and value['progress_key'] != previous['progress_key']):
                    tx.execute("UPDATE task_external_waits SET last_error='external_order_conflict' WHERE task_id=?", (tid,))
                    return 'retry'
                if value['progress_key'] == previous['progress_key']:
                    return 'unchanged'
            inserted = tx.execute('INSERT INTO task_external_receipts VALUES (?,?,?) ON CONFLICT DO NOTHING',
                                  (tid, value['event_id'], encode(value))).rowcount
            if not inserted:
                return 'duplicate'
            eid = 'external:' + tid + ':' + value['event_id']
            self.store._event(tx, control, 'task_external_result', 'system', dict(
                event_id=eid, tool='external_progress', result=dict(success=True, progress=value),
                task_id=tid, task_version=record.version, observed_at=now()),
                private=True, key='task-tool:' + eid)
            completed = finished(binding, value)
            waiting = {**record.waiting_for, 'external_progress': value, 'next_check_at': at + settings.task_external_interval_seconds}
            after = runs.repo.update(tid, lease['session_id'], expected_version=record.version,
                change_id=eid, source=dict(kind='tool_result', session_id=lease['session_id'], event_id=eid),
                patch=dict(status='completed' if completed else 'waiting_external', waiting_for=waiting), tx=tx)
            # This version already has a verified public progress event below.
            # It must not wake an empty Agent execution that asks the user again.
            tx.execute('UPDATE task_execution_wakes SET version=?,job_id=NULL,retry_at=0 WHERE task_id=?',
                       (after.version, tid))
            tx.execute('''UPDATE task_external_waits SET task_version=?,last_observation=?,last_error=NULL,state=? WHERE task_id=?''',
                (after.version, encode(value), 'completed' if completed else 'waiting', tid))
            from app.tasks.reply_facts import label
            content = '订单 ' + label(binding['order_id']) + '：' + value['content']
            if binding.get('tracking_number'):
                content += ' 运单：' + label(binding['tracking_number']) + '；仅代表这一票包裹。'
            if value['is_demo']:
                content += ' 本次为演示业务数据。'
            if value.get('deadline'):
                content += ' 业务系统要求期限：' + value['deadline'] + '。'
            if value.get('required_materials'):
                content += ' 业务系统要求材料：' + '、'.join(label(x) for x in value['required_materials']) + '。'
            control = self.store._control(tx, lease['session_id'], user)
            self.store._event(tx, control, 'message', 'bot', dict(role='assistant', content=content,
                task_id=tid, task_version=after.version, external_event_id=value['event_id'], evidence_ids=[eid]), key=eid)
            if value['phase']=='business_rejected':
                tx.execute("UPDATE task_external_waits SET state='human' WHERE task_id=?", (tid,))
                self.store._handoff(tx, control, 'business_request_rejected', 'system')
            elif binding['completion'] == 'manual' and value['phase'] in {'delivered', 'issued_local', 'refund_channel_succeeded'}:
                tx.execute("UPDATE task_external_waits SET state='human' WHERE task_id=?", (tid,))
                self.store._handoff(tx, control, 'external_scope_or_dispute', 'system')
            elif not completed and value.get('deadline') and timestamp(now()) >= value['deadline']:
                tx.execute("UPDATE task_external_waits SET state='human' WHERE task_id=?", (tid,))
                self.store._handoff(tx, control, 'external_deadline_requires_review', 'system')
            return 'completed' if completed else 'updated'

    def tick(self, limit=4):
        if not settings.task_external_progress_enabled:
            return []
        if not 1 <= limit <= 20:
            raise ValueError('External progress batch must be 1..20')
        self.discover(limit)
        with self.store.transaction(read_only=True) as tx:
            rows = tx.all("""SELECT w.task_id,w.user_id FROM task_external_waits w
                JOIN conversation_tasks t ON t.task_id=w.task_id WHERE w.state='waiting'
                AND t.status='waiting_external' AND w.next_check<=?
                AND NOT EXISTS(SELECT 1 FROM task_handoffs h WHERE h.task_id=w.task_id AND h.released_at IS NULL)
                ORDER BY w.next_check LIMIT ?""", (clock(tx), limit))
        results = []
        for row in rows:
            lease = self.claim(row['task_id'], row['user_id'])
            if not lease:
                continue
            def query(lease=lease):
                token = set_current_user_id(lease['user_id'])
                try:
                    return self.business.read(lease['user_id'], json.loads(lease['binding']))
                finally:
                    reset_current_user_id(token)
            try:
                value = bounded_calls.run(query, settings.task_external_timeout_seconds)
                results.append(self.apply(lease, value))
            except Exception as exc:
                # Persist a bounded diagnostic, not business payloads or secrets.
                results.append(self.apply(lease, error=type(exc).__name__))
        return results
