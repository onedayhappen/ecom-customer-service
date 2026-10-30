# 文件说明：提供记忆模块的延迟导出入口，组织长期事实、检索和持久处理。
"""Agent 记忆模块的公共入口。

本包负责跨会话可复用的长期记忆，包括：
- 结构化事实（用户画像、稳定偏好、可复用业务事实）
- Memory Item 存储、检索、准入校验与合并压缩

会话内的短期上下文不在这里维护；它仍由编排器持有的原始消息、会话摘要和
当前事项状态（由 app.tasks 管理）负责。这样可以避免把“刚刚说过的话”和“值得长期保存的知识”
混在同一层里。

这里使用延迟导入（``__getattr__``），主要是为了降低包级导入的副作用：
调用方只想引用 MemoryItem/MemoryValidator 时，不需要提前初始化 OpenAI client、
配置项或更重的存储依赖。
"""

__all__ = [
    "LongTermMemoryManager", "LongTermMemory", "MemoryFact",
    "MemoryItem", "MemoryValidator",
]


def __getattr__(name: str):
    # 延迟导出 facade，避免 import app.memory 时立刻加载所有子模块。
    if name == "LongTermMemoryManager":
        from app.memory.ltm_manager import LongTermMemoryManager

        return LongTermMemoryManager
    if name in {"LongTermMemory", "MemoryFact"}:
        from app.memory.long_term import LongTermMemory, MemoryFact

        return {
            "LongTermMemory": LongTermMemory,
            "MemoryFact": MemoryFact,
        }[name]
    if name == "MemoryItem":
        from app.memory.items import MemoryItem

        return MemoryItem
    if name == "MemoryValidator":
        from app.memory.validator import MemoryValidator

        return MemoryValidator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
