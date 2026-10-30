# 文件说明：测试核心工具与事项状态、确认卡片及 MCP 暴露范围的集成。
"""Integration of the new tools with task state, confirmation and MCP boundaries."""
import asyncio
import json
import pytest
from app.config.settings import settings
from app.services.commerce import service_factory, CommerceService
from app.services.commerce.common import ref
from app.tasks.operations import TaskOperations
from app.tools.request_context import set_current_user_id,reset_current_user_id
from tests.test_core_tools import database,svc,standard_provider
from tests.test_task_candidates import task
from tests.test_message_association import store
from tests.test_task_storage import empty_store
from tests.test_task_execution import Client,response,request,engine

@pytest.fixture
def business(svc,monkeypatch):
    monkeypatch.setattr(settings,'api_jwt_secret','isolated-test-confirmation-key-at-least-32-characters')
    monkeypatch.setattr(settings,'task_operations_enabled',True)
    def factory(user,scope):
        return CommerceService(svc.store,user,scope=scope or svc.scope,secret=svc.secret,keys=json.dumps(svc.cipher.keys))
    with service_factory(factory): yield svc

def test_mcp_only_exposes_read_tools():
    from app.mcp.server import list_business_tools,call_business_tool
    tools=asyncio.run(list_business_tools())
    assert len(tools)==36
    assert all(t.annotations.readOnlyHint for t in tools)
    assert 'prepare_invoice' not in {t.name for t in tools}
    assert not asyncio.run(call_business_tool('claim_coupon',{}))['ok']


def test_prepared_claim_is_not_rendered_as_claimed():
    from app.tasks.tool_facts import render
    lines=render('claim_coupon',{'success':True,'data':{'next_state':'needs_confirmation'}})
    assert '尚未执行' in ''.join(lines) and '已领取' not in ''.join(lines)


def test_dispatch_task_confirmation_queues_once(store,business):
    from app.services.commerce.providers import provider_factory
    from tests.test_extended_tools import Provider
    provider=Provider()
    rec,source=task(store,'s',goal='这单催一下发货',details={'order_id':'paid','platform_code':'shop'})
    client=Client(response(calls=[('request_dispatch',{'order_ref':ref('order','shop','paid')})]))
    with provider_factory(lambda _:provider):
        out=engine(client).execute_task(store,'alice',request(rec,source))
        assert out['state']=='needs_confirmation'
        with business.store.transaction() as tx: assert tx.one('SELECT COUNT(*) n FROM commerce_provider_requests')['n']==0
        operations=TaskOperations(store,'alice'); card=operations.list('s')[0]
        result=operations.confirm('s',card['operation_id'],card['confirmation_token'])
        assert result['status']=='succeeded',result
        assert result['result']['status']=='queued'
        assert operations.confirm('s',card['operation_id'],card['confirmation_token'])==result
        assert not provider.calls

def test_question_stops_same_turn_before_further_calls(store):
    rec,source=task(store,'s',goal='帮我查询订单')
    client=Client(response(calls=[('ask_user',{'question':'需要查询哪一笔订单？'}),('get_my_profile',{})]))
    out=engine(client).execute_task(store,'alice',request(rec,source))
    assert out['state']=='waiting_user' and out['tool_calls']==1
    assert out['question']['prompt']=='需要查询哪一笔订单？'

def test_handoff_creates_real_ticket_and_stops(store):
    rec,source=task(store,'s',goal='订单需要人工核实')
    client=Client(response(calls=[('handoff_to_human',{'reason':'用户要求人工核实'})]))
    out=engine(client).execute_task(store,'alice',request(rec,source))
    assert out['state']=='needs_human' and out['ticket_id']
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM support_tickets')['n']==1

def test_read_tool_records_owned_evidence(store,business):
    rec,source=task(store,'s',goal='查询订单',details={'order_id':'paid','platform_code':'shop'})
    client=Client(response(calls=[('get_order',{'order_ref':ref('order','shop','paid')})]),response('已查询订单信息。'))
    out=engine(client).execute_task(store,'alice',request(rec,source))
    assert out['state']=='ready' and out['tool_calls']==1 and out['evidence_ids']

def test_prepare_confirm_and_repeat_via_real_adapter(store,business,standard_provider):
    rec,source=task(store,'s',goal='申请退款',details={'order_id':'paid','platform_code':'shop'})
    args={'kind':'refund_only','items':[{'order_item_id':'i-paid','quantity':1}],'reason':'不需要了'}
    client=Client(response(calls=[('prepare_after_sale',args)]))
    out=engine(client).execute_task(store,'alice',request(rec,source))
    assert out['state']=='needs_confirmation',out
    with business.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM after_sale_requests')['n']==0
    operations=TaskOperations(store,'alice')
    card=next(x for x in operations.list('s') if x['operation_id']==out['operation_id'])
    result=operations.confirm('s',card['operation_id'],card['confirmation_token'])
    assert result['status']=='succeeded',result
    assert operations.confirm('s',card['operation_id'],card['confirmation_token'])==result
    with business.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM after_sale_requests')['n']==1

@pytest.mark.parametrize('text',['这张券帮我领一下','有什么券可以领取','如果我说“请帮我领取这张券”，会发生什么'])
def test_claim_always_requires_bound_confirmation(store,business,text):
    rec,source=task(store,'s',goal=text,details={'platform_code':'shop'})
    client=Client(response(calls=[('claim_coupon',{'coupon_ref':ref('coupon_template','c')})]),response('优惠券已领取。'))
    out=engine(client).execute_task(store,'alice',request(rec,source))
    assert out['state']=='needs_confirmation'
    with business.store.transaction() as tx: assert tx.one('SELECT COUNT(*) AS n FROM user_coupons')['n']==0
    operations=TaskOperations(store,'alice')
    card=operations.list('s')[0]
    assert card['review']['name']=='满十减五'
    result=operations.confirm('s',card['operation_id'],card['confirmation_token'])
    assert result['status']=='succeeded',result
    assert operations.confirm('s',card['operation_id'],card['confirmation_token'])==result
    with business.store.transaction() as tx: assert tx.one('SELECT COUNT(*) AS n FROM user_coupons')['n']==1

def test_invoice_card_is_private_and_prepare_replay_stays_masked(store,business,standard_provider):
    rec,source=task(store,'s',goal='申请发票',details={'order_id':'paid','platform_code':'shop'})
    args={'order_ref':ref('order','shop','paid'),'invoice_type':'company','title':'示例公司','tax_number':'SECRET-TAX'}
    operations=TaskOperations(store,'alice')
    result=operations.prepare('s',rec.task_id,rec.version,'prepare_invoice',arguments=args)
    assert result['review']['invoice_title']=='***'
    card=operations.list('s')[0]
    assert card['review']['invoice_title']=='示例公司' and card['review']['tax_number']=='SECRET-TAX'
    replay=operations.prepare('s',rec.task_id,rec.version,'prepare_invoice',arguments=args)
    assert replay['review']['tax_number']=='***' and 'confirmation_token' not in replay
    with store.transaction(read_only=True) as tx:
        payload=tx.one('SELECT payload FROM task_operation_proposals')['payload']
        assert 'SECRET-TAX' not in payload and '示例公司' not in payload

def test_platform_from_task_requires_owned_business_evidence(store,business):
    rec,source=task(store,'s',goal='买两件耳机多少钱',details={'platform_code':'shop'})
    client=Client(response(calls=[('estimate_purchase',{'items':[{'sku_id':'s1','quantity':2}]})]),response('已完成商品金额估算。'))
    out=engine(client).execute_task(store,'alice',request(rec,source))
    assert out['evidence_ids']
    with store.transaction(read_only=True) as tx:
        row=tx.one("SELECT payload FROM support_events WHERE event_key=?",('task-tool:'+out['evidence_ids'][0],))
        receipt=json.loads(row['payload'])
        assert receipt['result']['success'] and receipt['result']['data']['goods_after_discount']=='20.00'

def test_customer_reply_projects_new_tool_results(store,business):
    from app.tasks.reply_facts import facts,question_text
    from app.tasks.replies import ReplyCoordinator
    from app.services.commerce.common import decoded,dumps
    rec,source=task(store,'s',goal='查询订单',details={'order_id':'paid','platform_code':'shop'})
    client=Client(response(calls=[('get_order',{'order_ref':ref('order','shop','paid')})]),response('已查到订单。'))
    out=engine(client).execute_task(store,'alice',request(rec,source))
    published=ReplyCoordinator(store,'alice').publish('s')
    # Publication must contain actual new-schema facts, not the no-evidence fallback.
    with store.transaction(read_only=True) as tx:
        messages=tx.all("SELECT payload FROM support_events WHERE kind='message' AND visibility='public'")
    texts='\n'.join(json.loads(r['payload']).get('content','') for r in messages)
    assert '已支付' in texts and '订单 paid' in texts
    assert '耳机' in texts and '30.00' in texts
    assert published['evidence_ids'] == out['evidence_ids']
