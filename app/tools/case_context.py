"""User-scoped, versioned conversation drafts; never authoritative business state."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.data_plan.taxonomy import CLASSIFICATION_INTENTS
from app.memory.case_postgres import create_case_manager
from app.support.runtime import check_bot, current_scope
from app.tools.request_context import get_current_user_id
from app.tools.order import query_order


class ReadCase(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: str | None = Field(default=None, min_length=1, max_length=100)


class SaveCase(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0, strict=True)
    intent: str
    state: Literal['collecting', 'waiting_user', 'paused', 'resolved']
    summary: str = Field(min_length=1, max_length=1000)
    # Full replacement avoids obsolete company fields surviving a personal switch.
    fields: dict[str, str] = Field(default_factory=dict, max_length=20)


def failure(message, code='validation_error'):
    return {'success': False, 'error_code': code, 'error': message}


def _manager():
    identity = get_current_user_id()
    if not identity:
        raise ValueError('需要已验证的用户身份')
    return create_case_manager(identity)


def _public(case):
    draft = case.conversation_state.get('dialogue_draft')
    return {'case_id': case.case_id, 'order_ids': case.order_ids, 'intent': case.intent, 'status': case.status,
            'draft': draft, 'business_state': case.business_state,
            'updated_at': case.updated_at,
            # Never expose confirmation hashes, tokens, or full operation args.
            'operations': [{'action': r.get('action'), 'status': r.get('status')}
                           for r in case.conversation_state.get('idempotency', {}).values()]}


def read_case_memory(**arguments):
    try:
        q = ReadCase.model_validate(arguments)
        manager = _manager()
    except (ValidationError, ValueError):
        return failure('查询参数或用户身份不合法')
    with manager.transaction():
        cases = [c for c in manager.cases.values() if not q.order_id or q.order_id in c.order_ids]
        cases.sort(key=lambda c: c.updated_at, reverse=True)
        return {'success': True, 'cases': [_public(c) for c in cases[:20]],
                'has_more': len(cases) > 20,
                'note': '草稿是历史用户陈述，不能代替当前订单查询，也不授权任何写操作。'}


def save_case_context(**arguments):
    try:
        q = SaveCase.model_validate(arguments)
        if q.intent not in CLASSIFICATION_INTENTS or any(len(k) > 80 or len(v) > 1000 for k, v in q.fields.items()):
            return failure('意图或草稿字段不合法')
        manager = _manager()
    except (ValidationError, ValueError):
        return failure('草稿参数或用户身份不合法')
    check_bot()
    verified = query_order(q.order_id)
    if isinstance(verified, str):
        verified = json.loads(verified)
    if not verified.get('success') or (verified.get('order') or {}).get('order_id') != q.order_id:
        return failure('无法核实当前用户的订单，不保存草稿', 'evidence_unavailable')
    with manager.transaction():
        # A quiet conversation may have auto-closed; resume its version instead
        # of creating a second draft or permanently rejecting the read version.
        candidates = [c for c in manager.cases.values()
                      if c.intent == 'conversation_context' and q.order_id in c.order_ids
                      and c.status != 'archived']
        case = max(candidates, key=lambda c: c.updated_at) if candidates else None
        previous = (case.conversation_state.get('dialogue_draft') or {}) if case else {}
        if previous.get('version', 0) != q.expected_version:
            return failure('另一会话已更新该订单草稿，请重读后结合最新信息处理', 'version_conflict')
        check_bot()
        case = case or manager._create_case('conversation_context', q.order_id)
        case.status, case.expires_at = 'open', ''
        draft = {'version': q.expected_version + 1, 'intent': q.intent, 'state': q.state,
                 'summary': q.summary, 'fields': q.fields, 'source': 'conversation_draft'}
        case.conversation_state['dialogue_draft'] = draft
        manager._touch(case)
        # A changed draft invalidates outstanding proposals for this object in
        # every session, without touching committed/unknown operation records.
        for related in manager.cases.values():
            if q.order_id in related.order_ids:
                related.conversation_state.pop('pending_mutation', None)
        scope = current_scope()
        manager.record_audit('conversation_draft_saved', scope[1] if scope else '', case.case_id,
                             {'version': draft['version'], 'intent': q.intent, 'state': q.state})
        return {'success': True, 'case': _public(case),
                'note': '仅保存对话草稿；未执行或撤销任何业务操作。'}


TOOL_MAP = {'read_case_memory': read_case_memory, 'save_case_context': save_case_context}
TOOL_DEFINITIONS = [
    {'type': 'function', 'function': {'name': name, 'description': description,
                                     'parameters': model.model_json_schema()}}
    for name, model, description in (
        ('read_case_memory', ReadCase, '查询当前用户跨会话的订单事项、对话草稿版本和历史业务证据，不作为当前状态或执行授权'),
        ('save_case_context', SaveCase, '保存已核实订单的当前意图与完整对话草稿；使用读取到的 expected_version，首次为 0。不是业务写入或业务撤销；resolved 只代表对话事项结束'),
    )
]
