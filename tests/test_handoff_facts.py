# 文件说明：测试工单交接事实的对象范围、限定语、有界投影和敏感字段隔离。
"""RUN-03: current receipts retain their scope and caveats in real ticket views."""
import json

import pytest

from app.services.commerce.common import ok, ref
from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.handoff_facts import observation, operation_result
from app.tasks.provenance import event_ref
from tests.test_task_candidates import task, store
from tests.test_task_storage import empty_store
from tests.test_task_execution import request
from tests.test_image_context import images, analyzed
from tests.test_task_operations import Business, ready, confirm, flags


def result(data, **extra):
    return {**ok(data, sources=[{'kind': 'mysql', 'tables': ['orders']}],
                 limitations=['读取时刻不代表上游同步时刻']), **extra}


def save(store, record, source, receipts):
    runs = TaskRuns(store, record.user_id)
    req = TaskExecutionInput.model_validate(request(record, source))
    reservation, _ = runs.begin(req)
    saved = [runs.save_tool(req, reservation['payload'], i, name, args, value)
             for i, (name, args, value) in enumerate(receipts)]
    runs.finish(req, reservation['payload'], dict(state='ready', reply='', task_version=record.version))
    return saved


def handover(store):
    store.seed_staff('agent1', 'agent')
    ticket = store.handoff('s', 'alice')['ticket']
    store.change(ticket['ticket_id'], 'agent1', ticket['version'], 'claim')
    return ticket['ticket_id'], store.detail(ticket['ticket_id'], 'agent1')['task_handovers'][0]['handover']


def test_ticket_preserves_candidates_order_parcels_and_after_sale_without_flattening(store):
    record, source = task(store, 's', goal='核实订单物流和售后')
    order_ref = ref('order', 'shop', 'ORD-A')
    candidates = [dict(order_id='ORD-A', platform_code='shop', status='paid', currency='CNY', payable_amount='30.00'),
                  dict(order_id='ORD-B', platform_code='shop', status='cancelled', currency='USD', payable_amount='50.00')]
    parcels = [dict(shipment_id='S1', tracking_number='T1', status='delivered', shipment_kind='original',
                    items=[dict(order_item_id='I1', quantity=1)], events=[dict(event_time='2026-10-28T01:00:00Z', status='delivered')]),
               dict(shipment_id='S2', tracking_number='T2', status='in_transit', shipment_kind='exchange',
                    after_sale_id='A1', items=[dict(order_item_id='I1', quantity=1)])]
    after_sale = dict(after_sale=dict(after_sale_id='A1', order_id='ORD-A', status='processing', kind='return_refund'),
                      purchase_items=[dict(order_item_id='I1', quantity=1)],
                      refund_summary={'CNY': {'succeeded': '10.00', 'processing': '20.00'}},
                      return_authorization=dict(authorization_id='AUTH1', deadline='2026-11-05', status='active'),
                      replacement_shipments=[parcels[1]], processes_connected={'return': True, 'service': False})
    saved = save(store, record, source, [
        ('find_orders', {'query': '耳机'}, result(dict(items=candidates, total=3), next_cursor='cursor')),
        ('get_order', {'order_ref': order_ref}, result({**candidates[0], 'paid_amount': '30.00',
            'items': [dict(order_item_id='I1', quantity=3, payable_amount='30.00')], 'item_count': 1})),
        ('get_logistics', {'target_ref': order_ref}, result(dict(items=parcels, total=2,
            quantities=[dict(order_item_id='I1', purchased=3, recorded_shipped=1, recorded_replacement_shipped=1)]))),
        ('get_after_sale_status', {'after_sale_id': 'A1'}, result(after_sale,
            limitations=['渠道成功不代表已核实银行卡入账'], missing_fields=['work_order'], completeness='partial')),
    ])
    tid, view = handover(store)
    facts = {e['tool']: e for e in view['observed_evidence']}
    assert facts['find_orders']['facts']['items'] == candidates
    assert facts['find_orders']['evidence_kind'] == 'candidates'
    assert facts['find_orders']['has_more'] and 'order_id' not in facts['find_orders']['facts']
    assert facts['get_order']['facts']['paid_amount'] == '30.00'
    assert facts['get_logistics']['facts']['items'] == parcels
    assert facts['get_logistics']['facts']['quantities'][0]['purchased'] == 3
    assert facts['get_after_sale_status']['facts'] == after_sale
    assert facts['get_after_sale_status']['limitations'] == ['渠道成功不代表已核实银行卡入账']
    assert facts['get_after_sale_status']['missing_fields'] == ['work_order']
    for evidence in facts.values():
        assert evidence['sources'] == [{'kind': 'mysql', 'tables': ['orders']}]
        assert evidence['observed_at'] and evidence['result_observed_at'] and evidence['data_as_of'] is None
        assert evidence['session_id'] == 's' and evidence['sequence'] > 0
        assert evidence['task_version'] == 1
    assert facts['get_logistics']['query_scope'] == {'target_ref': order_ref}
    # Release freezes the same new-schema projection, independently of later events.
    store.resume('s', 'alice')
    frozen = store.detail(tid, 'agent1')['task_handovers'][0]
    assert not frozen['active'] and frozen['handover']['observed_evidence'] == view['observed_evidence']
    assert {e['event_id'] for e in view['observed_evidence']} == {e['event_id'] for e in saved}


@pytest.mark.parametrize('invalidity', ['event', 'decision', 'late'])
def test_invalid_or_late_receipts_do_not_become_handover_facts(store, invalidity):
    record, source = task(store, 's')
    receipt = save(store, record, source, [('get_order', {'order_ref': 'ref'}, result({'order_id': 'ORD-A'}))])[0]
    with store.transaction() as tx:
        row = tx.one("SELECT sequence FROM support_events WHERE event_key=?", ('task-tool:' + receipt['event_id'],))
        if invalidity == 'event':
            tx.execute('INSERT INTO task_invalid_sources VALUES (?,?,?,?)', ('alice', 'event', event_ref('s', row['sequence']), 'correction'))
        else:
            if invalidity == 'late':
                receipt['late'] = True
            else:
                receipt['provenance']['decisions'] = ['revoked']
                tx.execute('INSERT INTO task_invalid_sources VALUES (?,?,?,?)', ('alice', 'decision', 'revoked', 'correction'))
            tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
                       (json.dumps(receipt), 's', row['sequence']))
    assert handover(store)[1]['observed_evidence'] == []


def test_projection_is_bounded_and_never_exposes_private_receipt_fields():
    projected = observation(dict(tool='get_order', result=result(dict(
        order_id='ORD-A', phone='private-phone', details_ciphertext='private-cipher',
        items=[dict(order_item_id=str(i), quantity=1) for i in range(15)], item_count=15,
        spec_values={'颜色': '蓝色', 'secret': 'private-secret'},
        description='x' * 2100))))
    assert len(projected['facts']['items']) == 10 and projected['facts']['item_count'] == 15
    assert {'data.items', 'data.description'} <= set(projected['summary_omitted_paths'])
    assert 'private-' not in json.dumps(projected)
    assert projected['facts']['spec_values'] == {'颜色': '蓝色'}


def test_failed_empty_and_preparation_receipts_do_not_claim_business_success():
    failed = observation(dict(tool='get_order', result=dict(success=False, data={'status': 'paid'})))
    assert not failed['success'] and failed['facts'] == {}
    assert '不代表没有业务记录' in failed['note']
    empty = observation(dict(tool='future_tool', result=result({'status': 'paid'})))
    assert not empty['summary_supported'] and empty['facts'] == {}
    prepared = observation(dict(tool='prepare_after_sale', result=result(dict(next_state='needs_confirmation', operation_id='OP1'))))
    assert prepared['evidence_kind'] == 'operation_preparation'
    assert '尚未提交' in prepared['note']
    operation, _ = operation_result(dict(success=True, status='submitted', order_id='ORD-A', after_sale_id='A1',
                                         request_id='OP1', confirmation_token='private-token'))
    assert operation == dict(success=True, status='submitted', order_id='ORD-A', after_sale_id='A1', request_id='OP1')


def test_image_handover_is_separate_traceable_and_respects_revocation(images):
    store, media, _, _ = images
    aid, _, batch, service, snapshot, plan = analyzed(images)
    service.commit(batch, snapshot, plan)
    tid, view = handover(store)
    assert view['observed_evidence'] == []
    image = view['image_observations'][0]
    observation = image['observations'][0]
    assert image['attachment_id'] == aid and image['session_id'] == 's'
    assert observation['source_type'] == 'image_observation'
    assert observation['result']['summary'] == '杯子边缘有裂纹'
    assert observation['result']['uncertainties'] == ['无法确定破损原因']
    assert '不是用户原话' in observation['note']
    assert 'ORD-123' not in json.dumps(view, ensure_ascii=False)
    media.revoke('alice', aid)
    assert store.detail(tid, 'agent1')['task_handovers'][0]['handover']['image_observations'] == []


def test_submitted_operation_keeps_application_id_in_ticket(store, flags):
    class ApplicationBusiness(Business):
        def write(self, *args):
            return {**super().write(*args), 'after_sale_id': 'AS-NEW', 'platform_code': 'shop', 'is_demo': True}

    service, _, _, card = ready(store, business=ApplicationBusiness())
    confirm(service, card)
    operation = handover(store)[1]['operations'][0]
    assert operation['operation_id'] == card['operation_id']
    assert operation['status'] == 'succeeded'
    assert operation['result']['status'] == 'submitted'
    assert operation['result']['after_sale_id'] == 'AS-NEW'
    assert operation['result']['is_demo'] is True
    assert '申请提交不代表退款到账' in operation['note']


@pytest.mark.parametrize('tool,container', [('query_order', 'order'), ('query_logistics', 'logistics')])
def test_legacy_receipts_remain_historical_facts(tool, container):
    summary = observation(dict(tool=tool, result=dict(success=True, **{
        container: dict(order_id='OLD-ORDER', status='shipped', tracking_number='OLD-TRACK')})))
    assert summary['facts']['order_id'] == 'OLD-ORDER'
    assert summary['facts']['status'] == 'shipped'
    assert summary['result_observed_at'] is None and summary['sources'] == []
    assert '不代表当前状态' in summary['note']
