"""Input mistakes fail before business IO; public schemas retain useful constraints."""
import pytest
from pydantic import ValidationError

from app.tools.catalog import BY_NAME, TOOLS


@pytest.mark.parametrize('name,args', [
    ('get_my_account', {'cursor': 'previous'}),
    ('get_invoices', {'view': 'issued'}),
    ('find_orders', {'paid_amount_range': {'minimum': '100', 'maximum': '50', 'currency': 'CNY'}}),
    ('find_orders', {'paid_amount_range': {'currency': 'CNY'}}),
    ('find_orders', {'item_quantity': '2'}),
    ('find_orders', {'hints': {'created_range': {'from': '2026-10-01', 'to': '2026-10-02'}}}),
    ('find_orders', {'specs': [{'name': '尺寸', 'value': '38'}, {'name': '尺寸', 'value': '39'}]}),
    ('search_products', {'query': '鞋', 'specs': [{'name': '颜色', 'value': '红'}, {'name': '颜色', 'value': '蓝'}]}),
    ('get_product_details', {'target_ref': 'ref', 'specs': [{'name': '尺寸', 'value': '38'}]*2}),
    ('quote_delivery', {'address_id': '1', 'items': [{'sku_id': 's', 'quantity': 1}]*2}),
    ('estimate_purchase', {'items': [{'sku_id': 's', 'quantity': 1}]*2}),
    ('quote_checkout', {'address_id': '1', 'items': [{'sku_id': 's', 'quantity': 1}]*2}),
    ('check_coupon', {'coupon_ref': 'c', 'target': {'items': [{'sku_id': 's', 'quantity': 1}]*2}}),
    ('check_coupon', {'coupon_ref': 'c', 'target': {'order_ref': 'o', 'items': [{'sku_id': 's', 'quantity': 1}]}}),
    ('prepare_return_shipment', {'after_sale_id': 'a', 'carrier_code': 'SF', 'tracking_number': '123',
                                 'items': [{'order_item_id': 'i', 'quantity': 1}]*2}),
])
def test_ambiguous_or_invalid_inputs_are_rejected(name, args):
    with pytest.raises(ValidationError):
        BY_NAME[name].arguments.model_validate(args)


def test_order_clues_keep_uncertainty_and_zero_amount():
    args = BY_NAME['find_orders'].arguments.model_validate({
        'query': '鞋', 'paid_amount_range': {'maximum': '0', 'currency': 'CNY'},
        'hints': {'specs': [{'name': '尺寸', 'value': '38'}], 'item_quantity': 2},
    }).model_dump(exclude_none=True, by_alias=True)
    assert args['specs'] == [] and args['hints']['item_quantity'] == 2
    assert args['paid_amount_range']['maximum'] == '0'


def test_all_public_schemas_are_strict_and_keep_constraints():
    for tool in TOOLS:
        schema = tool.definition()['function']['parameters']
        assert schema['additionalProperties'] is False, tool.name
        assert 'user_id' not in schema['properties'], tool.name
        for nested in schema.get('$defs', {}).values():
            if nested.get('type') == 'object':
                assert nested['additionalProperties'] is False, tool.name
    for name, field in [('get_order', 'order_ref'), ('get_restock_plan', 'sku_id'),
                        ('claim_coupon', 'coupon_ref'), ('get_logistics', 'target_ref')]:
        prop = BY_NAME[name].definition()['function']['parameters']['properties'][field]
        assert prop['minLength'] == 1 and prop['maxLength'] == 512 and prop['description']
