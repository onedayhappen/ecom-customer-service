"""Request-scoped access to the authenticated user's long-term facts."""
from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

from app.tools.request_context import get_current_user_id

if TYPE_CHECKING:
    from app.memory.ltm_manager import LongTermMemoryManager

logger = logging.getLogger(__name__)
_memory_manager: ContextVar[LongTermMemoryManager | None] = ContextVar("memory_manager", default=None)


def set_memory_manager(manager: LongTermMemoryManager | None) -> Token:
    return _memory_manager.set(manager)


def reset_memory_manager(token: Token) -> None:
    _memory_manager.reset(token)


def _authenticated_manager():
    manager = _memory_manager.get()
    user_id = get_current_user_id()
    if (manager is None or not manager.memory_enabled or not user_id
            or manager.ltm.user_id != user_id):
        return None
    return manager


def memory_prompt_sections(
    query: str, *, context: list[dict] | None = None, session_id: str | None = None,
) -> list[dict]:
    manager = _authenticated_manager()
    if manager is None:
        return []
    try:
        return manager.build_memory_prompt_sections(query, context=context, session_id=session_id)
    except Exception as exc:
        logger.warning("Long-term memory retrieval unavailable: %s", type(exc).__name__)
        return []


def sync_memory_prompt_visibility(messages: list[dict]) -> None:
    """Sync deduplication with the messages actually retained by final budgeting."""
    manager = _authenticated_manager()
    if manager is not None:
        manager.set_injected_ids_from_messages(messages)


def recall_user_memory(query: str) -> dict:
    """Recall only facts belonging to the authenticated request user."""
    manager = _authenticated_manager()
    if manager is None:
        return {"success": False, "error": "记忆系统未启用或当前身份不可用"}
    if not query or not query.strip():
        return {"success": False, "error": "query 不能为空，请传入当前用户的问题或需求"}
    try:
        facts = manager.retrieve_relevant_long_term_memory(query, exclude_ids=manager.injected_ids)
    except Exception as exc:
        logger.warning("Long-term memory tool unavailable: %s", type(exc).__name__)
        return {"success": False, "error": "历史偏好暂时不可用，请按本次用户要求继续处理"}
    return {
        "success": True,
        "query": query,
        "note": "用户历史陈述可能过时；当前要求优先，不作为订单状态、操作授权或默认订单的依据。",
        "already_in_context": sorted(manager.injected_ids),
        "context_note": "上下文已有的用户原话或长期事实不重复返回，请参照已保留的上下文。" if manager.injected_ids else "",
        "long_term_facts": [manager.fact_record(fact) for fact in facts],
    }
