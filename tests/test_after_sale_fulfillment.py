# 文件说明：测试售后分包、商品数量、渠道回执及完成状态的数据库一致性。
"""After-sale parcel and completion regressions in disposable MySQL."""
from datetime import timedelta
import pytest
from tests.test_core_tools import database, svc, read, commit
from tests.test_extended_tools import extended, Provider
from app.services.commerce.common import BusinessError, utcnow, ref
from app.services.commerce.providers import provider_factory


def test_exchange_waits_for_receipt_and_reuses_parcel(extended):
    service=extended;now=utcnow()
    with service.store.transaction(write=True) as tx:
        tx.execute("UPDATE product_skus SET list_price=10 WHERE sku_id='s2'")
    parcel=dict(tracking_number='DEMO-EXCHANGE',carrier='DEMO',shipped_at=(now-timedelta(hours=2)).isoformat(),sku_id='s2',quantity=1)
    provider=Provider(dict(status='completed',reference='exchange',replacement_shipment=parcel),
        dict(status='completed',reference='exchange',replacement_shipment=dict(parcel,delivered_at=(now-timedelta(hours=1)).isoformat(),receipt_confirmed_at=now.isoformat())))
    with provider_factory(lambda _:provider):
        result=commit(service,'prepare_exchange',dict(order_item_id='i-paid',replacement_sku_id='s2',quantity=1,reason='换颜色'),'exchange-receipt')
        aid=result['after_sale_id'];service.process_provider_request('exchange-receipt')
        state=read(service,'get_after_sale_status',after_sale_id=aid)['data']
        assert state['after_sale']['status']=='processing'
        assert state['after_sale']['stage']=='replacement_in_transit'
        assert state['after_sale']['completed_at'] is None
        assert read(service,'get_order',order_ref=ref('order','shop','paid'))['data']['status']=='after_sale'
        service.process_provider_request('exchange-receipt')
        state=read(service,'get_after_sale_status',after_sale_id=aid)['data']
        assert state['after_sale']['status']=='completed' and state['after_sale']['completed_at']
        assert len(state['replacement_shipments'])==1
        assert state['replacement_shipments'][0]['items'][0]['actual_sku_id']=='s2'
        service.process_provider_request('exchange-receipt')
    with service.store.transaction() as tx:
        assert tx.one("SELECT available_qty,reserved_qty FROM sku_inventory WHERE sku_id='s2'")==dict(available_qty=19,reserved_qty=0)
        assert tx.one('SELECT COUNT(*) n FROM shipments')['n']==1


def test_split_return_quantities_and_all_parcel_reads(extended):
    service=extended;now=utcnow()
    authorization=dict(address={'detail':'test'},carriers=[dict(code='DEMO',name='模拟物流')],requirements=[],deadline=(now+timedelta(days=3)).isoformat())
    provider=Provider(dict(status='accepted',reference='repair',return_authorization=authorization),
        dict(status='processing',reference='repair',return_events=[dict(status='accepted',event_time=now.isoformat(),description='第一包验收')]))
    with provider_factory(lambda _:provider):
        result=commit(service,'prepare_repair',dict(order_item_id='i-paid',quantity=2,problem='维修'),'split-repair')
        aid=result['after_sale_id'];service.process_provider_request('split-repair')
        for index in (1,2):
            commit(service,'prepare_return_shipment',dict(after_sale_id=aid,carrier_code='DEMO',tracking_number=f'DEMO-{index}',items=[dict(order_item_id='i-paid',quantity=1)]),f'split-{index}')
            if index==1:
                service.process_provider_request('split-repair')
                assert read(service,'get_after_sale_status',after_sale_id=aid)['data']['after_sale']['stage']=='awaiting_return'
        with pytest.raises(BusinessError):
            service.preview('prepare_return_shipment',dict(after_sale_id=aid,carrier_code='DEMO',tracking_number='DEMO-3'))
    state=read(service,'get_after_sale_status',after_sale_id=aid)['data']
    assert len(state['return_shipments'])==2 and state['return_shipment'] is None
    assert sum(s['items'][0]['quantity'] for s in state['return_shipments'])==2


def test_migration_008_can_resume(database):
    import pymysql
    from app.data.order_dao import parse_mysql_dsn
    from scripts.migrate_mysql_business_schema import _apply_008
    conn=pymysql.connect(**parse_mysql_dsn(database.dsn),cursorclass=pymysql.cursors.DictCursor)
    try:
        _apply_008(conn);_apply_008(conn)
    finally:
        conn.close()


def test_partial_replacement_consumes_only_dispatched_stock(extended):
    service=extended
    with service.store.transaction(write=True) as tx:
        tx.execute("UPDATE product_skus SET list_price=10 WHERE sku_id='s2'")
    response=dict(status='processing',reference='partial',replacement_shipment=dict(
        tracking_number='DEMO-PARTIAL',carrier='DEMO',shipped_at=utcnow().isoformat(),sku_id='s2',quantity=1))
    provider=Provider(response,response,dict(status='rejected',reference='partial'))
    with provider_factory(lambda _:provider):
        commit(service,'prepare_exchange',dict(order_item_id='i-paid',replacement_sku_id='s2',quantity=2,reason='分包换货'),'partial')
        service.process_provider_request('partial');service.process_provider_request('partial')
        with service.store.transaction() as tx:
            assert tx.one("SELECT available_qty,reserved_qty FROM sku_inventory WHERE sku_id='s2'")==dict(available_qty=18,reserved_qty=1)
            assert tx.one('SELECT COUNT(*) n FROM shipments')['n']==1
        service.process_provider_request('partial')
    with service.store.transaction() as tx:
        assert tx.one("SELECT available_qty,reserved_qty FROM sku_inventory WHERE sku_id='s2'")==dict(available_qty=19,reserved_qty=0)
