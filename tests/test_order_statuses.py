"""Order-state transitions against the existing disposable MySQL fixture."""
import pytest
import pymysql
from pydantic import TypeAdapter, ValidationError
from app.tools.contracts import OrderStatus
from app.services.commerce.common import dumps, ref
from tests.test_core_tools import database, svc, read, commit


def test_order_status_contract():
    schema = TypeAdapter(OrderStatus)
    assert schema.validate_python('received') == 'received'
    assert schema.validate_python('after_sale') == 'after_sale'
    assert schema.validate_python('pending_receipt') == 'pending_receipt'
    for retired in ['completed','fulfilling','shipped']:
        with pytest.raises(ValidationError):
            schema.validate_python(retired)


@pytest.mark.parametrize('terminal', ['cancelled', 'rejected', 'completed'])
def test_multiple_after_sales_restore_received_only_after_last_ends(svc, terminal):
    with svc.store.transaction(write=True) as tx:
        tx.execute("UPDATE orders SET status='received' WHERE order_id='paid'")
        tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='refund_only'",
                   (dumps(dict(schema_version=1, allowed_statuses=['received'], local_authority=True)),))
    args = dict(kind='refund_only', items=[dict(order_item_id='i-paid', quantity=1)], reason='测试退款')
    first = commit(svc, 'prepare_after_sale', args)['after_sale_id']
    second = commit(svc, 'prepare_after_sale', args)['after_sale_id']
    def order():
        return read(svc, 'get_order', order_ref=ref('order','shop','paid'))['data']
    assert order()['status'] == 'after_sale'
    assert order()['status_before_after_sale'] == 'received'
    assert read(svc,'find_orders',status='after_sale')['data']['total'] == 1
    commit(svc,'prepare_after_sale_cancel',dict(after_sale_id=first,reason='撤回'))
    assert order()['status'] == 'after_sale'
    if terminal == 'cancelled':
        commit(svc,'prepare_after_sale_cancel',dict(after_sale_id=second,reason='撤回'))
    else:
        with svc.store.transaction(write=True) as tx:
            current=svc.owned_order(tx,ref('order','shop','paid'),lock=True)
            tx.execute('UPDATE after_sale_requests SET status=%s WHERE after_sale_id=%s',(terminal,second))
            svc.sync_order_after_sale(tx,current)
    assert order()['status'] == 'received'
    assert order()['status_before_after_sale'] is None


def test_database_rejects_retired_and_unbacked_states(svc):
    for status in ['completed','fulfilling','shipped','not_a_state','after_sale']:
        with pytest.raises(pymysql.MySQLError):
            with svc.store.transaction(write=True) as tx:
                tx.execute("UPDATE orders SET status=%s WHERE order_id='paid'",(status,))
