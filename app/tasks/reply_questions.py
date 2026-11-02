# 文件说明：从可信回执投影澄清候选，校验回复模型的问题与引用。
"""Question wording is model-authored; candidates and publication are server-owned."""
import re

from pydantic import Field, model_validator

from app.tasks.models import StrictModel


class Option(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    ref: str | None = Field(default=None, min_length=1, max_length=1000)


class ReplyQuestion(StrictModel):
    task_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=2000)
    options: list[Option] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def valid(self):
        if not self.prompt.strip() or any(not o.label.strip() for o in self.options):
            raise ValueError('empty_question')
        if len(self.options) == 1 or len({o.label for o in self.options}) != len(self.options):
            raise ValueError('invalid_question_options')
        if re.search(r'密码|验证码|银行卡号|密钥|\btoken\b|api.?key', self.prompt, re.I):
            raise ValueError('question_requests_credentials')
        return self


def clarification_material(receipts, handoff):
    """Historical lookup receipts provide identity only, never live statuses."""
    fields = {'order_id', 'platform_code', 'name', 'title', 'title_snapshot', 'created_at',
              'paid_amount', 'currency', 'quantity', 'specs', 'spec_snapshot', 'purchase_summary',
              'item_count', 'matching_item_count', 'items_truncated', 'order_item_id',
              'product_name', 'sku_name', 'sku_id', 'attributes', 'spec_values', 'label', 'code'}
    candidates = {}

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key in ('order_ref', 'product_ref', 'sku_ref', 'shipment_ref', 'after_sale_ref',
                        'coupon_ref', 'address_ref', 'invoice_ref', 'logistics_ref', 'subject_ref', 'ref'):
                ref = value.get(key)
                if isinstance(ref, str) and ref:
                    candidates[ref] = dict(ref=ref, **{k: v for k, v in value.items() if k in fields})
            for key, item in value.items():
                if key not in {'applied_conditions', 'arguments', 'items_query'}:
                    visit(item)

    for receipt in receipts:
        visit(receipt['result'].get('data', {}))
    refs = handoff.get('candidate_refs', [])
    if any(ref not in candidates for ref in refs):
        raise ValueError('unknown_clarification_candidate')
    return dict(missing_information=handoff['missing_information'],
                candidates=[candidates[ref] for ref in refs])


def validate_question(value, context):
    pending = {m['task_id']: m['clarification'] for m in context['matters'] if m.get('clarification')}
    if value is None:
        if pending:
            raise ValueError('missing_reply_question')
        return None
    question = ReplyQuestion.model_validate(value)
    if question.task_id not in pending:
        raise ValueError('unexpected_reply_question')
    refs = {c['ref'] for c in pending[question.task_id]['candidates']}
    selected = [o.ref for o in question.options if o.ref is not None]
    if any(ref not in refs for ref in selected) or len(selected) != len(set(selected)):
        raise ValueError('unknown_or_duplicate_question_reference')
    # Candidate choices must retain their binding. Free-form clarification can
    # use no options, or unbound attribute options when there are no candidates.
    if refs and question.options and any(o.ref is None for o in question.options):
        raise ValueError('unbound_question_option')
    return question.model_dump()


def question_text(question):
    text = question['prompt']
    if question['options']:
        text += '\n' + '；'.join(f"{i + 1}. {o['label']}" for i, o in enumerate(question['options']))
    return text
