"""Discovery is independent of inferred intent; each invocation enforces ownership."""
from app.tools.catalog import TOOLS

def for_task(intent=None, goal=""):
    return frozenset(t.name for t in TOOLS)
