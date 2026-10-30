# 文件说明：验证派生记忆对应的原始消息归属、公开范围及来源有效性。
"""Validate derived memory against owned, still visible original messages.

The memory store and vector/cache layers are hints. This check always reads the
support database; a cached fact never grants access to a missing source.
"""
from hashlib import sha256
import json
import re

_PRIVATE = re.compile(
    r"密码|口令|验证码|令牌|密钥|私钥|免确认|无需[^，。；！？,;!?\n]{0,8}确认|不用[^，。；！？,;!?\n]{0,8}确认|"
    r"跳过.{0,8}确认|永久.{0,8}授权|以后.{0,8}直接.{0,8}(?:退款|支付|提交)|"
    r"password|passwd|api[_ -]?key|access[_ -]?token|\b(?:token|secret)\s*[:=]|bearer\s+|"
    r"private[_ -]?key|skip.{0,12}confirmation|without.{0,12}confirm|"
    r"sk-[A-Za-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY-----",
    re.IGNORECASE,
)
OMITTED = "[该内容含敏感信息或长期授权要求，未放入模型记忆，请补充不含这些信息的业务描述。]"

POLICY_REFRESH = '此前查询过规则；继续处理时须重新检索适用条款，历史回复不能作为当前政策依据。'


def message_content(payload):
    """Preserve visible history while withholding cached policy conclusions."""
    if payload.get('role') == 'assistant' and 'context_content' in payload:
        value = payload['context_content']
        return value if isinstance(value, str) else POLICY_REFRESH
    return payload.get('content', '')


def memory_safe(value):
    return isinstance(value, str) and not _PRIVATE.search(value)


def safe_context(value):
    """Omit a whole string rather than splice away a condition or negation."""
    if isinstance(value, str):
        return value if memory_safe(value) else OMITTED
    if isinstance(value, dict):
        return {k: OMITTED if str(k).lower() in {
            'password', 'token', 'token_hash', 'confirmation_token', 'api_key', 'private_notes', 'secret'
        } else safe_context(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_context(v) for v in value]
    return value


class SourcePolicy:
    def __init__(self, store, user):
        self.store, self.user = store, user

    def source(self, tx, sid, sequence):
        from app.tasks.provenance import context_event_valid, invalid
        row = tx.one("""SELECT e.payload,e.actor FROM support_events e
            JOIN support_conversations c ON c.session_id=e.session_id
            WHERE e.session_id=? AND e.sequence=? AND c.user_id=?
            AND e.kind='message' AND e.visibility='public'""", (sid, sequence, self.user))
        if not row or row['actor'] != self.user or not context_event_valid(tx, sid, sequence):
            return None
        payload = json.loads(row['payload'])
        content = payload.get('content')
        if payload.get('role') != 'user' or not memory_safe(content):
            return None
        decisions = [r['decision_id'] for r in tx.all("""SELECT d.decision_id
            FROM association_sources s JOIN association_decisions d ON d.decision_id=s.decision_id
            WHERE s.session_id=? AND s.sequence=? AND d.user_id=?""", (sid, sequence, self.user))]
        if any(invalid(tx, self.user, 'decision', did) for did in decisions):
            return None
        return dict(content=content, proof=dict(user_id=self.user, session_id=sid,
            sequence=sequence, digest=sha256(content.encode()).hexdigest(), decisions=sorted(decisions)))

    def allows(self, fact, *, tx=None):
        if fact.metadata.get('reset') or not memory_safe(fact.content) or not memory_safe(fact.source_span):
            return False
        sid, separator, sequence = fact.source_event_id.rpartition(':')
        if not separator or sid != fact.source_session or not sequence.isdecimal():
            return False
        if tx is None:
            with self.store.transaction(read_only=True) as current:
                return self.allows(fact, tx=current)
        source = self.source(tx, sid, int(sequence))
        if not source or not fact.source_span or fact.source_span not in source['content']:
            return False
        proof = fact.metadata.get('source_proof')
        # Older facts have to pass the same source check; new facts additionally
        # bind the original content, even if somebody edits the event in place.
        if proof is not None:
            return (isinstance(proof, dict) and proof.get('user_id') == self.user
                and proof.get('session_id') == sid and proof.get('sequence') == int(sequence)
                and proof.get('digest') == source['proof']['digest']
                and isinstance(proof.get('decisions'), list)
                and all(did in source['proof']['decisions'] for did in proof['decisions']))
        return True
