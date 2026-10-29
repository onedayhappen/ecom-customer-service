"""The new catalog never restores retired tools through local or remote entry points."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.data_plan.capabilities import CAPABILITIES, IMPLEMENTED_CAPABILITIES
from app.execution.business_operations import BusinessOperations
from app.execution.task_proposals import PreparationRejected
from app.tasks.external_evidence import BusinessProgress, ExternalEvidenceError
from app.tools.manager import ToolManager
from app.tools.registry import TOOL_DEFINITIONS, _TOOL_MAP, execute_tool
from tests.test_task_candidates import task
from tests.test_message_association import store
from tests.test_task_storage import empty_store
from tests.test_task_execution import Client, engine, request, response, local_budgets


def test_local_and_mcp_catalogues_match_supported_boundaries():
    from app.mcp.server import list_business_tools
    assert len(TOOL_DEFINITIONS) == len(_TOOL_MAP) == 50
    assert len(CAPABILITIES) == len(IMPLEMENTED_CAPABILITIES) == 50
    assert len(asyncio.run(list_business_tools())) == 31


@pytest.mark.parametrize('name', [
    'query_order', 'query_product', 'query_logistics', 'list_user_orders',
    'apply_refund', 'issue_invoice', 'query_user_coupons', 'calc_promotion',
    'search_knowledge', 'load_skill', 'recall_user_memory',
    'save_task_progress',
    'prepare_operation', 'escalate_to_human',
])
def test_retired_tools_are_rejected_by_both_local_dispatchers(name):
    manager = ToolManager(allowed_tools={name})
    assert manager.tool_definitions == [] and manager.tool_names == set()
    for result in (execute_tool(name, {}), manager.execute_tool(name, {})):
        assert json.loads(result)['success'] is False


def test_old_remote_server_cannot_restore_deleted_tools(monkeypatch):
    class OldServer:
        def __init__(self, *args):
            pass

        def connect(self):
            return [{'type': 'function', 'function': {'name': 'query_order'}}]

        def call_tool(self, *args):
            pytest.fail('old remote tool was invoked')

        def close(self):
            pass

    monkeypatch.setattr('app.mcp.MCPClient', OldServer)
    manager = ToolManager(use_mcp=True, mcp_server_url='http://unused')
    try:
        assert len(manager.tool_definitions) == 50 and 'query_order' not in manager.tool_names
        assert json.loads(manager.execute_tool('query_order', {}))['success'] is False
    finally:
        manager.close()


def test_task_only_receives_the_new_subset_with_optional_flags(store, monkeypatch):
    monkeypatch.setattr(settings, 'memory_enabled', True)
    monkeypatch.setattr(settings, 'task_operations_enabled', True)
    record, source = task(store, 's')
    client = Client(response('目前无法查询业务数据，请补充您的具体需求。'))
    req = request(record, source)
    result = engine(client).execute_task(store, 'alice', req)
    assert result['state'] == 'ready' and result['tool_calls'] == 0
    assert result['evidence_ids'] == []
    names={t['function']['name'] for t in client.requests[0]['tools']}
    assert len(client.requests) == 1 and 'read_task_context' in names
    assert 'recall_user_memory' not in names and 'query_order' not in names
    assert engine(Client()).execute_task(store, 'alice', req) == result


@pytest.mark.parametrize('name', ['query_order', 'prepare_operation', 'recall_user_memory'])
def test_model_cannot_invoke_removed_tools(store, name):
    record, source = task(store, 's')
    client = Client(response(calls=[(name, {})]))
    result = engine(client).execute_task(store, 'alice', request(record, source),
        tools=lambda *args: pytest.fail('retired tool executed'))
    assert result['state'] == 'needs_human' and result['tool_calls'] == 0
    assert result['evidence_ids'] == []


def test_background_adapters_do_not_use_legacy_business_tables():
    business = BusinessOperations()
    with pytest.raises(PreparationRejected, match='已移除'):
        business.preview(SimpleNamespace(), 'apply_refund')
    assert business.write('alice', 'apply_refund', {}, 'old-operation', 'v1')['success'] is False
    # Missing implementation is not proof of a historical operation's failure.
    assert business.query('alice', 'old-operation')['status'] == 'unknown'
    with pytest.raises(ExternalEvidenceError, match='历史业务接口不支持该进度查询'):
        BusinessProgress().read('alice', {'kind': 'logistics'})
