"""工具调用的请求级身份上下文，禁止由模型参数决定当前用户。"""

from contextvars import ContextVar, Token

_current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
_remote_tool_context: ContextVar[dict | None] = ContextVar('remote_tool_context', default=None)


def set_remote_tool_context(context: dict) -> Token:
    return _remote_tool_context.set(context)


def reset_remote_tool_context(token: Token) -> None:
    _remote_tool_context.reset(token)


def get_remote_tool_context() -> dict | None:
    return _remote_tool_context.get()


def set_current_user_id(user_id: str | None) -> Token:
    return _current_user_id.set(user_id)


def reset_current_user_id(token: Token) -> None:
    _current_user_id.reset(token)


def get_current_user_id() -> str | None:
    return _current_user_id.get()
