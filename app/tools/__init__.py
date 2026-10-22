"""Lazy exports for tool infrastructure; the core registry is defined in catalog.py."""
__all__ = ["TOOL_DEFINITIONS", "execute_tool", "ToolManager"]


def __getattr__(name: str):
    if name in {"TOOL_DEFINITIONS", "execute_tool"}:
        from app.tools import registry

        return getattr(registry, name)
    if name == "ToolManager":
        from app.tools.manager import ToolManager

        return ToolManager
    raise AttributeError(name)
