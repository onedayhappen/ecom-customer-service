"""Read the real isolated business tables; never submit a poll as a new write."""
import json
import os
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.data.logistics_dao import MySqlLogisticsDAO
from app.tasks.external_progress import ExternalProgress
from app.tasks.repository import TaskRepository
from tests.test_task_operations_mysql import business_db, prepared, DSN
from tests.test_task_operations import flags
from tests.test_external_progress import polling, due
from tests.test_task_candidates import store, task
from tests.test_task_storage import empty_store
from tests.test_task_execution import engine, Client, response, request, local_budgets

pytestmark = pytest.mark.skipif(os.environ.get('ECOM_INTEGRATION') != '1', reason='isolated MySQL integration only')


def test_real_refund_request_followed_by_matching_application_and_settlement(store, business_db, monkeypatch):
    db, order = business_db
    monkeypatch.setattr(settings, 'order_db_dsn', DSN)
    operations, card = prepared(store, order)
    assert operations.confirm('s', card['operation_id'], card['confirmation_token'])['status'] == 'succeeded'
    tid = card['task_id']
    worker = ExternalProgress(store)
    assert worker.tick() == ['updated']
    current = TaskRepository(store, 'alice').get(tid)
    assert current.status == 'waiting_external'
    assert current.waiting_for['external_progress']['source'] == 'mysql_order_fields'
    assert current.waiting_for['external_progress']['is_demo'] is True
    after_id = 'AS-' + uuid4().hex
    row = dict(source='mysql_demo', observed_at='2026-10-01T12:00:00+00:00', after_sale_id=after_id,
        order_id=order, kind='refund', status='refunded', is_demo=True, request_id=card['operation_id'],
        settled_at='2026-10-01T11:00:00+00:00', transaction_id='TX-' + after_id)
    try:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO commerce_after_sales
                (platform_code,user_id,record_id,object_id,recorded_at,is_demo,payload)
                VALUES ('pdd','alice',%s,%s,NOW(6),TRUE,%s)''', (after_id, order, json.dumps(row)))
        # The imported record is older than the fresh order read: keep progress
        # and re-query rather than silently treating it as the latest event.
        due(store)
        assert worker.tick() == ['retry']
        from datetime import datetime, timezone
        row['observed_at'] = datetime.now(timezone.utc).isoformat()
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE commerce_after_sales SET payload=%s WHERE user_id='alice' AND record_id=%s",
                        (json.dumps(row), after_id))
        due(store)
        assert worker.tick() == ['completed']
        assert TaskRepository(store, 'alice').get(tid).status == 'completed'
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM commerce_mutations WHERE order_id=%s', (order,))
            assert cur.fetchone()['n'] == 1
        assert '演示业务数据' in store.public_history('s', 'alice')[-1]['content']
    finally:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM commerce_after_sales WHERE user_id='alice' AND record_id=%s", (after_id,))


def test_real_multi_package_tracking_only_updates_the_selected_parcel(store, business_db, monkeypatch):
    db, order = business_db
    monkeypatch.setattr(settings, 'order_db_dsn', DSN)
    first, second = 'TRACK-' + uuid4().hex, 'TRACK-' + uuid4().hex
    with db._connection() as conn, conn.cursor() as cur:
        MySqlLogisticsDAO.ensure_schema(conn)
        for tracking, status in ((first, 'delivered'), (second, 'in_transit')):
            cur.execute('''INSERT INTO logistics(tracking_number,platform_code,order_id,status)
                VALUES (%s,'pdd',%s,%s)''', (tracking, order, status))
    try:
        record, source = task(store, 's', goal='跟进指定运单签收', details=dict(order_id=order, tracking_number=first))
        def save(params):
            value = json.loads(next(m['content'] for m in reversed(params['messages']) if m['role'] == 'tool'))
            return response(calls=[('save_task_progress', dict(expected_version=record.version, state='waiting_external',
                wait_reason='等待这票包裹签收', evidence_id=value['_task_evidence']['event_id']))])
        result = engine(Client(response(calls=[('query_logistics', dict(order_id=order, tracking_number=first))]), save)).execute_task(
            store, 'alice', request(record, source), tools=lambda *_: dict(success=True,
                logistics=dict(order_id=order, tracking_number=first, status='in_transit')))
        assert result['state'] == 'waiting_external'
        assert ExternalProgress(store).tick() == ['completed']
        content = store.public_history('s', 'alice')[-1]['content']
        assert first in content and second not in content and '仅代表这一票包裹' in content
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT status FROM logistics WHERE tracking_number=%s', (second,))
            assert cur.fetchone()['status'] == 'in_transit'
    finally:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM logistics WHERE tracking_number IN (%s,%s)', (first, second))


def test_real_local_invoice_completion_never_claims_tax_invoice(store, business_db, monkeypatch):
    from app.tasks.operations import TaskOperations
    _, order = business_db
    monkeypatch.setattr(settings, 'order_db_dsn', DSN)
    record, _ = task(store, 's', goal='创建本地演示发票', details=dict(order_id=order,
        invoice_title='张三', invoice_type='个人'))
    operations = TaskOperations(store, 'alice')
    operations.prepare('s', record.task_id, record.version, 'issue_invoice')
    card = operations.list('s')[0]
    assert operations.confirm('s', card['operation_id'], card['confirmation_token'])['status'] == 'succeeded'
    assert ExternalProgress(store).tick() == ['completed']
    assert TaskRepository(store, 'alice').get(record.task_id).status == 'completed'
    assert '不是税务发票' in store.public_history('s', 'alice')[-1]['content']


def test_external_channel_application_is_read_instead_of_submitted_again(store, business_db, monkeypatch):
    from app.data.commerce_dao import CommerceRejection
    from app.tasks.operations import TaskOperations
    from app.tools.commerce import invoke
    from app.tools.request_context import set_current_user_id, reset_current_user_id
    db, order = business_db
    monkeypatch.setattr(settings, 'order_db_dsn', DSN)
    after_id = 'OTHER-CHANNEL-' + uuid4().hex
    value = dict(source='mysql_demo', observed_at='2026-10-01T12:00:00+00:00', after_sale_id=after_id,
        order_id=order, kind='refund', status='submitted', is_demo=True)
    try:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO commerce_after_sales
                (platform_code,user_id,record_id,object_id,recorded_at,is_demo,payload)
                VALUES ('pdd','alice',%s,%s,NOW(6),TRUE,%s)''', (after_id, order, json.dumps(value)))
        record, _ = task(store, 's', goal='申请退款', details=dict(order_id=order, reason='不想要了'))
        with pytest.raises(CommerceRejection, match='已有售后'):
            TaskOperations(store, 'alice').prepare('s', record.task_id, record.version, 'apply_refund')
        token = set_current_user_id('alice')
        try:
            result = invoke('query_after_sale', dict(order_id=order, after_sale_id=after_id))
            assert result['success'] and result['records'][0]['after_sale_id'] == after_id
        finally:
            reset_current_user_id(token)
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM commerce_mutations WHERE order_id=%s', (order,))
            assert cur.fetchone()['n'] == 0
    finally:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM commerce_after_sales WHERE user_id='alice' AND record_id=%s", (after_id,))
