# 文件说明：测试换货维修只读资格与确认办理共用规则且不产生业务副作用。
"""Read-only exchange/repair eligibility shares the confirmed-operation rules."""
import asyncio
import json
from datetime import timedelta

import pytest

from app.config.settings import settings
from app.data.business_store import Transaction
from app.services.commerce import CommerceService, service_factory
from app.services.commerce.common import BusinessError, dumps, utcnow
from app.services.commerce.providers import provider_factory
from app.tasks.candidates import BusinessScope
from app.tasks.operations import TaskOperations
from app.tasks.tool_facts import render
from app.tools.catalog import BY_NAME
from app.tools.registry import execute_tool
from app.tools.request_context import set_current_user_id, reset_current_user_id
from app.tools.selection import for_task
from tests.test_core_tools import database, svc
from tests.test_extended_tools import extended, Provider


def arguments(kind, **extra):
    return dict(order_item_id='i-paid', quantity=1,
        **({'replacement_sku_id':'s1'} if kind == 'exchange' else {}), **extra)


def preparing(kind, args):
    return dict(args, **({'reason':'换规格'} if kind == 'exchange' else {'problem':'用户描述无法开机'}))


@pytest.mark.parametrize('kind', ['exchange', 'repair'])
def test_public_assessment_has_no_writes_proposals_or_provider_calls(extended, monkeypatch, kind):
    service = extended
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    def forbidden(*args, **kwargs):
        pytest.fail('Consultation must not prepare, write, reserve or call a provider')
    monkeypatch.setattr(Transaction, 'execute', forbidden)
    monkeypatch.setattr(TaskOperations, 'prepare', forbidden)
    monkeypatch.setattr(service, 'preview', forbidden)
    token = set_current_user_id('alice')
    try:
        with service_factory(lambda user, scope:service), provider_factory(forbidden):
            result = json.loads(execute_tool('assess_' + kind, arguments(kind)))
    finally:
        reset_current_user_id(token)
    assert result['success'], result
    data = result['data']
    assert data['eligibility'] == 'eligible' and data['policy_id'] == kind
    assert data['policy_version'] == 1 and data['remaining_quantity'] == 3
    assert data['application_submitted'] is False and data['missing_fields'] == []
    assert 'amount' not in data and 'next_state' not in data and 'operation_id' not in data
    text = '\n'.join(render('assess_' + kind, result))
    assert '尚未提交' in text and '符合已核实规则' in text
    if kind == 'repair':
        assert data['repair_fee'] is None and data['inspection_result'] is None
        assert '不代表免费维修' in text


@pytest.mark.parametrize('kind', ['exchange', 'repair'])
@pytest.mark.parametrize('case,expected', [
    ('status', 'ineligible'), ('quantity', 'ineligible'), ('occupied', 'ineligible'),
    ('expired', 'ineligible'), ('manual_review', 'unknown'), ('missing_policy', 'unknown'),
])
def test_consultation_and_preparation_share_rejections(extended, kind, case, expected):
    service = extended; args = arguments(kind); now = utcnow()
    with service.store.transaction(write=True) as tx:
        if case == 'status':
            tx.execute("UPDATE orders SET status='closed' WHERE order_id='paid'")
        elif case == 'quantity':
            args['quantity'] = 4
        elif case == 'occupied':
            tx.execute("""INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,created_at,updated_at)
                VALUES ('existing','shop','paid','alice','repair','processing','test',%s,%s)""", (now, now))
            tx.execute("INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity) VALUES ('existing','i-paid',3)")
        elif case == 'missing_policy':
            tx.execute('DELETE FROM business_policy_versions WHERE action=%s', (kind,))
        else:
            rules = dict(schema_version=1, allowed_statuses=['paid'], **(
                {'requires_reason_review':True} if case == 'manual_review' else {'window_days':7}))
            tx.execute('UPDATE business_policy_versions SET rules=%s WHERE action=%s', (dumps(rules), kind))
            if case == 'expired':
                tx.execute("""INSERT INTO shipments(shipment_id,platform_code,order_id,status,created_at,shipped_at,delivered_at)
                    VALUES ('original','shop','paid','delivered',%s,%s,%s)""", (now, now-timedelta(days=31), now-timedelta(days=30)))
                tx.execute("INSERT INTO shipment_items(shipment_id,order_item_id,quantity) VALUES ('original','i-paid',3)")
    result = service.read('assess_' + kind, args)
    assert result['data']['eligibility'] == expected
    assert result['data']['reasons']
    assert result['data']['order_item_id'] == 'i-paid'
    if expected == 'unknown': assert result['data']['missing_fields']
    if case != 'missing_policy': assert result['data']['policy_id'] == kind
    with provider_factory(lambda _:Provider()), pytest.raises(BusinessError) as error:
        service.preview('prepare_' + kind, preparing(kind, args))
    assert error.value.code == ('not_eligible' if expected == 'ineligible' else 'insufficient_data')
    assert str(error.value) == result['data']['reasons'][0]


@pytest.mark.parametrize('case,expected', [
    ('missing_target','unknown'), ('missing_inventory','unknown'), ('stock','ineligible'),
    ('inactive','ineligible'), ('currency','ineligible'), ('product','ineligible'), ('price','ineligible'),
])
def test_exchange_target_stock_and_price_rules(extended, case, expected):
    args = arguments('exchange'); service = extended
    with service.store.transaction(write=True) as tx:
        if case == 'missing_target': args.pop('replacement_sku_id')
        elif case == 'missing_inventory': tx.execute("DELETE FROM sku_inventory WHERE sku_id='s1'")
        elif case == 'stock': tx.execute("UPDATE sku_inventory SET available_qty=0 WHERE sku_id='s1'")
        elif case == 'inactive': tx.execute("UPDATE product_skus SET status='inactive' WHERE sku_id='s1'")
        elif case == 'currency': tx.execute("UPDATE product_skus SET currency='USD' WHERE sku_id='s1'")
        elif case == 'product':
            tx.execute("INSERT INTO products(product_id,merchant_id,name,category,description,specs) VALUES ('other','m','其他商品','耳机','描述','{}')")
            tx.execute("UPDATE product_skus SET product_id='other' WHERE sku_id='s1'")
        else: args['replacement_sku_id'] = 's2'
    result = service.read('assess_exchange', args)
    assert result['data']['eligibility'] == expected
    if expected == 'unknown': assert result['data']['missing_fields']
    with provider_factory(lambda _:Provider()), pytest.raises(BusinessError) as error:
        service.preview('prepare_exchange', preparing('exchange', args))
    assert str(error.value) == result['data']['reasons'][0]


@pytest.mark.parametrize('kind', ['exchange', 'repair'])
@pytest.mark.parametrize('scope,user', [
    (BusinessScope(platform_code='shop'), 'bob'),
    (BusinessScope(platform_code='other'), 'alice'),
    (BusinessScope(platform_code='shop', store_id='other'), 'alice'),
])
def test_assessment_checks_owner_and_trusted_scope(extended, kind, scope, user):
    service = CommerceService(extended.store, user, scope=scope, secret=extended.secret)
    with pytest.raises(BusinessError) as error:
        service.read('assess_' + kind, arguments(kind))
    assert error.value.code == 'not_found'


@pytest.mark.parametrize('change', ['stock', 'policy'])
def test_prepare_and_submit_recheck_changes_after_consultation(extended, change):
    service = extended; args = arguments('exchange')
    assert service.read('assess_exchange', args)['data']['eligibility'] == 'eligible'
    with provider_factory(lambda _:Provider()):
        prepared = service.preview('prepare_exchange', preparing('exchange', args))
        with service.store.transaction(write=True) as tx:
            if change == 'stock': tx.execute("UPDATE sku_inventory SET available_qty=0 WHERE sku_id='s1'")
            else: tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='exchange'", (dumps(dict(schema_version=1, allowed_statuses=['received'])),))
        with pytest.raises(BusinessError):
            service.submit('prepare_exchange', prepared['arguments'], 'changed-eligibility', prepared['business_version'])
        with pytest.raises(BusinessError):
            service.preview('prepare_exchange', preparing('exchange', args))
    with service.store.transaction() as tx:
        for table in ('after_sale_requests', 'service_work_orders', 'service_inventory_reservations', 'commerce_provider_requests'):
            assert tx.one('SELECT COUNT(*) n FROM ' + table)['n'] == 0
        assert tx.one("SELECT reserved_qty FROM sku_inventory WHERE sku_id='s1'")['reserved_qty'] == 0


@pytest.mark.parametrize('price,difference', [(20,10),(5,-5),(10,0)])
def test_price_difference_uses_same_allocation_without_reporting_a_refund(extended, price, difference):
    with extended.store.transaction(write=True) as tx:
        tx.execute("UPDATE product_skus SET list_price=%s WHERE sku_id='s2'", (price,))
        tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='exchange'", (dumps(dict(schema_version=1,allowed_statuses=['paid'],allow_exchange_price_difference=True)),))
    args = arguments('exchange'); args['replacement_sku_id'] = 's2'
    result = extended.read('assess_exchange', args)
    assert result['data']['eligibility'] == 'eligible'
    assert result['data']['price_difference'] == difference and 'amount' not in result['data']
    with provider_factory(lambda _:Provider()):
        prepared = extended.preview('prepare_exchange', preparing('exchange', args))
    assert prepared['review']['price_difference'] == format(difference, '.2f')
    text = '\n'.join(render('assess_exchange', result))
    assert format(abs(difference), '.2f') in text
    if difference < 0: assert '比原商品分摊金额低' in text


def test_selection_mcp_and_validation_expose_read_only_tools(extended, monkeypatch):
    from app.mcp.server import list_business_tools, call_business_tool
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    for name, goal in [('assess_exchange','能换成白色吗'), ('assess_repair','这件能维修吗')]:
        assert name in for_task(goal=goal)
        assert BY_NAME[name].effect == 'read' and not BY_NAME[name].task_only
    listed = {tool.name:tool for tool in asyncio.run(list_business_tools())}
    for name in ('assess_exchange', 'assess_repair'):
        assert listed[name].annotations.readOnlyHint
    token = set_current_user_id('alice')
    try:
        with service_factory(lambda user, scope:extended):
            result = asyncio.run(call_business_tool('assess_repair', arguments('repair')))
            assert result['data']['eligibility'] == 'eligible'
            for name in ('assess_exchange','assess_repair'):
                for invalid in ({'quantity':True}, {'quantity':0}, {'user_id':'bob'}):
                    result = json.loads(execute_tool(name, dict(arguments(name.removeprefix('assess_')), **invalid)))
                    assert result['error']['code'] == 'invalid_arguments'
    finally:
        reset_current_user_id(token)
    monkeypatch.setattr(settings, 'tool_allowlist', 'assess_repair')
    assert [tool.name for tool in asyncio.run(list_business_tools())] == ['assess_repair']
