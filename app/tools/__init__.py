"""Tool package exports.

Keep the registry imports lazy.  Data access modules import
``app.tools.request_context`` during database initialisation; eagerly loading
the registry here creates a cycle through the refund tool and the order
service, which prevents the Docker order-init job from starting.
"""

__all__ = ["TOOL_DEFINITIONS", "execute_tool", "ToolManager"]


def __getattr__(name: str):
    if name in {"TOOL_DEFINITIONS", "execute_tool"}:
        from app.tools import registry

        return getattr(registry, name)
    if name == "ToolManager":
        from app.tools.manager import ToolManager

        return ToolManager
    raise AttributeError(name)
