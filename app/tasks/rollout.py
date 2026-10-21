"""Database-owned rollout controls and conservative historical task imports.

No business write is replayed here. Administrative transitions hold the exclusive
counterpart of SupportStore.transaction's shared deployment lock.
"""
import json
from uuid import uuid4

from app.runtime.outbox import clock
from app.support.store import SupportError, now
from app.tasks.migrations import json_value
from app.tasks.repository import TaskRepository

DEPLOYMENT_LOCK = 82344720
RELEASE = 'task-pipeline-v1'


def running(tx, user):
    state = tx.one("SELECT * FROM task_pipeline_state WHERE singleton=1")
    if not state or state['release'] != RELEASE or state['mode'] == 'stopped':
        return False
    selected = tx.one('SELECT enabled FROM task_pipeline_users WHERE user_id=?', (user,))
    return bool(selected['enabled']) if selected else state['mode'] == 'all'


def require_running(tx, user):
    if not running(tx, user):
        raise SupportError('自动办理已暂停，历史记录和人工服务仍可使用', 503)


class Rollout:
    def __init__(self, store):
        self.store = store

    def status(self):
        with self.store.transaction(read_only=True) as tx:
            return dict(state=tx.one('SELECT * FROM task_pipeline_state WHERE singleton=1'),
                        users=tx.all('SELECT * FROM task_pipeline_users ORDER BY user_id'),
                        unresolved_operations=tx.one("SELECT COUNT(*) AS n FROM support_operations WHERE status IN ('running','unknown')")['n'],
                        legacy_jobs=tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE kind='conversation' AND status IN ('pending','dispatched','running')")['n'])

    def set_mode(self, mode, *, user=None, actor='operator', reason):
        if not reason or len(reason) > 500 or not actor or len(actor) > 200:
            raise ValueError('An operator and a short reason are required')
        if mode not in ({'enabled', 'disabled'} if user else {'all', 'selected', 'stopped'}):
            raise ValueError('Invalid rollout mode')
        self.store.initialize()
        # Do not upgrade a shared lock: two operators could otherwise deadlock.
        with self.store._connection() as tx:
            if tx.postgres:
                tx.execute('SELECT pg_advisory_xact_lock(?)', (DEPLOYMENT_LOCK,))
            state = tx.one('SELECT * FROM task_pipeline_state WHERE singleton=1')
            if user:
                tx.execute('INSERT INTO task_pipeline_users VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled',
                           (user, int(mode == 'enabled')))
            else:
                tx.execute('UPDATE task_pipeline_state SET mode=? WHERE singleton=1', (mode,))
            tx.execute('UPDATE task_pipeline_state SET version=version+1,updated_at=? WHERE singleton=1', (now(),))
            users = [user] if user else [r['user_id'] for r in tx.all('SELECT DISTINCT user_id FROM support_conversations ORDER BY user_id')]
            for uid in users:
                self._fence(tx, uid, actor, reason)
            tx.execute('INSERT INTO task_rollout_audit VALUES (?,?,?,?,?,?,?)',
                       (str(uuid4()), now(), actor, user, mode, reason, state['version'] + 1))
        return self.status()

    def _fence(self, tx, user, actor, reason):
        # Preserve accepted input and every business receipt. Advancing the
        # control versions also invalidates confirmations and late tool answers.
        sessions = tx.all('SELECT * FROM support_conversations WHERE user_id=? ORDER BY session_id', (user,))
        for control in sessions:
            tx.execute('UPDATE support_conversations SET control_version=control_version+1,bot_job_id=NULL WHERE session_id=?',
                       (control['session_id'],))
            tx.execute('UPDATE message_intake_sessions SET active_job_id=NULL,lease_owner=NULL WHERE session_id=?',
                       (control['session_id'],))
            self.store._event(tx, control, 'pipeline_control', actor,
                              dict(reason=reason, automatic_processing=running(tx, user)), private=True)
        tx.execute("UPDATE runtime_jobs SET status='superseded',finished_at=?,delivery_owner=NULL,delivery_lease=0 WHERE user_id=? AND status IN ('pending','dispatched','running')",
                   (clock(tx), user))
        tx.execute('UPDATE task_external_waits SET lease_owner=NULL,lease_until=NULL WHERE user_id=?', (user,))
        tx.execute('UPDATE task_execution_wakes SET version=0,job_id=NULL,retry_at=0 WHERE task_id IN (SELECT task_id FROM conversation_tasks WHERE user_id=?)', (user,))

    def import_legacy(self, task_id, *, user, expected_version, actor, reason):
        """Copy an exact original request into a PAUSED matter, never execute it.

        Orders/slots inferred by old rewrite code are deliberately not copied.
        Imported tasks retain their original user message and the old record.
        """
        if not actor or not reason:
            raise ValueError('An operator and migration reason are required')
        repo = TaskRepository(self.store, user)
        with self.store.transaction() as tx:
            if running(tx, user):
                raise SupportError('先暂停此用户的自动办理，再迁移历史事项')
            old = repo._row(tx, task_id, lock=True)
            prior = tx.one('SELECT * FROM task_legacy_imports WHERE legacy_task_id=?', (task_id,))
            if prior:
                if prior['user_id'] != user or prior['legacy_version'] != expected_version:
                    raise SupportError('迁移编号或历史版本不匹配')
                return repo._record(repo._row(tx, prior['task_id']))
            if old['storage_mode'] != 'legacy' or old['version'] != expected_version:
                raise SupportError('历史事项版本已改变或已经采用新格式')
            if old['status'] in {'completed', 'cancelled'}:
                raise SupportError('已结束的历史事项保留查询，不重新办理')
            sources = tx.all("""SELECT e.session_id,e.sequence,e.payload FROM support_task_messages m
                JOIN support_events e ON e.session_id=m.session_id AND e.sequence=m.sequence
                JOIN support_conversations c ON c.session_id=e.session_id
                WHERE m.task_id=? AND c.user_id=? AND e.actor=? AND e.kind='message' AND e.visibility='public'
                ORDER BY e.created_at,e.sequence""", (task_id, user, user))
            # Multiple turns can contain corrections and hidden assumptions.
            # They remain historical reference until a human/user restates them.
            if len(sources) != 1:
                raise SupportError('历史来源不唯一，请保留参考并由用户重新说明')
            source = sources[0]
            payload = json_value(source['payload'], {})
            goal = old['request_text']
            from app.tasks.provenance import context_event_valid
            if (payload.get('role') != 'user' or payload.get('content') != goal or not goal
                    or not context_event_valid(tx, source['session_id'], source['sequence'])):
                raise SupportError('历史目标不是完整有效的用户原话，不能自动转换')
            if tx.one("""SELECT o.operation_id FROM support_operations o JOIN support_conversations c
                ON c.session_id=o.session_id WHERE c.user_id=? AND o.status IN ('running','unknown') LIMIT 1""", (user,)):
                raise SupportError('存在待核实的业务操作，先核实回执')
            record = repo.create(source['session_id'], change_id='legacy-import:' + task_id,
                source=dict(kind='message', session_id=source['session_id'], sequence=source['sequence']),
                goal=goal, details={}, status='paused', tx=tx)
            tx.execute('INSERT INTO task_legacy_imports VALUES (?,?,?,?,?,?,?)',
                       (task_id, expected_version, user, record.task_id, actor, reason, now()))
            return record
