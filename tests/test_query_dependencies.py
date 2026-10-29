"""Current read contracts must survive durable dependency scheduling."""
import json

import pytest

from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.scheduling import Dependency, ScheduleBlocked, TaskSchedule, TaskWorker
from app.tools.catalog import TOOLS
from tests.test_task_candidates import store, task
from tests.test_task_execution import Client, engine, local_budgets, request, response
from tests.test_task_scheduling import drain, product_result
from tests.test_task_storage import empty_store, message


@pytest.mark.parametrize('spec', TOOLS, ids=lambda spec: spec.name)
def test_dependency_uses_business_read_catalog(spec):
    raw = dict(job_id='parent', kind='query', tool=spec.name, field='data.items.0.status', value='ready')
    if spec.effect == 'read' and not spec.task_only:
        assert Dependency.model_validate(raw).tool == spec.name
    else:
        with pytest.raises(ValueError):
            Dependency.model_validate(raw)


@pytest.mark.parametrize('patch', [
    {'tool': 'query_order'}, {'tool': 'query_logistics'}, {'tool': 'query_product'},
    {'tool': 'search_knowledge'}, {'field': 'order.stock'}, {'field': 'data.items.-1'},
    {'field': 'data.items[0]'}, {'field': 'data.items.*'}, {'field': 'data.items.01'},
    {'compare': 'gt', 'value': True}, {'compare': 'gt', 'value': '1'},
    {'value': float('nan')}, {'value': float('inf')},
])
def test_dependency_rejects_retired_or_ambiguous_contract(patch):
    with pytest.raises(ValueError):
        Dependency.model_validate(dict(job_id='p', kind='query', tool='get_order',
                                      field='data.status', value=1) | patch)


def completed_parent(store, tool='get_product_details', arguments=None, receipt=None):
    parent, source = task(store, 's')
    req = TaskExecutionInput.model_validate(request(parent, source))
    TaskSchedule(store, 'alice').enqueue(req)
    runs = TaskRuns(store, 'alice')
    reserved, _ = runs.begin(req)
    eid = runs.save_tool(req, reserved['payload'], 1, tool,
                         arguments or {'target_ref': 'sku:test'}, receipt or product_result(None, None))['event_id']
    runs.finish(req, reserved['payload'], {'state': 'ready', 'evidence_ids': [eid]})
    return req.job_id, eid


def child_request(store, parent, **patch):
    child, source = task(store, 's')
    req = TaskExecutionInput.model_validate(request(child, source))
    dep = dict(job_id=parent, kind='query', tool='get_product_details',
               field='data.items.0.inventory.available_qty', compare='gt', value=0) | patch
    TaskSchedule(store, 'alice').enqueue(req, dependencies=[dep])
    return req


@pytest.mark.parametrize('tool,args,data,path,value', [
    ('get_order', {'order_ref': 'order:test'}, {'status': 'paid'}, 'data.status', 'paid'),
    ('get_logistics', {'target_ref': 'order:test'}, {'items': [{'status': 'delivered'}]},
     'data.items.0.status', 'delivered'),
    ('search_policies', {'query': '退货'}, {'policy_evidence': {'results': [{'source_id': 'p1'}]}},
     'data.policy_evidence.results.0.source_id', 'p1'),
])
def test_current_read_evidence_releases_child(store, tool, args, data, path, value):
    parent, eid = completed_parent(store, tool, args, {'ok': True, 'success': True, 'data': data})
    req = child_request(store, parent, tool=tool, field=path, compare='eq', value=value, evidence_id=eid)
    assert TaskRuns(store, 'alice').begin(req)[0]


@pytest.mark.parametrize('fault', [
    'failed', 'not_ok', 'late', 'stale', 'future', 'wrong_task', 'wrong_tool',
    'wrong_arguments', 'wrong_event_id', 'wrong_kind', 'public', 'actor',
    'controls', 'provenance', 'missing', 'boolean', 'empty', 'out_of_bounds', 'wrong_evidence', 'invalidated',
])
def test_invalid_read_cannot_release_child(store, fault):
    parent, eid = completed_parent(store)
    patch = {'field': 'data.items.1.inventory.available_qty'} if fault == 'out_of_bounds' else {}
    if fault == 'wrong_evidence':
        patch['evidence_id'] = 'missing'
    req = child_request(store, parent, **patch)
    with store.transaction() as tx:
        row = tx.one('SELECT payload FROM support_events WHERE event_key=?', ('task-tool:' + eid,))
        payload = json.loads(row['payload'])
        if fault == 'failed': payload['result']['success'] = False
        elif fault == 'not_ok': payload['result']['ok'] = False
        elif fault == 'late': payload['late'] = True
        elif fault == 'stale': payload['observed_epoch'] -= 61
        elif fault == 'future': payload['observed_epoch'] += 60
        elif fault == 'wrong_task': payload['task_id'] = 'other'
        elif fault == 'wrong_tool': payload['tool'] = 'query_product'
        elif fault == 'wrong_arguments': payload['arguments'] = {'product_id': 'old'}
        elif fault == 'wrong_event_id': payload['event_id'] = 'other'
        elif fault == 'controls': payload['controls'] = {}
        elif fault == 'provenance': payload['provenance'] = {}
        elif fault == 'missing': payload['result']['data'] = {}
        elif fault == 'boolean': payload['result']['data']['items'][0]['inventory']['available_qty'] = True
        elif fault == 'empty': payload['result']['data']['items'] = []
        tx.execute('UPDATE support_events SET payload=? WHERE event_key=?',
                   (json.dumps(payload), 'task-tool:' + eid))
        updates = {'wrong_kind': ('kind', 'task_operation_result'), 'public': ('visibility', 'public'),
                   'actor': ('actor', 'user')}
        if fault in updates:
            column, value = updates[fault]
            tx.execute(f'UPDATE support_events SET {column}=? WHERE event_key=?', (value, 'task-tool:' + eid))
        if fault == 'invalidated':
            from app.tasks.provenance import event_ref
            row = tx.one('SELECT session_id,sequence FROM support_events WHERE event_key=?', ('task-tool:' + eid,))
            tx.execute('INSERT INTO task_invalid_sources VALUES (?,?,?,?)',
                       ('alice', 'event', event_ref(row['session_id'], row['sequence']), 'test'))
    with pytest.raises(ScheduleBlocked, match='condition_not_verified'):
        TaskRuns(store, 'alice').begin(req)


@pytest.mark.parametrize('running', [False, True])
def test_persisted_retired_dependency_never_crashes_or_retries_unconditionally(store, running):
    parent, _ = completed_parent(store)
    req = child_request(store, parent)
    if running:
        TaskRuns(store, 'alice').begin(req)
    with store.transaction() as tx:
        row = tx.one('SELECT payload FROM runtime_jobs WHERE job_id=?', (req.job_id,))
        payload = json.loads(row['payload'])
        payload['schedule']['dependencies'][0].update(tool='query_product', field='product.stock')
        tx.execute('UPDATE runtime_jobs SET payload=?,run_deadline=0 WHERE job_id=?',
                   (json.dumps(payload), req.job_id))
    worker = TaskWorker(store, engine(Client()))
    try:
        worker.tick()
        state = TaskSchedule(store, 'alice').get(req.job_id)
        assert state['reason'] == 'dependency_contract_unsupported'
        assert state['status'] == ('interrupted' if running else 'pending')
        assert not worker.futures
        with store.transaction(read_only=True) as tx:
            assert tx.one('SELECT COUNT(*) AS n FROM runtime_jobs')['n'] == 2
    finally:
        worker.close()


def test_registered_product_read_and_duplicate_wakeup(store):
    parent, source = task(store, 's', goal='查询商品库存', details={'product_id': 'test'})
    parent_req = request(parent, source)
    schedule = TaskSchedule(store, 'alice')
    parent_id = schedule.enqueue(parent_req)
    child_req = child_request(store, parent_id)
    calls = []
    def read(name, args):
        calls.append((name, args))
        return product_result(name, args)
    client = Client(response(calls=[('get_product_details', {'target_ref': 'sku:test'})]),
                    response('查询完成。'), response('继续处理。'))
    worker = TaskWorker(store, engine(client), tools=read, concurrency=1)
    try:
        rows = drain(worker, [parent_id, child_req.job_id])
        assert all(row['result']['state'] == 'ready' for row in rows)
        assert calls == [('get_product_details', {'target_ref': 'sku:test'})]
        assert schedule.enqueue(parent_req) == parent_id
        worker.tick()
        assert len(calls) == 1
    finally:
        worker.close()
