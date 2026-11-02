# 文件说明：校验自然语言答复的格式、证据归属及生成快照。
"""Publication contract for model-authored, evidence-checked short replies."""
from dataclasses import dataclass
from hashlib import sha256
import json

UNAVAILABLE_REPLY = "暂时无法核实，请稍后重试。"


def fingerprint(context):
    return sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_draft(value, context):
    draft = json.loads(value)
    if not isinstance(draft, dict) or set(draft) not in ({"reply", "claim_ids"}, {"reply", "claim_ids", "question"}):
        raise ValueError("invalid_natural_reply")
    from app.tasks.reply_questions import validate_question
    question = validate_question(draft.get('question'), context)
    text, ids = draft["reply"], draft["claim_ids"]
    if not isinstance(text, str) or (not text.strip() and question is None):
        raise ValueError("invalid_reply_text")
    text = text.strip()
    if text.startswith(("{", "[", "```")):
        raise ValueError("invalid_reply_format")
    if not isinstance(ids, list) or (not ids and question is None) or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("invalid_reply_sources")
    claims = {c["claim_id"]: c for m in context["matters"] for c in m.get("claims", [])}
    if any(i not in claims for i in ids):
        raise ValueError("unknown_reply_source")
    required = {m["task_id"] for m in context["matters"] if m.get("claims")}
    if {claims[i]["task_id"] for i in ids} != required:
        raise ValueError("uncovered_reply_matter")
    for matter in context["matters"]:
        for claim in matter.get("claims", []):
            if claim["task_id"] != matter["task_id"] or claim["evidence_id"] not in matter["evidence_ids"]:
                raise ValueError("unbound_reply_source")
    return dict(draft, reply=text, question=question)


@dataclass(frozen=True)
class VerifiedReply:
    text: str
    context_fingerprint: str
    question: dict | None = None

    def model_dump(self, *, mode="python"):
        """Keep evaluation/observability records JSON serializable."""
        return dict(text=self.text, context_fingerprint=self.context_fingerprint, question=self.question)

    def render(self, context):
        if self.context_fingerprint != fingerprint(context) or (not self.text and self.question is None):
            raise ValueError("stale_or_invalid_natural_reply")
        from app.tasks.reply_questions import validate_question, question_text
        question = validate_question(self.question, context)
        return '\n\n'.join(filter(None, [self.text, question_text(question) if question else '']))
