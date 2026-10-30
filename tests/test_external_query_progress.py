"""Query-based waits through current receipts and the production MySQL reader."""
import json

import pytest

from app.services.commerce import service_factory
from app.services.commerce.common import dumps, ok, ref, utcnow
from app.tasks.external_progress import ExternalProgress
from app.tasks.external_evidence import observation
from app.tasks.provenance import event_ref
from app.tasks.repository import TaskRepository
from tests.test_core_tools import database, svc, commit
from tests.test_external_progress import polling, read_wait, due, record
from tests.test_task_candidates import store
from tests.test_task_storage import empty_store, message


def logistics_receipt(**changes):
    row = dict(shipment_id='pack', order_id='ORD-A', platform_code='shop', tracking_number='TRACK-A',
               status='in_transit', items=[dict(order_item_id='i', sku_id='s', product_id='p', quantity=1)])
    row.update(changes)
    return ok({'shipment': row}, sources=[{'kind': 'mysql'}])


def waiting(store, receipt=None, **details):
    return read_wait(store, '跟进这票包裹', {'order_id': 'ORD-A', **details}, 'get_logistics',
                     receipt or logistics_receipt(), {'target_ref': ref('shipment', 'pack')})


class Logistics:
    def __init__(self): self.calls = 0
    def read(self, user, binding):
        self.calls += 1
        return observation(binding, phase='in_transit', rank=25, content='这票包裹正在运输。',
                           source='mysql', observed_at='2026-10-29T00:00:00Z', reference='pack')


@pytest.mark.parametrize('fault', ['old_tool', 'late', 'expired', 'controls', 'event_id', 'user',
                                  'kind', 'source', 'arguments', 'tracking', 'line', 'order', 'invalidated', 'future'])
def test_invalid_query_wait_never_calls_business(store, fault):
    current = waiting(store, item_id='i')
    key = 'task-tool:' + current.waiting_for['evidence_id']
    with store.transaction() as tx:
        row = tx.one('SELECT * FROM support_events WHERE event_key=?', (key,))
        e = json.loads(row['payload'])
        if fault == 'old_tool': e['tool'] = 'query_logistics'
        elif fault == 'late': e['late'] = True
        elif fault == 'expired': e['observed_epoch'] -= 61
        elif fault == 'future': e['result']['observed_at'] = '2099-01-01T00:00:00Z'
        elif fault == 'controls': e['controls'] = {}
        elif fault == 'event_id': e['event_id'] = 'different'
        elif fault == 'source': e['result']['sources'] = [{'kind': 'user_message'}]
        elif fault == 'arguments': e['arguments'] = {'order_id': 'ORD-A'}
        elif fault == 'tracking': e['result']['data']['shipment']['tracking_number'] = None
        elif fault == 'line': e['result']['data']['shipment']['items'][0]['order_item_id'] = 'another'
        elif fault == 'order': e['result']['data']['shipment']['order_id'] = 'other'
        elif fault == 'invalidated':
            tx.execute('INSERT INTO task_invalid_sources VALUES (?,?,?,?)',
                       ('alice', 'event', event_ref(row['session_id'], row['sequence']), 'test'))
        tx.execute('UPDATE support_events SET payload=? WHERE event_key=?', (json.dumps(e), key))
        if fault == 'kind': tx.execute("UPDATE support_events SET kind='task_operation_result' WHERE event_key=?", (key,))
        if fault == 'user': tx.execute("UPDATE support_events SET actor='user' WHERE event_key=?", (key,))
    business = Logistics()
    assert ExternalProgress(store, business=business).tick() == []
    assert business.calls == 0 and store.state('s', 'alice')['mode'] == 'handoff_pending'


@pytest.mark.parametrize('when', ['before_claim', 'in_flight'])
def test_source_retraction_fences_persisted_wait_and_late_publication(store, when):
    current = waiting(store)
    worker = ExternalProgress(store, business=Logistics())
    worker.discover(4)
    lease = worker.claim(current.task_id, 'alice') if when == 'in_flight' else None
    with store.transaction() as tx:
        row = tx.one('SELECT session_id,sequence FROM support_events WHERE event_key=?',
                     ('task-tool:' + current.waiting_for['evidence_id'],))
        tx.execute('INSERT INTO task_invalid_sources VALUES (?,?,?,?)',
                   ('alice', 'event', event_ref(row['session_id'], row['sequence']), 'test'))
    if lease:
        assert worker.apply(lease, worker.business.read('alice', json.loads(lease['binding']))) == 'stale'
    else:
        assert worker.claim(current.task_id, 'alice') is None
    assert record(store, current.task_id).status == 'waiting_external'
    assert store.state('s', 'alice')['mode'] == 'handoff_pending'


def test_unchanged_wait_survives_source_age_restart_and_duplicate_poll(store):
    current = waiting(store)
    worker = ExternalProgress(store, business=Logistics())
    assert worker.tick() == ['updated']
    with store.transaction() as tx:
        row = tx.one('SELECT payload FROM support_events WHERE event_key=?', ('task-tool:' + current.waiting_for['evidence_id'],))
        e = json.loads(row['payload']); e['observed_epoch'] -= 3600
        tx.execute('UPDATE support_events SET payload=? WHERE event_key=?', (json.dumps(e), 'task-tool:' + e['event_id']))
    history = store.public_history('s', 'alice')
    due(store)
    assert ExternalProgress(store, business=Logistics()).tick() == ['unchanged']
    assert store.public_history('s', 'alice') == history


def test_legacy_persisted_query_wait_is_not_silently_retargeted(store):
    current = waiting(store)
    worker = ExternalProgress(store, business=Logistics()); worker.discover(4)
    with store.transaction() as tx:
        tx.execute('UPDATE task_external_waits SET binding=?', (json.dumps({'kind': 'logistics', 'order_id': 'ORD-A'}),))
    assert worker.tick() == [] and worker.business.calls == 0
    assert store.state('s', 'alice')['mode'] == 'handoff_pending'


@pytest.mark.parametrize('paged', [False, True])
def test_order_query_does_not_choose_first_of_multiple_packages(store, paged):
    row = logistics_receipt()['data']['shipment']
    result = ok({'items': [row] if paged else [row, {**row, 'shipment_id': 'second', 'tracking_number': 'TRACK-B'}],
                 'total': 2}, sources=[{'kind': 'mysql'}], next_cursor='next' if paged else None)
    read_wait(store, '跟进订单物流', {'order_id': 'ORD-A'}, 'get_logistics', result,
              {'target_ref': ref('order', 'shop', 'ORD-A')})
    business = Logistics()
    assert ExternalProgress(store, business=business).tick() == [] and business.calls == 0


def stored_read(svc, name, args):
    return json.loads(dumps(svc.read(name, args)))


def create_sale(svc):
    # A historical/imported application without a local operation or reserved
    # refund. These tests deliberately inject partial/malformed channel records.
    from uuid import uuid4
    aid=uuid4().hex
    with svc.store.transaction(write=True) as tx:
        tx.execute('''INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,requested_amount,created_at,updated_at)
            VALUES (%s,'shop','paid','alice','refund_only','pending_review','查询历史申请',10,%s,%s)''',(aid,utcnow(),utcnow()))
        tx.execute("INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity,requested_amount) VALUES (%s,'i-paid',1,10)",(aid,))
    return aid


@pytest.mark.parametrize('name', ['get_after_sale_status', 'get_refunds'])
def test_real_query_refund_wait_uses_no_operation_id_and_only_full_channel_receipt_completes(store, svc, name):
    aid = create_sale(svc)
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO refund_transactions(refund_id,after_sale_id,amount,currency,status,requested_at) VALUES ('r',%s,5,'CNY','processing',%s)", (aid, utcnow()))
    args = {'after_sale_id': aid} if name == 'get_after_sale_status' else {'target_ref': ref('after_sale', aid)}
    current = read_wait(store, '跟进这笔退款的处理结果', {'order_id': 'paid', 'platform_code': 'shop', 'item_id': 'i-paid', 'quantity': 1},
                        name, stored_read(svc, name, args), args)
    with service_factory(lambda user, scope: svc):
        assert ExternalProgress(store).tick() == ['updated']
        with svc.store.transaction(write=True) as tx:
            tx.execute("UPDATE refund_transactions SET status='succeeded',provider_refund_id='channel-r',completed_at=%s", (utcnow(),))
        due(store)
        assert ExternalProgress(store).tick() == ['unchanged']  # only half the requested amount
        with svc.store.transaction(write=True) as tx:
            tx.execute('UPDATE refund_transactions SET amount=10')
        due(store)
        assert ExternalProgress(store).tick() == ['completed']
    final = record(store, current.task_id)
    assert final.status == 'completed' and 'operation_id' not in final.waiting_for
    assert '银行卡实际入账尚未核实' in store.public_history('s', 'alice')[-1]['content']


def test_real_logistics_query_tracks_only_selected_package_and_survives_restart(store, svc):
    from app.support.store import SupportStore
    with svc.store.transaction(write=True) as tx:
        for sid in ('pack', 'second'):
            tx.execute("INSERT INTO shipments(shipment_id,platform_code,order_id,tracking_number,status,created_at,shipped_at) VALUES (%s,'shop','paid',%s,'in_transit',%s,%s)", (sid, 'TRACK-'+sid, utcnow(), utcnow()))
            tx.execute("INSERT INTO shipment_items(shipment_id,order_item_id,quantity) VALUES (%s,'i-paid',1)", (sid,))
    args = {'target_ref': ref('order', 'shop', 'paid')}
    current = read_wait(store, '跟进指定运单签收', {'order_id': 'paid', 'tracking_number': 'TRACK-pack', 'sku_id': 's1'},
                        'get_logistics', stored_read(svc, 'get_logistics', args), args)
    with service_factory(lambda user, scope: svc):
        assert ExternalProgress(store).tick() == ['updated']
        with svc.store.transaction(write=True) as tx:
            tx.execute("UPDATE shipments SET status='delivered',delivered_at=%s WHERE shipment_id='second'", (utcnow(),))
        due(store)
        assert ExternalProgress(SupportStore(store.dsn)).tick() == ['unchanged']
        with svc.store.transaction(write=True) as tx:
            tx.execute("UPDATE shipments SET status='delivered',delivered_at=%s WHERE shipment_id='pack'", (utcnow(),))
        due(store)
        assert ExternalProgress(store).tick() == ['completed']
    assert record(store, current.task_id).status == 'completed'
    assert '仅代表这一票包裹' in store.public_history('s', 'alice')[-1]['content']


@pytest.mark.parametrize('fault', ['no_provider', 'no_time', 'currency', 'pagination', 'bank_goal'])
def test_real_refund_proof_and_completion_limits(store, svc, monkeypatch, fault):
    aid = create_sale(svc)
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO refund_transactions(refund_id,after_sale_id,amount,currency,status,requested_at,provider_refund_id,completed_at) VALUES ('r',%s,10,'CNY','succeeded',%s,%s,%s)",
                   (aid, utcnow(), None if fault == 'no_provider' else 'channel-r', None if fault == 'no_time' else utcnow()))
        if fault == 'currency': tx.execute("UPDATE refund_transactions SET currency='USD'")
    args = {'target_ref': ref('after_sale', aid)}
    goal = '跟进退款到账' if fault == 'bank_goal' else '跟进这笔退款处理结果'
    current = read_wait(store, goal, {'order_id': 'paid', 'platform_code': 'shop'}, 'get_refunds',
                        stored_read(svc, 'get_refunds', args), args)
    original = svc.read
    def read(name, arguments):
        result = original(name, arguments)
        if fault == 'pagination' and name == 'get_refunds': result['next_cursor'] = 'more'
        return result
    monkeypatch.setattr(svc, 'read', read)
    with service_factory(lambda user, scope: svc):
        assert ExternalProgress(store).tick() == ['updated']
    assert record(store, current.task_id).status == 'waiting_external'
    assert store.state('s', 'alice')['mode'] == ('handoff_pending' if fault == 'bank_goal' else 'bot')


@pytest.mark.parametrize('change', ['order', 'goal', 'control'])
def test_changes_during_read_do_not_publish_old_query_progress(store, change):
    current = waiting(store)
    worker = ExternalProgress(store, business=Logistics()); worker.discover(4)
    lease = worker.claim(current.task_id, 'alice')
    if change == 'control':
        with store.transaction() as tx:
            tx.execute("UPDATE support_conversations SET control_version=control_version+1 WHERE session_id='s'")
    else:
        patch = {'details': {'order_id': 'OTHER'}} if change == 'order' else {'goal': '改为跟进另一事项'}
        TaskRepository(store, 'alice').update(current.task_id, 's', expected_version=current.version,
            change_id='change', source=message(store, 's', 'alice', '更正'), patch=patch)
    history = store.public_history('s', 'alice')
    assert worker.apply(lease, worker.business.read('alice', json.loads(lease['binding']))) == 'stale'
    # A control change may emit a handoff notice, but never business progress.
    assert not any('这票包裹正在运输' in m['content'] for m in store.public_history('s', 'alice')[len(history):])


def test_real_return_shipment_retains_sale_scope_and_delivery_is_not_refund_completion(store, svc):
    aid = create_sale(svc)
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO return_authorizations VALUES ('auth',%s,'ciphertext',1,'[]','[]',%s,'active','warehouse',%s)", (aid, utcnow(), utcnow()))
        tx.execute("INSERT INTO return_shipments VALUES ('returned','auth','carrier','RETURN-A','in_transit','return-op',%s,%s)", (utcnow(), utcnow()))
        tx.execute("INSERT INTO return_shipment_items VALUES ('returned',%s,'i-paid',1)", (aid,))
    args = {'target_ref': ref('return_shipment', 'returned')}
    receipt = stored_read(svc, 'get_logistics', args)
    assert receipt['data']['shipment']['after_sale_id'] == aid
    current = read_wait(store, '跟进退货物流及退款', {'order_id': 'paid', 'after_sale_id': aid, 'item_id': 'i-paid'},
                        'get_logistics', receipt, args)
    with service_factory(lambda user, scope: svc):
        assert ExternalProgress(store).tick() == ['updated']
        with svc.store.transaction(write=True) as tx:
            tx.execute("UPDATE return_shipments SET status='delivered'")
            tx.execute("INSERT INTO return_shipment_events VALUES ('ev','returned','delivered','签收','carrier',%s)", (utcnow(),))
        due(store)
        assert ExternalProgress(store).tick() == ['updated']
    assert record(store, current.task_id).status == 'waiting_external'
    assert store.state('s', 'alice')['mode'] == 'handoff_pending'


def test_real_future_delivery_timestamp_cannot_complete_wait(store, svc):
    from datetime import timedelta
    with svc.store.transaction(write=True) as tx:
        tx.execute("INSERT INTO shipments(shipment_id,platform_code,order_id,tracking_number,status,created_at,shipped_at,delivered_at) VALUES ('pack','shop','paid','TRACK','delivered',%s,%s,%s)",
                   (utcnow(), utcnow(), utcnow() + timedelta(days=1)))
    args = {'target_ref': ref('shipment', 'pack')}
    current = read_wait(store, '跟进这票物流', {'order_id': 'paid'}, 'get_logistics', stored_read(svc, 'get_logistics', args), args)
    with service_factory(lambda user, scope: svc):
        assert ExternalProgress(store).tick() == ['retry']
    assert record(store, current.task_id).status == 'waiting_external'
