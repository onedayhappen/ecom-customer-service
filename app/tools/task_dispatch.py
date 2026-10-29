"""Default task dispatch: scoped MCP reads, local task/confirmation workflows."""
from app.config.settings import settings
from app.runtime.concurrency import check_execution_lease
from app.support.store import SupportError
from app.tasks.runtime import current_task
from app.tools.catalog import BY_NAME
from app.tools.registry import execute_tool as local_execute_tool
from app.tools.request_context import get_current_user_id


def execute_task_tool(name, arguments):
    check_execution_lease()
    context = current_task()
    if context is None or context.runs.user != get_current_user_id():
        raise SupportError('工具调用缺少可信事项身份', 403)
    context.check()
    if name not in context.allowed_tools:
        raise SupportError('工具不在当前事项允许范围', 403)
    if not settings.mcp_enabled or BY_NAME[name].task_only:
        return local_execute_tool(name, arguments)

    from app.mcp.auth import issue_mcp_user_context
    from app.tools.manager import ToolManager
    allowed = {tool for tool in context.allowed_tools if not BY_NAME[tool].task_only}
    token = issue_mcp_user_context(context.runs.user,
        business_scope=vars(context.runs.candidates.scope), allowed_tools=allowed)
    # A connection belongs to one admitted read, never to a process-wide user.
    # This runs inside the bounded read thread: even after a timeout, that thread
    # owns cleanup and cannot race a task-finally closing its active transport.
    manager = ToolManager(use_mcp=True, mcp_server_url=settings.mcp_server_url,
        allowed_tools=allowed, mcp_service_token=settings.mcp_service_token,
        mcp_user_context_token=token, mcp_timeout=context.call_timeout())
    try:
        context.check()  # Discovery may have outlived the task lease or its input.
        check_execution_lease()
        if name in manager.remote_tool_names:
            context.mcp_remote_tools.add(name)
        return manager.execute_tool(name, arguments, require_remote=name in context.mcp_remote_tools)
    finally:
        manager.close()
