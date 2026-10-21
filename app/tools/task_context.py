"""Read and update only the task currently owned by the execution harness."""
from pydantic import BaseModel, ConfigDict, Field

from app.tasks.execution_models import ProgressInput


class ReadTaskContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: str | None = Field(default=None, min_length=1, max_length=100)


def _current():
    from app.tasks.runtime import current_task
    return current_task()


def _missing():
    return {'success': False, 'error_code': 'validation_error', 'error': '请通过已关联事项的执行入口访问进度'}


def read_task_context(**arguments):
    context = _current()
    return context.read_progress(arguments) if context is not None else _missing()


def save_task_progress(**arguments):
    context = _current()
    return context.save_progress(arguments) if context is not None else _missing()


TOOL_MAP = {'read_task_context': read_task_context, 'save_task_progress': save_task_progress}
TOOL_DEFINITIONS = [
    {'type': 'function', 'function': {'name': name, 'description': description,
                                    'parameters': model.model_json_schema()}}
    for name, model, description in (
        ('read_task_context', ReadTaskContext, '读取当前事项的进度、相关原话和历史回执；历史记录不代表当前业务状态或授权'),
        ('save_task_progress', ProgressInput, '按当前版本保存事项进度，不改变订单对象或提交业务操作'),
    )
]
