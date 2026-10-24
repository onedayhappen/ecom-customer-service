"""Explicit registry for the 48 customer-service tools."""
import importlib
from app.tools.catalog import TOOLS
from app.services.commerce.common import BusinessError,dumps,failure
from app.support.runtime import check_bot

_TOOL_MAP={t.name:getattr(importlib.import_module('app.tools.'+t.domain+'_tools'),t.name) for t in TOOLS}
TOOL_DEFINITIONS=[t.definition() for t in TOOLS]


def execute_tool(name: str, arguments: dict) -> str:
    function=_TOOL_MAP.get(name)
    if function is None:
        return dumps(failure(BusinessError('invalid_arguments','未知工具')))
    try:
        check_bot()
        if not isinstance(arguments,dict): raise BusinessError('invalid_arguments','参数必须为对象')
        result=function(**arguments)
    except BusinessError as error:
        result=failure(error)
    except Exception as error:
        from app.support.store import SupportError
        if isinstance(error,SupportError):
            result=failure(BusinessError('state_changed','事项状态已变化，无法继续执行'))
        else:
            # Never expose connection URLs, SQL, keys or raw exception inputs.
            result=failure(BusinessError('temporarily_unavailable','工具暂时不可用，请稍后核实'))
    return dumps(result)
