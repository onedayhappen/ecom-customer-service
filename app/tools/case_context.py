"""User-scoped, versioned conversation drafts; never authoritative business state."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.memory.case_postgres import create_case_manager
from app.tools.request_context import get_current_user_id


class ReadCase(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: str | None = Field(default=None, min_length=1, max_length=100)


from app.tasks.execution_models import ProgressInput


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
    from app.tasks.runtime import current_task
    if current_task() is not None:
        return current_task().read_progress(arguments)
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
    from app.tasks.runtime import current_task
    if current_task() is not None:
        return current_task().save_progress(arguments)
    return failure('请通过已关联事项的执行入口保存进度，旧订单草稿写入已退役', 'retired_path')


TOOL_MAP = {'read_case_memory': read_case_memory, 'save_case_context': save_case_context}
TOOL_DEFINITIONS = [
    {'type': 'function', 'function': {'name': name, 'description': description,
                                     'parameters': model.model_json_schema()}}
    for name, model, description in (
        ('read_case_memory', ReadCase, '查询当前用户跨会话的订单事项、对话草稿版本和历史业务证据，不作为当前状态或执行授权'),
        ('save_case_context', ProgressInput, '只保存当前已关联事项的进度，使用当前版本；不创建订单草稿或提交业务'),
    )
]
