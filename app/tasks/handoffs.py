# 文件说明：维护事项人工接管、版本化交接更新及显式恢复后的执行上下文。
"""Durable task handovers on top of the existing support ticket permissions."""
import json
from typing import get_args

from app.agents.association import DetailField
from app.memory.source_policy import safe_context
from app.runtime.outbox import clock
from app.support.store import SupportError, now
from app.tasks.models import TaskRecord
from app.tasks.provenance import revision, context_event_valid, invalid
from app.tasks.repository import TaskRepository


def staff_statements(tx, user, task_id):
    field = "e.payload::jsonb->>'ticket_id'" if tx.postgres else "json_extract(e.payload,'$.ticket_id')"
    rows = tx.all(f"""SELECT e.session_id,e.sequence,e.payload,e.actor,e.created_at
        FROM support_events e JOIN support_conversations c ON c.session_id=e.session_id
        JOIN task_handoffs h ON h.ticket_id={field} AND h.user_id=c.user_id
        WHERE h.user_id=? AND h.task_id=? AND e.kind='message' AND e.visibility='public'
        ORDER BY e.created_at DESC,e.sequence DESC LIMIT 8""", (user, task_id))
    return [dict(session_id=r['session_id'], sequence=r['sequence'], actor=r['actor'],
        created_at=r['created_at'], content=json.loads(r['payload'])['content'],
        scope='工单公开沟通，可能涉及多个事项；不是业务回执或权益授权')
        for r in reversed(rows) if json.loads(r['payload']).get('role') == 'agent'
        and context_event_valid(tx, r['session_id'], r['sequence'])]


def handoff_context(tx, user, task_id):
    active = tx.all("""SELECT h.ticket_id,t.status FROM task_handoffs h
        JOIN support_tickets t ON t.ticket_id=h.ticket_id
        WHERE h.user_id=? AND h.task_id=? AND h.released_at IS NULL ORDER BY h.acquired_at""", (user, task_id))
    field = "e.payload::jsonb->>'task_id'" if tx.postgres else "json_extract(e.payload,'$.task_id')"
    rows = tx.all(f"""SELECT e.session_id,e.sequence,e.payload,e.actor,e.created_at FROM support_events e
        JOIN support_conversations c ON c.session_id=e.session_id
        WHERE c.user_id=? AND e.kind='task_staff_update' AND {field}=?
        ORDER BY e.created_at DESC,e.sequence DESC LIMIT 5""", (user, task_id))
    updates = []
    for row in reversed(rows):
        payload = json.loads(row['payload'])
        updates.append(dict(session_id=row['session_id'], sequence=row['sequence'], actor=row['actor'],
            created_at=row['created_at'], task_version=payload['after']['version'], patch=payload['patch']))
    statements = staff_statements(tx, user, task_id)
    if not active and not updates and not statements:
        return {}
    return safe_context(dict(active_tickets=[dict(row) for row in active], staff_updates=updates, staff_statements=statements,
        note='人工更新说明当前诉求和待办，不代表业务已经成功，也不授权额外权益；业务结论仍须查询。'))


class TaskHandoffs:
    def __init__(self, store, user):
        self.store, self.user = store, user
        self.repo = TaskRepository(store, user)

    def snapshot(self, tx, task_id):
        record = self.repo._record(self.repo._row(tx, task_id))
        sources = tx.all("""SELECT e.session_id,e.sequence,e.payload,e.created_at
            FROM support_task_messages m JOIN support_events e
            ON e.session_id=m.session_id AND e.sequence=m.sequence
            JOIN support_conversations c ON c.session_id=e.session_id
            WHERE m.task_id=? AND c.user_id=? AND e.kind='message'
            AND e.visibility='public' AND e.actor=?
            ORDER BY e.created_at DESC,e.sequence DESC LIMIT 12""", (task_id, self.user, self.user))
        field = "e.payload::jsonb->>'task_id'" if tx.postgres else "json_extract(e.payload,'$.task_id')"
        rows = tx.all(f"""SELECT e.session_id,e.sequence,e.payload FROM support_events e
            JOIN support_conversations c ON c.session_id=e.session_id
            WHERE c.user_id=? AND e.kind='task_tool_result' AND {field}=?
            AND e.actor='system' AND e.visibility='internal'
            ORDER BY e.created_at DESC,e.sequence DESC LIMIT 12""", (self.user, task_id))
        from app.tasks.handoff_facts import observation, operation_result
        evidence = []
        for row in rows:
            if not context_event_valid(tx, row['session_id'], row['sequence']):
                continue
            receipt = json.loads(row['payload'])
            # Images use the current validated attachment/observation view below.
            # Recalled memory and context are not business query receipts.
            if (receipt.get('late') or receipt.get('tool') in {'inspect_image', 'recall_user_memory', 'read_task_context'}
                    or any(invalid(tx, self.user, 'decision', did)
                           for did in (receipt.get('provenance') or {}).get('decisions', []))):
                continue
            evidence.append(dict(event_id=receipt['event_id'], tool=receipt['tool'],
                observed_at=receipt['observed_at'],
                task_version=receipt['task_version'], object=receipt['object'],
                **observation(receipt),
                session_id=row['session_id'], sequence=row['sequence']))
        operations = tx.all("""SELECT o.operation_id,o.tool,o.status,o.result,o.created_at
            FROM task_operation_proposals p JOIN support_operations o ON o.operation_id=p.operation_id
            WHERE p.user_id=? AND p.task_id=? ORDER BY p.created_at DESC LIMIT 30""", (self.user, task_id))
        receipts = []
        for row in operations:
            result = json.loads(row['result']) if row['result'] else {}
            facts, omitted = operation_result(result)
            receipts.append({**dict(row), 'result': facts, 'evidence_kind': 'business_operation',
                             'summary_omitted_paths': omitted,
                             'note': '操作状态与业务回执分别核对；申请提交不代表退款到账、履约或开票完成。'})
        from app.media.context import task_images
        return safe_context(dict(task=record.model_dump(),
            customer_statements=[dict(session_id=r['session_id'], sequence=r['sequence'],
                created_at=r['created_at'], content=json.loads(r['payload'])['content']) for r in reversed(sources)
                if context_event_valid(tx, r['session_id'], r['sequence'])],
            staff_statements=staff_statements(tx, self.user, task_id),
            observed_evidence=evidence, operations=receipts,
            image_observations=task_images(tx, self.user, task_id),
            note='事项字段和用户原话表示诉求；查询回执表示当时核实的结果，需核对时间与对象。'
                 '这里只展示有限的交接摘要；未载入或缺失字段不代表没有真实业务记录。图片仅为观察线索。'
                 '人工说明和其他客服的权益承诺不授予业务权限；赔付、改价等未支持权益只能记录请求。'))

    def acquire(self, tx, control, ticket_id):
        rows = tx.all("""SELECT t.* FROM conversation_tasks t JOIN task_sessions s ON s.task_id=t.task_id
            WHERE s.session_id=? AND t.user_id=? AND t.storage_mode='durable'
            AND t.status NOT IN ('completed','cancelled') ORDER BY t.task_id""", (control['session_id'], self.user))
        field = "payload::jsonb->>'task_id'" if tx.postgres else "json_extract(payload,'$.task_id')"
        for row in rows:
            record = self.repo._record(self.repo._row(tx, row['task_id'], lock=True))
            inserted = tx.execute('INSERT INTO task_handoffs VALUES (?,?,?,?,?,?,NULL) ON CONFLICT DO NOTHING',
                (ticket_id, record.task_id, self.user, record.version,
                 json.dumps(self.snapshot(tx, record.task_id), ensure_ascii=False), now())).rowcount
            if not inserted:
                continue
            # Task jobs in every linked session stop. Already-started business
            # operations keep their own running/unknown status and receipts.
            tx.execute(f"""UPDATE runtime_jobs SET status='superseded',finished_at=?,error='事项已转人工'
                WHERE user_id=? AND kind='task_execution' AND {field}=?
                AND status IN ('pending','dispatched','running')""", (clock(tx), self.user, record.task_id))
            tx.execute("""UPDATE support_operations SET status='cancelled'
                WHERE status='prepared' AND operation_id IN (
                    SELECT operation_id FROM task_operation_proposals WHERE user_id=? AND task_id=?)""",
                (self.user, record.task_id))
            tx.execute(f"""UPDATE task_reply_items SET disposition='stale' WHERE user_id=? AND item_key IN (
                SELECT 'job:' || job_id FROM runtime_jobs WHERE user_id=? AND kind='task_execution' AND {field}=?)
                AND disposition='deferred'""", (self.user, self.user, record.task_id))

    def require_resumable(self, tx, ticket_id):
        unknown = tx.one("""SELECT o.operation_id FROM support_operations o
            JOIN task_operation_proposals p ON p.operation_id=o.operation_id
            JOIN task_handoffs h ON h.task_id=p.task_id AND h.user_id=p.user_id
            WHERE h.ticket_id=? AND h.user_id=? AND h.released_at IS NULL
            AND o.status IN ('running','unknown') LIMIT 1""", (ticket_id, self.user))
        if unknown:
            raise SupportError('关联事项仍有执行中或结果待核实的操作，请先由客服核实')
        legacy = tx.one("""SELECT o.operation_id FROM support_operations o
            JOIN task_sessions s ON s.session_id=o.session_id
            JOIN task_handoffs h ON h.task_id=s.task_id
            WHERE h.ticket_id=? AND h.user_id=? AND h.released_at IS NULL
            AND o.status IN ('running','unknown')
            AND NOT EXISTS(SELECT 1 FROM task_operation_proposals p WHERE p.operation_id=o.operation_id)
            AND (EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id AND m.task_id=h.task_id)
                OR NOT EXISTS(SELECT 1 FROM support_operation_tasks m WHERE m.operation_id=o.operation_id)) LIMIT 1""",
            (ticket_id, self.user))
        if legacy:
            raise SupportError('关联历史操作仍需核实，不能恢复自动执行')

    def release(self, tx, control, ticket_id):
        rows = tx.all('SELECT task_id FROM task_handoffs WHERE user_id=? AND ticket_id=? AND released_at IS NULL ORDER BY task_id',
                      (self.user, ticket_id))
        for row in rows:
            task_id = row['task_id']
            record = self.repo._record(self.repo._row(tx, task_id, lock=True))
            tx.execute('UPDATE task_handoffs SET snapshot=?,released_at=? WHERE ticket_id=? AND task_id=?',
                (json.dumps(self.snapshot(tx, task_id), ensure_ascii=False), now(), ticket_id, task_id))
            if tx.one('SELECT ticket_id FROM task_handoffs WHERE user_id=? AND task_id=? AND released_at IS NULL LIMIT 1',
                      (self.user, task_id)):
                continue
            sequence = self.store._event(tx, control, 'task_resumed', self.user,
                dict(task_id=task_id, ticket_id=ticket_id, previous_version=record.version),
                private=True, key='task-resumed:' + ticket_id + ':' + task_id)
            # A new task version schedules a fresh execution. Paused/closed
            # matters stay paused/closed; old questions are not reissued.
            patch = dict(status='in_progress' if record.status == 'waiting_user' else record.status,
                         waiting_for=record.waiting_for if record.status == 'waiting_external' else None)
            self.repo.update(task_id, control['session_id'], expected_version=record.version,
                change_id='task-resumed:' + ticket_id + ':' + task_id,
                source=dict(kind='support_event', session_id=control['session_id'], sequence=sequence, task_id=task_id),
                patch=patch, tx=tx)

    def ticket_view(self, tx, ticket_id):
        rows = tx.all('SELECT * FROM task_handoffs WHERE ticket_id=? AND user_id=? ORDER BY task_id', (ticket_id, self.user))
        return [dict(task_id=r['task_id'], active=r['released_at'] is None,
            acquired_at=r['acquired_at'], released_at=r['released_at'],
            handover=self.snapshot(tx, r['task_id']) if r['released_at'] is None else json.loads(r['snapshot'])) for r in rows]

    def update(self, ticket_id, actor, *, request_id, task_id, expected_version, details=None, goal=None):
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 200
                or type(expected_version) is not int or expected_version < 1):
            raise SupportError('人工更新编号或版本不合法', 422)
        if details is not None and (not isinstance(details, dict) or not details.keys() <= set(get_args(DetailField))
                or any(v is not None and type(v) not in {str, int} for v in details.values())):
            raise SupportError('只能更新事项字段，不能增加权限或业务结论', 422)
        if any(v is not None and ((type(v) is not int or v < 1) if k == 'quantity' else not isinstance(v, str))
               for k, v in (details or {}).items()):
            raise SupportError('数量须为正整数，其他事项字段须为文字', 422)
        if not details and goal is None:
            raise SupportError('请填写需要更新的事项信息', 422)
        args = dict(ticket_id=ticket_id, actor=actor, task_id=task_id,
                    expected_version=expected_version, details=details, goal=goal)
        fingerprint = self.repo._fingerprint(args)
        with self.store.transaction() as tx:
            ticket = tx.one('SELECT session_id FROM support_tickets WHERE ticket_id=?', (ticket_id,))
            if not ticket:
                raise SupportError('工单不存在', 404)
            control = self.store._control(tx, ticket['session_id'], self.user)
            ticket = self.store._authorize_ticket(tx, control, actor)
            if (control['ticket_id'] != ticket_id or control['mode'] != 'human'
                    or ticket['status'] not in {'in_progress', 'waiting_customer'}):
                raise SupportError('工单当前不能更新事项')
            revision(tx, self.user, lock=True)
            if not tx.one('SELECT task_id FROM task_handoffs WHERE ticket_id=? AND task_id=? AND user_id=? AND released_at IS NULL',
                          (ticket_id, task_id, self.user)):
                raise SupportError('事项不在本工单接管范围', 404)
            key = 'staff-task:' + actor + ':' + request_id
            previous = tx.one('SELECT payload FROM support_events WHERE session_id=? AND event_key=?',
                              (control['session_id'], key))
            if previous:
                saved = json.loads(previous['payload'])
                if saved['request_hash'] != fingerprint:
                    raise SupportError('更新编号已用于不同内容')
                return TaskRecord.model_validate(saved['after'])
            record = self.repo._record(self.repo._row(tx, task_id, lock=True))
            if record.version != expected_version:
                from app.tasks.repository import VersionConflict
                raise VersionConflict()
            updated = dict(record.details)
            from app.tasks.execution_store import OBJECT_FIELDS
            object_changed = any(k in (details or {}) and details[k] != updated.get(k) for k in OBJECT_FIELDS)
            if object_changed and tx.one("""SELECT o.operation_id FROM support_operations o
                    JOIN task_operation_proposals p ON p.operation_id=o.operation_id
                    WHERE p.user_id=? AND p.task_id=? AND o.status IN ('running','unknown','succeeded') LIMIT 1""",
                    (self.user, task_id)):
                raise SupportError('该事项已有已开始的业务操作，须保留原对象和回执，不能改成另一笔业务')
            if details and details.get('order_id') and details['order_id'] != updated.get('order_id'):
                for field in (*OBJECT_FIELDS, 'tracking_number', 'after_sale_id', 'invoice_id'):
                    updated.pop(field, None)
            for field, value in (details or {}).items():
                if value is None:
                    updated.pop(field, None)
                else:
                    updated[field] = value
            patch = dict(details=updated, waiting_for=record.waiting_for if record.status == 'waiting_external' else None,
                         status='in_progress' if record.status == 'waiting_user' else record.status)
            if goal is not None:
                patch['goal'] = goal
            payload = dict(task_id=task_id, ticket_id=ticket_id, request_hash=fingerprint,
                actor=actor, recorded_role=self.store._role(tx, actor), patch=patch,
                note='人工记录的事项更新，不是业务成功回执或额外权益授权。')
            seq = self.store._event(tx, control, 'task_staff_update', actor, payload, private=True, key=key)
            after = self.repo.update(task_id, control['session_id'], expected_version=expected_version,
                change_id=key, source=dict(kind='support_event', session_id=control['session_id'],
                                          sequence=seq, task_id=task_id), patch=patch, tx=tx)
            tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
                (json.dumps({**payload, 'after': after.model_dump()}, ensure_ascii=False), control['session_id'], seq))
            return after
