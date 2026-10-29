"""Read-only cross-session context for an already identified task."""
import json

from app.memory.source_policy import safe_context
from app.tasks.provenance import context_event_valid


def task_history(runs, record):
    with runs.store.transaction(read_only=True) as tx:
        runs._scope(tx, record.task_id)
        rows = tx.all("""SELECT e.session_id,e.sequence,e.payload FROM support_task_messages m
            JOIN support_events e ON e.session_id=m.session_id AND e.sequence=m.sequence
            JOIN support_conversations c ON c.session_id=e.session_id
            WHERE m.task_id=? AND c.user_id=? AND e.kind='message'
            AND e.visibility='public' AND e.actor=?
            ORDER BY e.created_at DESC,e.session_id,e.sequence DESC LIMIT 8""",
            (record.task_id, runs.user, runs.user))
        sources = [dict(session_id=r['session_id'], sequence=r['sequence'],
                        text=json.loads(r['payload'])['content']) for r in reversed(rows)
                   if context_event_valid(tx, r['session_id'], r['sequence'])]
        operations = tx.all("""SELECT p.operation_id,p.task_version,o.tool,o.status,o.result
            FROM task_operation_proposals p JOIN support_operations o ON o.operation_id=p.operation_id
            WHERE p.user_id=? AND p.task_id=? ORDER BY p.created_at DESC LIMIT 20""",
            (runs.user, record.task_id))
        from app.tasks.handoffs import handoff_context
        human = handoff_context(tx, runs.user, record.task_id)
        from app.media.context import task_images
        visual = task_images(tx, runs.user, record.task_id)
        summaries = []
        from app.config.settings import settings
        if settings.short_term_memory_enabled:
            from app.session.short_term import _current_summary
            from app.session.context_budget import estimate_text_tokens
            snapshots = tx.all("""SELECT s.session_id,s.payload FROM support_context_snapshots s
                JOIN task_sessions t ON t.session_id=s.session_id
                WHERE t.task_id=? AND s.scope_key='session'
                ORDER BY s.updated_at DESC LIMIT 8""", (record.task_id,))
            seen = {(s['session_id'], s['sequence']) for s in sources}
            for snapshot in snapshots:
                try:
                    items = _current_summary(tx, snapshot['session_id'], json.loads(snapshot['payload'])['items'])
                except (KeyError, TypeError, ValueError):
                    continue
                for item in items:
                    key = (snapshot['session_id'], item['sequence'])
                    if (item['role'] != 'user' or key in seen or not tx.one(
                            'SELECT task_id FROM support_task_messages WHERE task_id=? AND session_id=? AND sequence=?',
                            (record.task_id, *key))):
                        continue
                    original = dict(session_id=key[0], sequence=key[1], text=item['quote'])
                    if estimate_text_tokens(json.dumps(summaries + [original], ensure_ascii=False)) <= settings.short_term_summary_tokens:
                        summaries.append(original)
                        seen.add(key)
    # Confirmation credentials, proposed arguments and internal notes never
    # enter history. The actual operation status survives a new session.
    progress = []
    for row in operations:
        receipt = json.loads(row['result']) if row['result'] else {}
        progress.append(dict(operation_id=row['operation_id'], action=row['tool'],
            status=row['status'], task_version=row['task_version'],
            receipt={key: receipt[key] for key in (
                'success', 'order_id', 'status', 'request_id', 'refund_id', 'invoice_id')
                if key in receipt and isinstance(receipt[key], (str, bool, int, float))}))
    return safe_context(dict(sources=sources, historical_sources=summaries, operations=progress, human_handoff=human,
        visual_context=visual,
        note='事项记录是沟通进度；操作回执只表示当时办理结果。查询最新订单和退款状态后再答复。'
             'prepared 不代表已授权；新会话不恢复确认，不重放已提交操作。'))


class TaskMemory:
    """Optional memory failure leaves authoritative task history available."""
    def __init__(self, store, user):
        self.store, self.user = store, user
        self.manager = None
        self.shown = {}
        self.sources = []

    def watch_sources(self, sources):
        known = {(s['session_id'], s['sequence']): s for s in self.sources}
        for source in sources:
            known.setdefault((source['session_id'], source['sequence']), source)
        self.sources = list(known.values())

    def recall(self, query):
        from app.memory.runtime import build_memory_manager
        from app.memory.recall import recall_user_memory, set_memory_manager, reset_memory_manager
        try:
            if self.manager is None:
                self.manager = build_memory_manager(self.user, store=self.store)
            token = set_memory_manager(self.manager)
            try:
                result = recall_user_memory(query)
            finally:
                reset_memory_manager(token)
            for fact in result.get('long_term_facts', []):
                self.shown[fact['memory_id']] = fact
            return result
        except Exception:
            return dict(success=False, error='历史偏好暂时不可用，请按原话和事项记录继续。')

    def current(self):
        try:
            with self.store.transaction(read_only=True) as tx:
                for source in self.sources:
                    sid, sequence = source['session_id'], source['sequence']
                    row = tx.one("""SELECT e.payload FROM support_events e JOIN support_conversations c
                        ON c.session_id=e.session_id WHERE e.session_id=? AND e.sequence=? AND c.user_id=?
                        AND e.visibility='public' AND e.kind='message' AND e.actor=?""",
                        (sid, sequence, self.user, self.user))
                    if (not row or not context_event_valid(tx, sid, sequence)
                            or safe_context(json.loads(row['payload']).get('content')) != source['text']):
                        return False
            if not self.shown:
                return True
            # Include history so an intentional historical query still works;
            # updated validity windows/version data invalidate an old prompt.
            facts = self.manager.ltm.history_facts()
            valid = {f.memory_id: self.manager.fact_record(f) for f in facts if self.manager._allowed(f)}
            return all(valid.get(mid) == record for mid, record in self.shown.items())
        except Exception:
            return False
