"""记忆查询工具：让 Agent 在 ReAct 循环中主动查询用户记忆。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.memory.ltm_manager import LongTermMemoryManager

_memory_manager: ContextVar[LongTermMemoryManager | None] = ContextVar(
    "memory_manager", default=None,
)


def set_memory_manager(manager: LongTermMemoryManager) -> None:
    """由 Agent 初始化时调用，注入长期记忆管理器引用。"""
    _memory_manager.set(manager)


def recall_user_memory(query: str) -> dict:
    """按当前 Query 查询相关长期事实。"""
    manager = _memory_manager.get()
    if manager is None or not manager.memory_enabled:
        return {"success": False, "error": "记忆系统未启用"}
    if not query or not query.strip():
        return {"success": False, "error": "query 不能为空，请传入当前用户的问题或需求"}

    facts = manager.retrieve_relevant_long_term_memory(query)

    result: dict = {
        "success": True,
        "query": query,
        "long_term_facts": [
            {"content": f.content, "category": f.category}
            for f in facts
        ],
    }

    return result
