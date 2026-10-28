"""Tool exposure, dispatch boundaries and live goal changes with no provider IO."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from app.config.settings import Settings, settings
from app.data_plan.enums import Intent
from app.session.context_budget import estimate_request_tokens
from app.tools.catalog import BY_NAME
from app.tools.registry import TOOL_DEFINITIONS, execute_tool
from app.tools.selection import COMMON, TASK_TOOLS, for_task, global_tools
from tests.test_message_association import batch_for, proposal, service, store, update
from tests.test_task_candidates import task
from tests.test_task_execution import Client, engine, request, response
from tests.test_task_storage import empty_store


@pytest.fixture(autouse=True)
def isolated_selection(monkeypatch):
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    monkeypatch.setattr(settings, 'api_concurrency_redis_url', '')


@pytest.mark.parametrize('goal,required,excluded', [
    ('查询耳机物流', {'find_orders', 'get_order', 'get_logistics'}, {'prepare_purchase', 'prepare_invoice'}),
    ('申请退款', {'find_orders', 'get_order_items', 'assess_after_sale', 'prepare_after_sale'}, {'prepare_purchase'}),
    ('查询退款到账进度', {'get_refunds', 'find_after_sales'}, {'prepare_after_sale'}),
    ('申请换货', {'search_products', 'get_product_details', 'prepare_exchange'}, {'prepare_invoice'}),
    ('耳机维修', {'prepare_repair', 'get_order_items'}, {'prepare_purchase'}),
    ('查询价保资格', {'get_price_history', 'assess_price_protection', 'prepare_price_protection'}, {'claim_coupon'}),
    ('申请发票', {'find_orders', 'get_invoices', 'prepare_invoice'}, {'prepare_after_sale'}),
    ('修改收货地址', {'list_addresses', 'assess_order_action', 'prepare_order_address_change'}, {'prepare_exchange'}),
    ('这单催一下发货', {'request_dispatch', 'get_logistics'}, {'prepare_after_sale'}),
    ('购买两件耳机', {'search_products', 'quote_checkout', 'prepare_purchase'}, {'prepare_invoice'}),
    ('查询库存', {'get_restock_plan'}, {'prepare_purchase'}),
    ('查询会员积分', {'get_membership', 'list_points'}, {'prepare_purchase'}),
    ('帮我处理一下', {'find_orders', 'ask_user'}, {'prepare_purchase', 'prepare_after_sale'}),
])
def test_goals_keep_dependencies_and_reduce_schemas(goal, required, excluded):
    selected = for_task(Intent.OTHERS, goal)
    assert required | COMMON <= selected
    assert not selected & excluded
    schemas = [d for d in TOOL_DEFINITIONS if d['function']['name'] in selected]
    assert estimate_request_tokens({'tools': schemas}) < estimate_request_tokens({'tools': TOOL_DEFINITIONS})


def test_profiles_cover_registered_tools_without_inventing_names():
    assert COMMON.union(*TASK_TOOLS.values()) == frozenset(BY_NAME)
    assert 'get_logistics' in for_task(Intent.LOGISTICS_INQUIRY)
    assert 'prepare_invoice' in for_task(Intent.INVOICE_REQUEST)
    assert {'get_logistics', 'prepare_invoice'} <= for_task('others', '查询物流并申请发票')


@pytest.mark.parametrize('value,expected', [
    ('', set()), ('  ', set()),
    (' get_order, get_logistics, get_order ', {'get_order', 'get_logistics'}),
])
def test_global_ceiling_also_restricts_common_tools(monkeypatch, value, expected):
    monkeypatch.setattr(settings, 'tool_allowlist', value)
    assert global_tools() == expected
    assert for_task('others', '查询物流') == expected


def test_environment_configuration_is_validated(monkeypatch):
    monkeypatch.setenv('TOOL_ALLOWLIST', 'get_order,get_logistics')
    assert Settings(_env_file=None).tool_allowlist == 'get_order,get_logistics'
    for value in ('get_oder', '*,get_order'):
        monkeypatch.setenv('TOOL_ALLOWLIST', value)
        with pytest.raises(ValidationError, match='TOOL_ALLOWLIST'):
            Settings(_env_file=None)


def test_global_ceiling_applies_to_registry_manager_and_mcp(monkeypatch):
    from app.mcp.server import list_business_tools, call_business_tool
    from app.tools.manager import ToolManager
    from app.tools.registry import _TOOL_MAP
    monkeypatch.setattr(settings, 'tool_allowlist', 'get_order,ask_user')
    monkeypatch.setitem(_TOOL_MAP, 'get_logistics', lambda **kw: pytest.fail('disabled tool executed'))
    manager = ToolManager(allowed_tools={'get_order', 'get_logistics', 'ask_user'})
    assert manager.tool_names == {'get_order', 'ask_user'}
    assert not json.loads(execute_tool('get_logistics', {}))['success']
    assert not json.loads(manager.execute_tool('get_logistics', {}))['success']
    assert {t.name for t in asyncio.run(list_business_tools())} == {'get_order'}
    assert not asyncio.run(call_business_tool('get_logistics', {}))['success']


def tool_names(params):
    return {d['function']['name'] for d in params.get('tools', [])}


def test_every_react_request_contains_only_selected_tools(store):
    original, source = task(store, 's', goal='查询物流')
    client = Client(response(calls=[('get_order', {})]), response('已核实当前订单。'))
    result = engine(client).execute_task(store, 'alice', request(original, source),
        tools=lambda *args: {'success': True, 'data': {}})
    assert result['state'] == 'ready' and len(client.requests) == 2
    for params in client.requests:
        assert tool_names(params) == for_task('others', original.goal)
        assert len(params['tools']) < len(TOOL_DEFINITIONS)


@pytest.mark.parametrize('allowlist,tool', [('*', 'prepare_purchase'), ('get_order', 'get_logistics'), ('', 'get_order')])
def test_model_cannot_execute_a_hidden_registered_tool(store, monkeypatch, allowlist, tool):
    monkeypatch.setattr(settings, 'tool_allowlist', allowlist)
    original, source = task(store, 's', goal='查询物流')
    client = Client(response(calls=[(tool, {})]))
    result = engine(client).execute_task(store, 'alice', request(original, source),
        tools=lambda *args: pytest.fail('hidden tool executed'))
    assert tool not in tool_names(client.requests[0])
    if not allowlist:
        assert 'tools' not in client.requests[0]
    assert result['state'] == 'needs_human' and result['tool_calls'] == 0


def test_replan_replaces_tool_profiles_with_current_goal(store):
    original, source = task(store, 's', goal='查询物流')
    def change_goal(params):
        assert 'get_logistics' in tool_names(params)
        assert 'prepare_invoice' not in tool_names(params)
        batch, svc = batch_for(store, '改为申请发票'), service(store)
        decision = update(batch, original, effect='amend')
        decision['goal'] = '申请发票'
        svc.commit(batch, svc.snapshot(batch), proposal(decision))
        return response(calls=[('get_logistics', {})])
    client = Client(change_goal, response('已记录开票需求。'))
    result = engine(client).execute_task(store, 'alice', request(original, source),
        tools=lambda *args: pytest.fail('stale tool executed'))
    assert result['state'] == 'ready' and len(client.requests) == 2
    assert 'prepare_invoice' in tool_names(client.requests[1])
    assert 'get_logistics' not in tool_names(client.requests[1])
