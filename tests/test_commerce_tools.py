"""Money boundaries, evidence validation, ownership and registration contracts."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.tools.commerce import CONTRACTS, TOOL_DEFINITIONS, invoke
from app.tools.commerce_models import Offer
from app.tools.request_context import set_current_user_id, reset_current_user_id
from app.data_plan.capabilities import CAPABILITIES
from scripts.init_commerce_db import demo_records

AT = datetime.now(timezone.utc)


def rule(**overrides):
    return {'source': 'test', 'observed_at': (AT - timedelta(hours=1)).isoformat(),
            'rule_id': 'r1', 'version': 'v1', 'channel': 'pdd', 'currency': 'CNY',
            'starts_at': (AT - timedelta(days=1)).isoformat(),
            'ends_at': (AT + timedelta(days=1)).isoformat(), 'kind': 'fixed',
            'amount_minor': 100, **overrides}


def calculation(**overrides):
    return {'items': [{'sku_id': 'a', 'quantity': 1, 'unit_price_minor': 1000}],
            'currency': 'CNY', 'channel': 'pdd', 'shipping_minor': 200,
            'rules': [rule()], 'eligibility': {'r1': 'eligible'}, **overrides}


def offer(identifier='a', **overrides):
    return {'source': 'test', 'observed_at': (AT - timedelta(hours=1)).isoformat(),
            'offer_id': identifier, 'sku_id': 'sku', 'specification': 'black-42',
            'condition': 'new', 'included_items': 'standard', 'channel': identifier,
            'region': '上海', 'quantity': 1, 'currency': 'CNY', 'goods_minor': 1000,
            'discount_minor': 0, 'shipping_minor': 100, 'fees_minor': 0,
            'total_minor': 1100, 'eligibility': 'eligible', 'eligibility_basis': 'public',
            'valid_until': (AT + timedelta(hours=1)).isoformat(), **overrides}


def test_all_18_tools_are_registered_once_and_match_capabilities():
    from app.tools.registry import _TOOL_MAP, TOOL_DEFINITIONS as definitions
    names = [d['function']['name'] for d in definitions]
    assert len(CONTRACTS) == 18
    assert len(names) == len(set(names))
    for name in CONTRACTS:
        assert callable(_TOOL_MAP[name]) and names.count(name) == 1
        assert CAPABILITIES[name].implemented
    for definition in TOOL_DEFINITIONS:
        schema = definition['function']['parameters']
        assert schema['additionalProperties'] is False
        assert '$ref' not in json.dumps(schema)
        assert 'user_id' not in schema.get('properties', {})
        assert 'confirmed' not in schema.get('properties', {})


@pytest.mark.parametrize('amount', [True, 1.2, -1, '100'])
def test_invalid_money_is_not_coerced(amount):
    result = invoke('calc_promotion', calculation(shipping_minor=amount))
    assert result['error_code'] == 'validation_error'


def test_fixed_price_math_includes_freight_and_fees():
    result = invoke('calc_promotion', calculation(fees_minor=70))
    assert result['total_minor'] == 1170
    assert result['goods_minor'] - result['discount_minor'] + 270 == result['total_minor']


def test_large_discount_is_capped_at_eligible_goods_and_rounding_is_conserved():
    items = [{'sku_id': s, 'quantity': 1, 'unit_price_minor': p} for s, p in [('a', 101), ('b', 100), ('c', 900)]]
    rules = [rule(kind='percentage', amount_minor=0, discount_bps=3333, sku_ids=['a', 'b']),
             rule(rule_id='r2', sku_ids=['a'], amount_minor=1000, priority=1)]
    result = invoke('calc_promotion', calculation(items=items, rules=rules,
                    eligibility={'r1': 'eligible', 'r2': 'eligible'}, shipping_minor=0))
    assert result['success']
    assert result['discount_minor'] == 134  # 66 allocated as 33+33, then remaining a=68.
    assert result['total_minor'] == 967


@pytest.mark.parametrize('change,expected', [
    ({'eligibility': {}}, 'evidence_unavailable'),
    ({'rules': [rule(minimum_minor=2000)]}, 'business_rejection'),
    ({'rules': [rule(currency='USD')]}, 'validation_error'),
    ({'rules': [rule(ends_at=(AT - timedelta(hours=1)).isoformat())]}, 'business_rejection'),
    ({'rules': [rule(), rule()]}, 'validation_error'),
])
def test_invalid_or_unverified_promotion_plan_is_rejected(change, expected):
    assert invoke('calc_promotion', calculation(**change))['error_code'] == expected


def test_exclusive_rules_cannot_be_stacked():
    rules = [rule(exclusive_group='g'), rule(rule_id='r2', exclusive_group='g')]
    assert invoke('calc_promotion', calculation(rules=rules, eligibility={'r1': 'eligible', 'r2': 'eligible'}))['error_code'] == 'business_rejection'


def test_coupon_diagnosis_lists_actual_failed_checks():
    result = invoke('check_coupon_eligibility', {
        'coupon': {'source': 'test', 'observed_at': AT.isoformat(), 'coupon_id': 'c',
                   'status': 'locked', 'owned': True, 'rule': rule(minimum_minor=2000)},
        'items': calculation()['items'], 'channel': 'other', 'currency': 'CNY',
    })
    assert result['success'] and not result['eligible']
    assert set(result['failure_reasons']) == {'available', 'channel', 'minimum'}


def test_offer_comparison_uses_total_not_goods_price():
    a, b = offer('a'), offer('b', goods_minor=950, shipping_minor=200, total_minor=1150)
    result = invoke('compare_offers', {'offers': [b, a]})
    assert [r['offer_id'] for r in result['rankings']] == ['a', 'b']
    assert result['rankings'][1]['difference_minor'] == 50


@pytest.mark.parametrize('change', [{'specification': 'white-42'}, {'currency': 'USD'},
                                  {'eligibility': 'unknown'}, {'quantity': 2},
                                  {'valid_until': (AT - timedelta(minutes=1)).isoformat()}])
def test_incomparable_offers_never_produce_cheapest_claim(change):
    result = invoke('compare_offers', {'offers': [offer('a'), offer('b', **change)]})
    assert not result['success'] and 'rankings' not in result


def test_inconsistent_or_incomplete_prices_fail_validation():
    for change in ({'total_minor': 99}, {'shipping_minor': None}):
        assert invoke('compare_offers', {'offers': [offer(), offer('b', **change)]})['error_code'] == 'validation_error'


def test_history_is_observed_minimum_with_explicit_coverage():
    history = {'source': 'test', 'observed_at': AT.isoformat(), 'sku_id': 's',
               'specification': 'black', 'channel': 'pdd', 'currency': 'CNY', 'price_type': 'list',
               'start': (AT - timedelta(days=10)).isoformat(), 'end': AT.isoformat(),
               'coverage_complete': False, 'points': [
                   {'source': 'test', 'observed_at': (AT - timedelta(days=2)).isoformat(), 'amount_minor': 100},
                   {'source': 'test', 'observed_at': (AT - timedelta(days=1)).isoformat(), 'amount_minor': 90},
               ]}
    result = invoke('summarize_price_history', {'history': history})
    assert result['lowest_observed_minor'] == 90 and result['sample_count'] == 2
    assert result['coverage_complete'] is False
    history['points'].append({**history['points'][0], 'amount_minor': 101})
    assert invoke('summarize_price_history', {'history': history})['error_code'] == 'evidence_conflict'


def test_policy_scope_expiry_and_conflicts():
    p = {'source': 'test', 'observed_at': AT.isoformat(), 'policy_id': 'return',
         'version': '1', 'topic': 'refund', 'channel': 'pdd', 'category': 'shoes',
         'effective_from': (AT - timedelta(days=1)).isoformat(),
         'effective_until': (AT + timedelta(days=1)).isoformat(), 'text': '条款 A'}
    args = {'topic': 'refund', 'channel': 'pdd', 'category': 'shoes', 'policies': [p]}
    assert invoke('validate_policy_evidence', args)['success']
    assert invoke('validate_policy_evidence', {**args, 'category': 'food'})['error_code'] == 'evidence_unavailable'
    assert invoke('validate_policy_evidence', {**args, 'policies': [p, {**p, 'version': '2'}]})['error_code'] == 'evidence_conflict'


def test_private_queries_require_server_identity_and_reject_spoofed_identity():
    token = set_current_user_id(None)
    try:
        assert invoke('query_user_coupons', {})['error_code'] == 'business_rejection'
        assert invoke('query_user_coupons', {'user_id': 'other'})['error_code'] == 'validation_error'
    finally:
        reset_current_user_id(token)


def test_missing_database_does_not_fall_back_to_demo(monkeypatch):
    monkeypatch.setattr('app.tools.commerce_service.settings.order_db_dsn', '')
    result = invoke('query_offers', {'sku_id': 'sku', 'channels': ['pdd'], 'region': '上海'})
    assert result['error_code'] == 'evidence_unavailable'


@pytest.mark.parametrize('name', ['issue_invoice', 'reissue_invoice'])
def test_direct_invoice_calls_cannot_bypass_confirmation(name):
    args = {'order_id': 'ORD-X', 'preview_version': 'v',
            'fields': {'title': '个人', 'invoice_type': 'personal'}}
    if name == 'reissue_invoice':
        args['invoice_id'] = 'INV-X'
    assert invoke(name, args)['error_code'] == 'confirmation_invalid'


def test_support_queries_are_scoped_to_current_user_and_session(tmp_path):
    from app.support.store import SupportStore
    from app.support.runtime import bot_scope
    store = SupportStore(f'sqlite:///{tmp_path / "support.db"}')
    state = store.ensure('s', 'u')
    token = set_current_user_id('u')
    try:
        with bot_scope(store, 's', 'u', state['control_version']):
            assert invoke('query_handoff_status', {})['ticket'] is None
            assert invoke('query_mutation_status', {'operation_id': 'missing'})['status'] == 'unknown'
            alien = set_current_user_id('alien')
            try:
                assert invoke('query_handoff_status', {})['error_code'] == 'business_rejection'
            finally:
                reset_current_user_id(alien)
    finally:
        reset_current_user_id(token)


def test_seed_quotes_satisfy_current_contract_and_are_labelled_demo():
    records = demo_records([{'product_id': 'p', 'name': '商品', 'price': '123.45'}], [])
    for r in records:
        if r['kind'] == 'offers':
            assert Offer.model_validate(r['payload']).source == 'mysql_demo'
