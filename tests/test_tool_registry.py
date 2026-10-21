"""Explicit manager whitelists bound both schemas and dispatch."""
import json

import pytest

from app.tools.manager import ToolManager


@pytest.mark.parametrize('allowed', [set(), {'query_order'}])
def test_manager_does_not_expand_explicit_whitelist(monkeypatch, allowed):
    calls = []
    monkeypatch.setattr('app.tools.manager.local_execute_tool',
                        lambda name, args: calls.append(name) or '{}')
    manager = ToolManager(use_mcp=False, allowed_tools=allowed)
    schemas = manager.filter_tool_definitions({'query_order', 'apply_refund'})
    assert {d['function']['name'] for d in schemas} == allowed
    assert manager.filter_tool_definitions(set()) == []
    assert 'error' in json.loads(manager.execute_tool('apply_refund', {}))
    assert calls == []
    if 'query_order' in allowed:
        manager.execute_tool('query_order', {'order_id': 'ORD-A'})
        assert calls == ['query_order']
