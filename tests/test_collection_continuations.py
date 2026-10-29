"""Public summary-to-detail paths against a disposable real MySQL database."""
import asyncio
import json
import time
from datetime import timedelta

import pytest

from app.config.settings import settings
from app.services.commerce import service_factory
from app.services.commerce.common import BusinessError, ref, utcnow
from app.tasks.candidates import BusinessScope
from app.tasks.tool_facts import render
from app.tools.registry import execute_tool
from app.tools.request_context import set_current_user_id, reset_current_user_id
from app.tools.selection import TASK_TOOLS
from tests.test_core_tools import database, svc


@pytest.fixture
def collections(svc, monkeypatch):
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    now = utcnow()
    with svc.store.transaction(write=True) as tx:
        for aid in ('sale', 'sale2'):
            tx.execute('''INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,created_at,updated_at)
                VALUES (%s,'shop','paid','alice','return_refund','approved','test',%s,%s)''', (aid, now, now))
        tx.execute('''INSERT INTO return_authorizations(authorization_id,after_sale_id,address_ciphertext,key_version,carriers,requirements,deadline,status,source,updated_at)
            VALUES ('auth','sale',%s,1,'[]','[]',%s,'active','test',%s)''', (b'unused-address', now + timedelta(days=7), now))
        tx.execute('''INSERT INTO return_shipments(return_shipment_id,authorization_id,carrier_code,tracking_number,status,operation_id,created_at,updated_at)
            VALUES ('return','auth','test','tracking','registered','test-op',%s,%s)''', (now, now))
        for sid, kind, aid in [('original','original',None), ('exchange','exchange','sale'), ('empty','original',None)]:
            tx.execute('''INSERT INTO shipments(shipment_id,platform_code,order_id,status,created_at,shipment_kind,after_sale_id)
                VALUES (%s,'shop','paid','registered',%s,%s,%s)''', (sid, now, kind, aid))
        for index in range(53):
            ident = f'line-{index:03}'
            tx.execute('''INSERT INTO order_items(order_item_id,platform_code,order_id,sku_id,product_id,title_snapshot,spec_snapshot,quantity,unit_price,discount_amount,payable_amount)
                VALUES (%s,'shop','paid','s1','p','测试商品','{"颜色":"红色"}',1,10,0,10)''', (ident,))
            tx.execute("INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity) VALUES ('sale',%s,1)", (ident,))
            tx.execute("INSERT INTO return_shipment_items(return_shipment_id,after_sale_id,order_item_id,quantity) VALUES ('return','sale',%s,1)", (ident,))
            for sid in ('original', 'exchange'):
                tx.execute('''INSERT INTO shipment_items(shipment_id,order_item_id,quantity,actual_sku_id,actual_spec_snapshot)
                    VALUES (%s,%s,1,'s2','{"颜色":"蓝色"}')''', (sid, ident))
            tx.execute('''INSERT INTO restock_plans(plan_id,sku_id,warehouse_id,status,source,updated_at)
                VALUES (%s,'s1','warehouse','planned','test',%s)''', (f'plan-{index:03}', now - timedelta(seconds=index//2)))
    token = set_current_user_id('alice')
    try:
        with service_factory(lambda user, scope: svc):
            yield svc
    finally:
        reset_current_user_id(token)


def call(name, **arguments):
    result = json.loads(execute_tool(name, arguments))
    assert result['success'], result
    return result


def follow(entry, key='order_item_id'):
    arguments = dict(entry['arguments'])
    identifiers, sizes = [], []
    for _ in range(10):
        result = call(entry['tool'], **arguments)
        data = result['data']
        identifiers.extend(row[key] for row in data['items'])
        sizes.append(len(data['items']))
        assert data['has_more'] == bool(result['next_cursor'])
        assert result['sources'][0]['kind'] == 'mysql'
        if not result['next_cursor']:
            assert len(identifiers) == len(set(identifiers)) == data['total']
            return identifiers, sizes
        arguments['cursor'] = result['next_cursor']
    pytest.fail('pagination did not terminate')


def test_summaries_lead_to_all_items_through_public_tools(collections):
    expected = [f'line-{i:03}' for i in range(53)]
    for kind, ident in [('shipment','original'), ('shipment','exchange'), ('return_shipment','return')]:
        summary = call('get_logistics', target_ref=ref(kind,ident))['data']['shipment']
        assert summary['items_truncated'] and len(summary['items']) == 20
        assert follow(summary['items_query']) == (expected, [20,20,13])
        result = call('get_shipment_items', target_ref=ref(kind,ident))
        assert result['data']['items'][0]['spec_snapshot'] == {'颜色':'红色'}
        assert any('包裹记录数量' in line for line in render('get_shipment_items', result))
        if kind == 'shipment':
            assert result['data']['items'][0]['actual_spec_snapshot'] == {'颜色':'蓝色'}
    summary = call('get_after_sale_status', after_sale_id='sale')['data']
    assert summary['items_truncated'] and len(summary['purchase_items']) == 20
    assert follow(summary['purchase_items_query']) == (expected, [20,20,13])
    result = call('get_after_sale_items', after_sale_id='sale')
    assert any('售后申请数量' in line for line in render('get_after_sale_items', result))
    listed = call('find_after_sales')['data']['items']
    sale = next(row for row in listed if row['after_sale_id'] == 'sale')
    assert sale['items_truncated'] and follow(sale['items_query'])[0] == expected
    order = call('get_order', order_ref=ref('order','shop','paid'))['data']
    assert order['items_truncated']
    assert follow(order['items_query'])[0] == ['i-paid'] + expected
    listed = call('find_orders')['data']['items']
    order = next(row for row in listed if row['order_id'] == 'paid')
    assert order['items_truncated'] and len(follow(order['items_query'])[0]) == 54
    assert follow({'tool':'get_restock_plan','arguments':{'sku_id':'s1'}}, 'plan_id') == (
        [f'plan-{i:03}' for i in range(53)], [20,20,13])


@pytest.mark.parametrize('count,sizes', [(0,[0]), (20,[20]), (21,[20,1])])
def test_restock_page_boundaries(collections, count, sizes):
    with collections.store.transaction(write=True) as tx:
        tx.execute('DELETE FROM restock_plans WHERE plan_id>=%s', (f'plan-{count:03}',))
    assert follow({'tool':'get_restock_plan','arguments':{'sku_id':'s1'}}, 'plan_id')[1] == sizes


def test_empty_item_collections(collections):
    for name, args in [('get_shipment_items',{'target_ref':ref('shipment','empty')}),
                       ('get_after_sale_items',{'after_sale_id':'sale2'})]:
        assert follow({'tool':name,'arguments':args}) == ([],[0])


def test_cursor_cannot_cross_target_tool_user_or_scope(collections, monkeypatch):
    token = call('get_shipment_items', target_ref=ref('shipment','original'))['next_cursor']
    for name, args in [('get_shipment_items',{'target_ref':ref('shipment','exchange')}),
                       ('get_shipment_items',{'target_ref':ref('return_shipment','return')}),
                       ('get_after_sale_items',{'after_sale_id':'sale'}),
                       ('get_logistics',{'target_ref':ref('shipment','original')})]:
        with pytest.raises(BusinessError) as error:
            collections.read(name, dict(args,cursor=token))
        assert error.value.code == 'invalid_cursor'
    token = call('get_restock_plan',sku_id='s1')['next_cursor']
    with pytest.raises(BusinessError) as error:
        collections.read('get_restock_plan', {'sku_id':'s2','cursor':token})
    assert error.value.code == 'invalid_cursor'
    with monkeypatch.context() as patch:
        patch.setattr(collections, 'user', 'bob')
        with pytest.raises(BusinessError) as error:
            collections.read('get_restock_plan', {'sku_id':'s1','cursor':token})
        assert error.value.code == 'invalid_cursor'
        for name,args in [('get_after_sale_items',{'after_sale_id':'sale'}),
                          ('get_shipment_items',{'target_ref':ref('return_shipment','return')})]:
            with pytest.raises(BusinessError): collections.read(name,args)
    with monkeypatch.context() as patch:
        patch.setattr(collections, 'scope', BusinessScope(platform_code='shop',store_id='m'))
        with pytest.raises(BusinessError) as error:
            collections.read('get_restock_plan', {'sku_id':'s1','cursor':token})
        assert error.value.code == 'invalid_cursor'
    with monkeypatch.context() as patch:
        future = time.time() + 1000
        patch.setattr('app.services.commerce.pagination.time.time', lambda: future)
        with pytest.raises(BusinessError) as error:
            collections.read('get_restock_plan', {'sku_id':'s1','cursor':token})
        assert error.value.code == 'invalid_cursor'


def test_each_page_rechecks_owner_and_item_links(collections):
    args = {'target_ref':ref('shipment','original')}
    cursor = call('get_shipment_items',**args)['next_cursor']
    with collections.store.transaction(write=True) as tx:
        tx.execute("UPDATE shipments SET order_id='other' WHERE shipment_id='original'")
    with pytest.raises(BusinessError) as error:
        collections.read('get_shipment_items',dict(args,cursor=cursor))
    assert error.value.code == 'not_found'
    with collections.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity) VALUES ('sale','i-other',1)")
    with pytest.raises(BusinessError) as error:
        collections.read('get_after_sale_items',{'after_sale_id':'sale'})
    assert error.value.code == 'insufficient_data'
    with collections.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity) VALUES ('sale2','i-paid',1)")
        tx.execute("INSERT INTO return_shipment_items(return_shipment_id,after_sale_id,order_item_id,quantity) VALUES ('return','sale2','i-paid',1)")
    with pytest.raises(BusinessError) as error:
        collections.read('get_shipment_items',{'target_ref':ref('return_shipment','return')})
    assert error.value.code == 'insufficient_data'


def test_continuation_tools_are_available_in_profiles_and_mcp(monkeypatch):
    from app.mcp.server import list_business_tools
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    for profile in TASK_TOOLS.values():
        if 'get_after_sale_status' in profile or 'get_logistics' in profile:
            assert 'get_shipment_items' in profile
        if 'find_after_sales' in profile or 'get_after_sale_status' in profile:
            assert 'get_after_sale_items' in profile
    names = {tool.name for tool in asyncio.run(list_business_tools())}
    assert {'get_shipment_items','get_after_sale_items'} <= names


def test_account_and_invoice_summaries_have_complete_masked_pages(collections, monkeypatch):
    now = utcnow()
    with collections.store.transaction(write=True) as tx:
        tx.execute('''INSERT INTO invoice_requests(invoice_id,platform_code,order_id,user_id,invoice_type,title_ciphertext,key_version,status,request_id,created_at)
            VALUES ('invoice','shop','paid','alice','personal',%s,1,'issued','invoice-op',%s)''', (b'private-title',now))
        for index in range(53):
            tx.execute('''INSERT INTO user_contacts(user_id,contact_type,value_ciphertext,lookup_digest,key_version,verified_at,created_at)
                VALUES ('alice','email',%s,%s,1,%s,%s)''', (b'private-contact',index.to_bytes(32,'big'),now,now))
            tx.execute('''INSERT INTO user_platform_accounts(user_id,platform_code,external_user_id,verified_at,status)
                VALUES ('alice',%s,%s,%s,'active')''', (f'platform-{index:03}',f'private-external-{index}',now))
            tx.execute('''INSERT INTO issued_invoices(issued_invoice_id,invoice_id,invoice_number,status,document_key,provider,issued_at,updated_at)
                VALUES (%s,'invoice',%s,'issued','private/document.pdf','test',%s,%s)''', (f'issued-{index:03}',f'number-{index:03}',now,now))
    summary = call('get_my_account')['data']
    assert summary['contacts_truncated'] and summary['platform_accounts']['truncated']
    assert follow(summary['contacts_query'],'contact_id')[1] == [20,20,13]
    assert follow(summary['platform_accounts']['items_query'],'id')[1] == [20,20,13]
    contacts = call('get_my_account',collection='contacts')
    platforms = call('get_my_account',collection='platform_accounts')
    assert all(row['value']=='***' for row in contacts['data']['items'])
    assert 'private-' not in json.dumps([contacts,platforms])
    assert render('get_my_account',contacts) and render('get_my_account',platforms)
    summary = call('get_invoices')['data']['items'][0]
    assert summary['issued_invoices_truncated'] and len(summary['issued_invoices']) == 5
    assert follow(summary['issued_invoices_query'],'issued_invoice_id') == (
        [f'issued-{index:03}' for index in range(53)], [20,20,13])
    invoice_args = summary['issued_invoices_query']['arguments']
    records = call('get_invoices',**invoice_args)
    assert 'document_key' not in json.dumps(records)
    assert any('正式发票' in line for line in render('get_invoices',records))
    for name,args,cursor in [
        ('get_my_account',{'collection':'platform_accounts'},contacts['next_cursor']),
        ('get_invoices',{'target_ref':ref('invoice','invoice'),'view':'requests'},records['next_cursor']),
    ]:
        result = json.loads(execute_tool(name,dict(args,cursor=cursor)))
        assert not result['success'] and result['error']['code']=='invalid_cursor'
    for name,args in [('get_my_account',{'cursor':contacts['next_cursor']}),('get_invoices',{'view':'issued'})]:
        assert not json.loads(execute_tool(name,args))['success']
    with monkeypatch.context() as patch:
        patch.setattr(collections,'user','bob')
        with pytest.raises(BusinessError):
            collections.read('get_invoices',dict(invoice_args,cursor=records['next_cursor']))
        with pytest.raises(BusinessError):
            collections.read('get_my_account',{'collection':'contacts','cursor':contacts['next_cursor']})
