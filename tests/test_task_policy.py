# 文件说明：测试政策适用上下文的可信时间、业务范围和商品行证据。
"""Historical policy evidence keeps its trusted scope after tool retirement."""
from types import SimpleNamespace

import pytest

from app.rag.policy import PolicyError
from app.rag.task_policy import context_for_task
from app.tasks.candidates import BusinessScope


def order(**overrides):
    value = dict(order_id='ORD-A', platform_code='pdd', store_id='shop-a', channel='web',
                 category='headphones', created_at='2024-10-01T00:00:00Z', status='已发货')
    value.update(overrides)
    return {'success': True, 'order': value}


def task_context(details=None, result=None, **scope):
    receipt = dict(tool='query_order', result=result or order(), observed_epoch=100, event_id='proof')
    return SimpleNamespace(record=SimpleNamespace(details=details or {'order_id': 'ORD-A'}),
        runs=SimpleNamespace(candidates=SimpleNamespace(scope=BusinessScope(**scope))),
        historical=[], receipts=[receipt], evidence_time=100)


def test_order_time_and_scope_are_never_filled_from_user_claims():
    ctx = task_context(details=dict(order_id='ORD-A', category='headphones', channel='web',
        created_at='2024-10-01T00:00:00Z'), result=order(category=None, channel=None, created_at='2024-10-01 08:00:00'))
    selected = context_for_task(ctx)
    assert selected.order_created_at is None and selected.category is None and selected.channel is None
    with pytest.raises(PolicyError, match='scope_unverified'):
        context_for_task(task_context(store_id='shop-b'))


def test_multi_item_category_requires_a_verified_item_match():
    value = order(category=None, items=[{'product_id': 'a', 'category': 'headphones'},
                                       {'product_id': 'b', 'category': 'clothing'}])
    assert context_for_task(task_context(result=value)).category is None
    selected = context_for_task(task_context(details={'order_id': 'ORD-A', 'product_id': 'b'}, result=value))
    assert selected.category == 'clothing'
    assert context_for_task(task_context(details={'order_id': 'ORD-A', 'product_id': 'absent'}, result=value)).category is None


def test_order_dto_preserves_explicit_scope_without_inventing_defaults():
    from app.data.dto import OrderDTO
    row = order()['order']
    assert {k: OrderDTO.from_row(row, 'pdd').to_dict()[k] for k in ('store_id', 'channel', 'category')} == {
        'store_id': 'shop-a', 'channel': 'web', 'category': 'headphones'}
    assert OrderDTO.from_mock(row, 'pdd').to_dict()['channel'] == 'web'
    empty = OrderDTO.from_row({'order_id': 'ORD-A'}, 'pdd').to_dict()
    assert not {'store_id', 'channel', 'category'} & empty.keys()


def test_general_consultation_can_record_channel_category_but_not_permission():
    from app.agents.association import Detail
    from pydantic import ValidationError
    for field in ('channel', 'category'):
        Detail(field=field, value='web', evidence={'session_id': 's', 'sequence': 1, 'quote': 'web'})
    with pytest.raises(ValidationError):
        Detail(field='audience', value='staff', evidence={'session_id': 's', 'sequence': 1, 'quote': 'staff'})
