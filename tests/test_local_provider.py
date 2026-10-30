# 文件说明：测试本地模拟渠道的持久事务、重复请求、故障恢复与私有票据。
"""The real MySQL application/provider transactions, files, and worker recovery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path

import pytest

from app.config.settings import settings
from app.services.commerce import CommerceService, service_factory
from app.services.commerce.common import BusinessError, decoded, dumps, ref, utcnow
from app.services.commerce.providers import gateway, invoice_file
from app.tasks.external_evidence import BusinessProgress, finished
from scripts.commerce_worker import run_once
from tests.test_core_tools import database, svc, commit, read
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store


@pytest.fixture
def demo(svc, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'commerce_provider_mode', 'simulator')
    monkeypatch.setattr(settings, 'commerce_simulator_scenario', 'success')
    monkeypatch.setattr(settings, 'commerce_simulator_step_seconds', 0)
    monkeypatch.setattr(settings, 'commerce_invoice_root', str(tmp_path / 'invoices'))
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE orders SET is_demo=TRUE WHERE user_id='alice'")
        tx.execute('''INSERT INTO payment_transactions(payment_id,platform_code,order_id,user_id,provider,provider_txn_id,amount,currency,status,created_at)
            VALUES ('pay','shop','paid','alice','demo','demo-paid',30,'CNY','succeeded',%s)''', (utcnow(),))
    return svc


def apply(svc, kind='refund_only', oid='demo-sale', quantity=1):
    return commit(svc, 'prepare_after_sale', dict(kind=kind, items=[dict(order_item_id='i-paid', quantity=quantity)], reason='演示申请'), oid)


def tick(svc, count=1):
    # The actual worker uses service(user), and production SQL to scan pending jobs.
    with service_factory(lambda user, scope: svc):
        for _ in range(count): assert run_once(svc.store)['errors'] == 0


def query_progress(svc, result):
    key = 'invoice_id' if 'invoice_id' in result else 'after_sale_id'
    binding = dict(kind='commerce_invoice' if key == 'invoice_id' else 'commerce_sale',
        operation_id=result['request_id'], order_id='paid', platform_code='shop',
        completion='commerce_result', **{key: result[key]})
    with service_factory(lambda user, scope: svc):
        progress = BusinessProgress().read('alice', binding)
    assert progress['binding']==binding
    return binding, progress


def test_refund_worker_idempotence_restart_and_notifications(demo):
    args = dict(kind='refund_only', items=[dict(order_item_id='i-paid', quantity=1)], reason='演示申请')
    preview = demo.preview('prepare_after_sale', args)
    assert preview['review']['is_demo'] and '真实资金' in preview['review']['impact']
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: demo.submit('prepare_after_sale', preview['arguments'], 'demo-sale', preview['business_version']), range(2)))
    result = results[0]; assert results[0] == results[1] and result['is_demo']
    assert not finished(*query_progress(demo, result))
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda _: demo.process_provider_request('demo-sale'), range(3)))
    tick(demo, 5)
    sale = read(demo, 'get_after_sale_status', after_sale_id=result['after_sale_id'])
    assert sale['data']['after_sale']['status'] == 'completed' and sale['is_demo']
    refunds = read(demo, 'get_refunds', target_ref=ref('after_sale', result['after_sale_id']))
    assert refunds['data']['total'] == 1 and refunds['data']['recorded_success_amount'] == 10
    assert refunds['data']['items'][0]['source'] == 'simulated_provider'
    binding, progress = query_progress(demo, result)
    assert finished(binding, progress) and progress['is_demo']
    assert '未发生真实资金流转' in progress['content']
    with demo.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM simulated_provider_requests')['n'] == 1
        job = tx.one('SELECT * FROM commerce_provider_requests WHERE request_id=%s', ('demo-sale',))
        payload = decoded(demo.cipher.open(job['payload_ciphertext'], 'provider:demo-sale', job['key_version']))
    # New adapter instances recover exactly the persisted response after completion.
    one = gateway('refund_only', store=demo.store, user='alice').call('query', 'demo-sale', 'refund_only', payload)
    two = gateway('refund_only', store=demo.store, user='alice').call('query', 'demo-sale', 'refund_only', payload)
    assert one == two
    with pytest.raises(BusinessError):
        gateway('refund_only', store=demo.store, user='alice').call('submit', 'demo-sale', 'refund_only', dict(payload, amount='1.00'))


@pytest.mark.parametrize('scenario', ['reject', 'delay', 'lost_response'])
def test_scenarios_are_persistent_and_recover_original_request(demo, monkeypatch, scenario):
    monkeypatch.setattr(settings, 'commerce_simulator_scenario', scenario)
    if scenario == 'delay': monkeypatch.setattr(settings, 'commerce_simulator_step_seconds', 60)
    result = apply(demo); tick(demo)
    with demo.store.transaction() as tx:
        job = tx.one('SELECT * FROM commerce_provider_requests')
        assert job['status'] == ('unknown' if scenario == 'lost_response' else 'accepted')
    monkeypatch.setattr(settings, 'commerce_simulator_scenario', 'success')
    if scenario == 'delay':
        tick(demo)
        with demo.store.transaction(write=True) as tx:
            assert tx.one('SELECT phase FROM simulated_provider_requests')['phase'] == 0
            tx.execute('UPDATE simulated_provider_requests SET next_at=%s,step_seconds=0', (utcnow() - timedelta(seconds=1),))
    tick(demo, 3)
    sale = read(demo, 'get_after_sale_status', after_sale_id=result['after_sale_id'])['data']['after_sale']
    assert sale['status'] == ('rejected' if scenario == 'reject' else 'completed')
    with demo.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM simulated_provider_requests')['n'] == 1
        assert tx.one("SELECT COUNT(*) n FROM refund_transactions WHERE status='succeeded'")['n'] == (0 if scenario == 'reject' else 1)


def test_first_invoice_generates_owned_verified_demo_pdf(demo):
    preview = demo.preview('prepare_invoice', dict(order_ref=ref('order','shop','paid'), invoice_type='personal', title='演示用户'))
    assert preview['review']['amount'] == '30.00'
    assert preview['review']['currency'] == 'CNY' and preview['review']['amount_kind'] == 'invoice_total'
    result = commit(demo, 'prepare_invoice', dict(order_ref=ref('order','shop','paid'), invoice_type='personal', title='演示用户'), 'demo-invoice')
    tick(demo, 3)
    invoices = read(demo, 'get_invoices', target_ref=ref('invoice', result['invoice_id']))
    row = invoices['data']['items'][0]; doc = row['issued_invoices'][0]
    assert row['status'] == 'issued' and row['is_demo'] and doc['document_available']
    with demo.store.transaction() as tx:
        stored, _ = demo.issued_invoice(tx, doc['issued_invoice_id'])
        path = invoice_file(stored)
    assert path.read_bytes().startswith(b'%PDF-')
    assert read(demo, 'get_invoice_document', issued_invoice_id=doc['issued_invoice_id'])['is_demo']
    binding, progress = query_progress(demo, result)
    assert finished(binding, progress) and '演示票据' in progress['content']
    other = CommerceService(demo.store, 'bob')
    with other.store.transaction() as tx, pytest.raises(BusinessError): other.issued_invoice(tx, doc['issued_invoice_id'])
    tick(demo, 2)
    with demo.store.transaction() as tx: assert tx.one('SELECT COUNT(*) n FROM issued_invoices')['n'] == 1
    path.write_bytes(b'corrupt')
    with pytest.raises(BusinessError): read(demo, 'get_invoice_document', issued_invoice_id=doc['issued_invoice_id'])
    assert not read(demo, 'get_invoices', target_ref=ref('invoice',result['invoice_id']))['data']['items'][0]['issued_invoices'][0]['document_available']
    assert not finished(*query_progress(demo, result))


def test_file_written_before_provider_commit_recovers_same_document(demo, monkeypatch):
    from app.services.commerce import simulator
    original=simulator.invoice_document
    calls=[]
    def interrupt(*args):
        result=original(*args); calls.append(result)
        if len(calls)==1: raise RuntimeError('process stopped after file write')
        return result
    monkeypatch.setattr(simulator,'invoice_document',interrupt)
    result=commit(demo,'prepare_invoice',dict(order_ref=ref('order','shop','paid'),invoice_type='personal',title='演示抬头'),'interrupted-file')
    tick(demo,3)
    with demo.store.transaction() as tx:
        assert tx.one('SELECT COUNT(*) n FROM issued_invoices')['n']==0
        assert tx.one('SELECT status FROM commerce_provider_requests')['status']=='unknown'
    tick(demo)
    assert calls[0]==calls[1]
    assert len(list(Path(settings.commerce_invoice_root).rglob('*.pdf')))==1
    assert finished(*query_progress(demo,result))


def test_invoice_download_api_ownership_and_missing_file(demo,monkeypatch):
    from contextlib import closing
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.api.auth import create_access_token
    monkeypatch.setattr(settings,'api_auth_backend','env')
    monkeypatch.setattr(settings,'api_jwt_secret','demo-test-signing-key-longer-than-32-bytes')
    result=commit(demo,'prepare_invoice',dict(order_ref=ref('order','shop','paid'),invoice_type='personal',title='演示抬头'),'api-invoice')
    tick(demo,3)
    doc=read(demo,'get_invoices',target_ref=ref('invoice',result['invoice_id']))['data']['items'][0]['issued_invoices'][0]
    url='/api/commerce/invoices/'+doc['issued_invoice_id']+'/document'
    def factory(user,scope):
        return demo if user=='alice' else CommerceService(demo.store,user)
    # Exercise the actual auth middleware/router, without starting unrelated
    # PostgreSQL workers and RAG model loading in the app lifespan.
    with service_factory(factory), closing(TestClient(app)) as client:
        assert client.get(url).status_code==401
        assert client.get(url,headers={'Authorization':'Bearer '+create_access_token('bob')}).status_code==404
        status_url='/api/commerce/invoice-requests/'+result['invoice_id']
        assert client.get(status_url,headers={'Authorization':'Bearer '+create_access_token('bob')}).status_code==404
        status=client.get(status_url,headers={'Authorization':'Bearer '+create_access_token('alice')})
        assert status.status_code==200 and status.json()['is_demo']
        assert status.json()['issued_invoices'][0]['document_available']
        response=client.get(url,headers={'Authorization':'Bearer '+create_access_token('alice')})
        assert response.status_code==200 and response.content.startswith(b'%PDF-')
        with demo.store.transaction() as tx: stored,_=demo.issued_invoice(tx,doc['issued_invoice_id'])
        invoice_file(stored).unlink()
        assert client.get(url,headers={'Authorization':'Bearer '+create_access_token('alice')}).status_code==404


def test_return_refund_requires_all_lines_and_quantities(demo):
    result = apply(demo, kind='return_refund', quantity=2); aid = result['after_sale_id']
    tick(demo, 2)
    auth = read(demo, 'get_return_instructions', after_sale_id=aid)
    assert auth['is_demo'] and '请勿实际寄件' in auth['data']['address']['detail']
    for number in (1, 2):
        receipt = commit(demo, 'prepare_return_shipment', dict(after_sale_id=aid, carrier_code='DEMO', tracking_number='DEMO-' + str(number),
            items=[dict(order_item_id='i-paid',quantity=1)]), 'return-' + str(number))
        assert receipt['is_demo']
        logistics = read(demo, 'get_logistics', target_ref=ref('return_shipment',receipt['return_shipment_id']))
        assert logistics['is_demo'] and logistics['data']['shipment']['is_demo']
        tick(demo, 2)
        binding = dict(kind='query_logistics', target_ref=logistics['data']['shipment']['target_ref'],
            tracking_number='DEMO-' + str(number), after_sale_id=aid, scope_details={}, platform_code='shop', order_id='paid')
        with service_factory(lambda user, scope: demo):
            progress = BusinessProgress().read('alice',binding)
        assert progress['is_demo'] and progress['source']=='simulated_provider' and '本地模拟演示' in progress['content']
        tick(demo, 3)
        status = read(demo, 'get_after_sale_status', after_sale_id=aid)['data']['after_sale']['status']
        assert status == ('processing' if number == 1 else 'completed')
    refund = read(demo, 'get_refunds', target_ref=ref('after_sale',aid))
    assert refund['data']['recorded_success_amount'] == 20


def test_channel_isolation_disabled_and_atomic_rollback(demo, monkeypatch):
    with demo.store.transaction(write=True) as tx: tx.execute("UPDATE orders SET is_demo=FALSE WHERE order_id='paid'")
    with pytest.raises(BusinessError, match='演示订单'): apply(demo)
    with demo.store.transaction(write=True) as tx: tx.execute("UPDATE orders SET is_demo=TRUE WHERE order_id='paid'")
    original = demo.queue_provider
    def fail(*args, **kwargs): raise RuntimeError('queue interrupted')
    monkeypatch.setattr(demo, 'queue_provider', fail)
    with pytest.raises(RuntimeError): apply(demo)
    with demo.store.transaction() as tx:
        for table in ('after_sale_requests','refund_transactions','business_operation_receipts','service_work_orders'):
            assert tx.one('SELECT COUNT(*) n FROM ' + table)['n'] == 0
    monkeypatch.setattr(demo, 'queue_provider', original)
    apply(demo)
    monkeypatch.setattr(settings, 'commerce_provider_mode', 'http')
    tick(demo)
    with demo.store.transaction() as tx:
        assert tx.one('SELECT error_code FROM commerce_provider_requests')['error_code'] == 'provider_mode_mismatch'
        assert tx.one('SELECT COUNT(*) n FROM simulated_provider_requests')['n'] == 0
    monkeypatch.setattr(settings, 'commerce_provider_mode', 'disabled')
    with pytest.raises(BusinessError, match='未配置'): apply(demo, oid='other')


@pytest.mark.parametrize('fault',['amount','order','items','missing_refund'])
def test_invalid_complete_response_keeps_unknown_and_no_side_effects(demo,fault):
    result = apply(demo); tick(demo, 2)
    with demo.store.transaction(write=True) as tx:
        row = tx.one('SELECT * FROM simulated_provider_requests')
        response = decoded(row['result'])
        response.update(status='completed', refunds=[dict(payment_id='pay',provider_refund_id='bad',amount='10.00',currency='CNY',completed_at=utcnow().isoformat()+'Z')])
        if fault=='amount': response['refunds'][0]['amount']='99.00'
        elif fault=='order': response['order_id']='wrong-order'
        elif fault=='items': response['items'][0]['quantity']=2
        else: response['refunds']=[]
        tx.execute('UPDATE simulated_provider_requests SET result=%s', (dumps(response),))
    tick(demo)
    with demo.store.transaction() as tx:
        assert tx.one('SELECT status,error_code FROM commerce_provider_requests') == dict(status='unknown',error_code='insufficient_data')
        assert tx.one("SELECT COUNT(*) n FROM refund_transactions WHERE status='succeeded'")['n'] == 0


def test_http_request_never_moves_into_simulator(demo,monkeypatch):
    monkeypatch.setattr(settings,'commerce_provider_mode','http')
    monkeypatch.setattr(settings,'commerce_provider_config',json.dumps({'refund_only':{'url':'https://unused.invalid','token':'test'}}))
    with demo.store.transaction(write=True) as tx:
        order = demo.owned_order(tx,ref('order','shop','paid'))
        with pytest.raises(BusinessError, match='演示订单'):
            demo.queue_provider(tx,'blocked-demo','refund_only','target',order,{})
        tx.execute('UPDATE orders SET is_demo=FALSE')
    result=apply(demo)
    assert not result.get('is_demo')
    monkeypatch.setattr(settings,'commerce_provider_mode','simulator')
    tick(demo)
    with demo.store.transaction() as tx:
        assert tx.one('SELECT provider_mode,error_code,attempts FROM commerce_provider_requests')==dict(provider_mode='http',error_code='provider_mode_mismatch',attempts=0)
        assert tx.one('SELECT COUNT(*) n FROM simulated_provider_requests')['n']==0


def test_migration_012_is_resumable(database):
    import pymysql
    from app.data.order_dao import parse_mysql_dsn
    from scripts.migrate_mysql_business_schema import _apply_012
    conn=pymysql.connect(**parse_mysql_dsn(database.dsn),cursorclass=pymysql.cursors.DictCursor)
    try:
        _apply_012(conn); _apply_012(conn)
    finally: conn.close()


@pytest.mark.parametrize('handoff',[False,True])
def test_confirmation_worker_progress_and_handoff_use_real_business_receipts(demo,store,monkeypatch,handoff):
    from app.tasks.operations import TaskOperations
    from app.tasks.external_progress import ExternalProgress
    from app.tasks.repository import TaskRepository
    from tests.test_task_execution import Client,response,request,engine
    monkeypatch.setattr(settings,'task_operations_enabled',True)
    monkeypatch.setattr(settings,'api_jwt_secret','demo-confirmation-test-key-longer-than-32-bytes')
    monkeypatch.setattr(settings,'task_external_progress_enabled',True)
    monkeypatch.setattr(settings,'task_external_interval_seconds',1)
    monkeypatch.setattr(settings,'tool_allowlist','*')
    rec,source=task(store,'s',goal='跟进仅退款的处理结果',details={'order_id':'paid','platform_code':'shop'})
    args=dict(kind='refund_only',items=[dict(order_item_id='i-paid',quantity=1)],reason='演示申请')
    with service_factory(lambda user,scope:demo):
        out=engine(Client(response(calls=[('prepare_after_sale',args)]))).execute_task(store,'alice',request(rec,source))
        assert out['state']=='needs_confirmation'
        operations=TaskOperations(store,'alice'); card=operations.list('s')[0]
        assert card['review']['is_demo']
        assert operations.confirm('s',card['operation_id'],card['confirmation_token'])['status']=='succeeded'
        assert TaskRepository(store,'alice').get(rec.task_id).status=='waiting_external'
        if handoff:
            store.seed_staff('demo-agent','agent'); ticket=store.handoff('s','alice')['ticket']
            store.change(ticket['ticket_id'],'demo-agent',ticket['version'],'claim')
        history=store.public_history('s','alice')
        tick(demo,3)
        progress=ExternalProgress(store)
        results=progress.tick()
        if handoff:
            assert TaskRepository(store,'alice').get(rec.task_id).status!='completed'
            assert store.public_history('s','alice')==history
            view=store.detail(ticket['ticket_id'],'demo-agent')['task_handovers'][0]['handover']
            assert view['operations'][0]['result']['is_demo']
        else:
            assert results==['completed']
            assert TaskRepository(store,'alice').get(rec.task_id).status=='completed'
            assert '未发生真实资金流转' in store.public_history('s','alice')[-1]['content']
            before=store.public_history('s','alice'); progress.tick()
            assert store.public_history('s','alice')==before


def test_demo_script_and_worker_run_in_separate_processes(demo):
    import os
    import subprocess
    import sys
    env=dict(os.environ,PYTHONIOENCODING='utf-8',ORDER_DB_DSN=demo.store.dsn,COMMERCE_PROVIDER_MODE='simulator',
        COMMERCE_SIMULATOR_SCENARIO='success',COMMERCE_SIMULATOR_STEP_SECONDS='0',
        COMMERCE_INVOICE_ROOT=settings.commerce_invoice_root,API_JWT_SECRET=demo.secret,
        BUSINESS_DATA_KEYS=json.dumps(demo.cipher.keys),BUSINESS_DATA_KEY_VERSION='1')
    seeded=subprocess.run([sys.executable,'scripts/demo_commerce.py','--user','alice','--submit'],env=env,capture_output=True,text=True,encoding='utf-8',check=True)
    value=json.loads(seeded.stdout)
    assert len(value['submitted'])==3
    for _ in range(2):
        worker=subprocess.run([sys.executable,'scripts/commerce_worker.py','--once'],env=env,capture_output=True,text=True,encoding='utf-8',check=True)
        assert "'errors': 0" in worker.stdout
    with demo.store.transaction() as tx:
        assert tx.one("SELECT COUNT(*) n FROM commerce_provider_requests WHERE status='completed'")['n']==2
        assert tx.one("SELECT COUNT(*) n FROM commerce_provider_requests WHERE status='processing'")['n']==1
        assert tx.one("SELECT is_demo FROM orders WHERE order_id='other'")['is_demo']==0
