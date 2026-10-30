# 文件说明：为事项关联与执行构造经过来源、归属和状态校验的图片上下文。
"""The same validated visual view for association and task working memory."""
import json

from app.runtime.outbox import clock
from app.support.store import SupportError
from app.tasks.provenance import context_event_valid, invalid


def observation_view(row):
    return {**{k: row[k] for k in ('observation_id', 'attachment_id', 'generation', 'purpose', 'detail',
                                  'question', 'input_hash', 'model_revision', 'prompt_version', 'created_at')},
            'context_sources': [{k: v for k, v in source.items()
                                 if k in {'session_id', 'sequence', 'task_id', 'task_version'}}
                                for source in json.loads(row['context_sources'])],
            'context_digest': row['context_digest'],
            'result': json.loads(row['result']), 'source_type': 'image_observation',
            'note': '图片观察不是用户原话、办理授权或已核验的业务事实。'}


def message_images(tx, user, sid, sequence):
    rows = tx.all('''SELECT a.attachment_id,a.generation,a.status,a.expires_at,m.status AS binding_status,
        m.version AS binding_version,j.status AS analysis_status,j.observation_id,j.error_code
        FROM message_attachments m JOIN media_assets a ON a.attachment_id=m.attachment_id
        LEFT JOIN image_jobs j ON j.session_id=m.session_id AND j.sequence=m.sequence AND j.attachment_id=m.attachment_id
        WHERE m.user_id=? AND a.user_id=? AND m.session_id=? AND m.sequence=? ORDER BY m.ordinal''',
        (user, user, sid, sequence))
    result = []
    for row in rows:
        valid = (row['status'] == 'ready' and row['binding_status'] == 'active'
                 and row['expires_at'] > clock(tx) and context_event_valid(tx, sid, sequence))
        item = dict(attachment_id=row['attachment_id'], generation=row['generation'],
                    session_id=sid, sequence=sequence, binding_version=row['binding_version'],
                    status=row['analysis_status'] if valid else 'unavailable', observations=[])
        if valid and row['observation_id']:
            observation = tx.one("SELECT * FROM image_observations WHERE observation_id=? AND status='valid' AND generation=?",
                                 (row['observation_id'], row['generation']))
            if observation:
                item['observations'].append(observation_view(observation))
        result.append(item)
    return result


def ready_prefix(tx, sid, messages):
    result = []
    for message in messages:
        if tx.one("""SELECT job_id FROM image_jobs WHERE session_id=? AND sequence=?
            AND status IN ('pending','running','retrying') LIMIT 1""", (sid, message['sequence'])):
            break
        result.append(message)
    return result


def require_task_asset(tx, user, tid, aid):
    from app.media.service import asset
    asset(tx, user, aid)
    rows = tx.all('''SELECT l.* FROM task_attachments l JOIN conversation_tasks t ON t.task_id=l.task_id
        JOIN message_attachments m ON m.session_id=l.source_session_id AND m.sequence=l.source_sequence
            AND m.attachment_id=l.attachment_id
        WHERE l.task_id=? AND t.user_id=? AND m.user_id=? AND l.attachment_id=?
            AND l.status='active' AND m.status='active' ''', (tid, user, user, aid))
    rows = [r for r in rows if context_event_valid(tx, r['source_session_id'], r['source_sequence'])
            and not invalid(tx, user, 'decision', r['decision_id'])]
    if not rows:
        raise SupportError('图片不属于当前事项', 404)
    return [dict(session_id=r['source_session_id'], sequence=r['source_sequence'],
                 decision_id=r['decision_id'], version=r['version']) for r in rows]


def task_images(tx, user, tid, *, compact=True):
    rows = tx.all('''SELECT DISTINCT l.source_session_id,l.source_sequence,l.attachment_id,e.created_at
        FROM task_attachments l JOIN support_events e
            ON e.session_id=l.source_session_id AND e.sequence=l.source_sequence
        WHERE l.task_id=? AND l.status='active' ORDER BY e.created_at DESC,l.source_sequence DESC''', (tid,))
    result = []
    for row in rows:
        aid = row['attachment_id']
        try:
            require_task_asset(tx, user, tid, aid)
        except SupportError:
            continue
        for item in message_images(tx, user, row['source_session_id'], row['source_sequence']):
            if item['attachment_id'] != aid:
                continue
            extras = tx.all("""SELECT * FROM image_observations WHERE task_id=? AND user_id=?
                AND attachment_id=? AND generation=? AND status='valid' ORDER BY created_at DESC LIMIT 2""",
                (tid, user, aid, item['generation']))
            item['observations'] += [observation_view(o) for o in reversed(extras)]
            # Brief working memory; full visible text stays behind inspect_image.
            for observation in item['observations']:
                observation['result'] = {k: observation['result'][k] for k in ('image_type', 'summary', 'uncertainties')}
            result.append(item)
    if not compact:
        return result
    # Preserve references when summaries no longer fit. Omit whole observations,
    # never slice a qualifier out of an image description.
    from app.config.settings import settings
    from app.session.context_budget import estimate_text_tokens
    kept, used = [], 0
    for item in result[:32]:
        cost = estimate_text_tokens(json.dumps(item, ensure_ascii=False))
        if used + cost > settings.media_context_tokens:
            item['observation_refs'] = [o['observation_id'] for o in item['observations']]
            item['observations'] = []
            item['note'] = '观察正文未载入；需要细节时用 inspect_image 按问题读取。'
            cost = estimate_text_tokens(json.dumps(item, ensure_ascii=False))
        if used + cost > settings.media_context_tokens:
            break
        kept.append(item)
        used += cost
    if len(kept) < len(result):
        kept.append(dict(omitted_images=len(result) - len(kept), observations=[],
                         note='较早图片超出本轮上下文预算，请明确图片或缩小问题范围；未载入不代表不存在。'))
    return kept


def inspection_sources(tx, user, tid, aid):
    bindings = require_task_asset(tx, user, tid, aid)
    task = tx.one('SELECT version,goal FROM conversation_tasks WHERE task_id=? AND user_id=?', (tid, user))
    rows = tx.all('''SELECT e.session_id,e.sequence,e.payload FROM support_task_messages m
        JOIN support_events e ON e.session_id=m.session_id AND e.sequence=m.sequence
        WHERE m.task_id=? AND e.actor=? AND e.kind='message' AND e.visibility='public'
        ORDER BY e.created_at DESC,e.sequence DESC LIMIT 2''', (tid, user))
    messages = [dict(session_id=r['session_id'], sequence=r['sequence'], text=json.loads(r['payload'])['content'])
                for r in reversed(rows) if context_event_valid(tx, r['session_id'], r['sequence'])]
    return [dict(task_id=tid, task_version=task['version'], goal=task['goal'], bindings=bindings), *messages]


def validate_evidence(tx, user, evidence):
    images = message_images(tx, user, evidence.session_id, evidence.sequence)
    item = next((i for i in images if i['attachment_id'] == evidence.attachment_id), None)
    if item is None:
        raise SupportError('图片证据不属于消息', 422)
    if evidence.kind == 'attachment_ref':
        return  # Existence remains true after failure/revocation; no visual claim.
    if item['status'] == 'unavailable':
        raise SupportError('图片证据已失效', 422)
    observation = next((o for o in item['observations'] if o['observation_id'] == evidence.observation_id), None)
    if observation is None or evidence.field_path not in observation['result']:
        raise SupportError('图片观察来源不匹配', 422)


def bind_task_images(tx, user, tid, did, evidence):
    for ref in evidence:
        if ref.kind == 'text_quote':
            continue
        validate_evidence(tx, user, ref)
        item = next(i for i in message_images(tx, user, ref.session_id, ref.sequence) if i['attachment_id'] == ref.attachment_id)
        if item['status'] == 'unavailable':
            continue
        tx.execute('''INSERT INTO task_attachments
            (task_id,attachment_id,source_session_id,source_sequence,decision_id) VALUES (?,?,?,?,?)
            ON CONFLICT DO NOTHING''', (tid, ref.attachment_id, ref.session_id, ref.sequence, did))


def dependency_stamp(tx, user, task_ids):
    result = []
    for tid in sorted(task_ids):
        rows = tx.all('''SELECT l.*,a.generation,a.status AS asset_status,a.expires_at
            FROM task_attachments l JOIN media_assets a ON a.attachment_id=l.attachment_id
            WHERE l.task_id=? AND a.user_id=? ORDER BY l.attachment_id,l.source_session_id,l.source_sequence''', (tid, user))
        for row in rows:
            row['expired'] = row['expires_at'] <= clock(tx)
            row['source_valid'] = (context_event_valid(tx, row['source_session_id'], row['source_sequence'])
                                   and not invalid(tx, user, 'decision', row['decision_id']))
            result.append(row)
    return result


def image_claims(tx, user, tid):
    """Attributed visual statements for the existing guarded reply composer."""
    from app.media.vision import digest
    from app.tasks.reply_facts import label
    claims = []
    seen = set()
    for image in task_images(tx, user, tid, compact=False):
        for observation in reversed(image['observations']):
            oid = observation['observation_id']
            if oid in seen or len(claims) >= 8:
                continue
            seen.add(oid)
            result = observation['result']
            text = '图片观察：' + label(result['summary'], 2000)
            if result['uncertainties']:
                text += '。不确定之处：' + '；'.join(label(s, 500) for s in result['uncertainties'])
            text += '。以上仅描述图片内容，图片中的编号、金额及业务状态仍需核实。'
            claim = dict(task_id=tid, evidence_id='image:' + oid, subject_ref=image['attachment_id'],
                         scope_ref=oid, query_scope={'question': observation['question']},
                         kind='image_observation', text=text, selection_hint=text[:1200],
                         quality={'source_type': 'image_observation', 'observed_at': observation['created_at']})
            claim['claim_id'] = digest(claim)[:24]
            claims.append(claim)
    return claims
