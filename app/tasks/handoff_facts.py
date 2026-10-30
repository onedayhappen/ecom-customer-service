"""Bounded, field-allowlisted handover facts, preserving receipt object structure.

This is a historical projection for staff, not evidence authorizing another write.
Never flatten candidates, parcel lines or currency/status totals into one order.
"""
import math

from app.tools.catalog import BY_NAME


SCALARS = frozenset('''
    id order_id platform_code merchant_id store_id order_ref target_ref product_id sku_id
    order_item_id item_id shipment_id return_shipment_id after_sale_id refund_id payment_id
    invoice_id issued_invoice_id request_id operation_id authorization_id work_order_id
    event_id provider_reference logistics_ref tracking_number carrier carrier_code
    shipment_kind kind status raw_status stage fulfillment_status after_sale_status
    name title title_snapshot product_name category quantity purchased recorded_shipped
    recorded_replacement_shipped recorded_delivered unshipped_quantity after_sale_occupied
    without_dispatch_record remaining_fulfillment total count item_count events_total
    return_shipments_total replacement_shipments_total return_events_total
    has_more truncated items_truncated events_truncated return_shipments_truncated
    replacement_shipments_truncated skus_truncated currency order_currency amount
    total_amount payable_amount paid_amount goods_amount shipping_amount discount_amount refund_amount
    requested_amount approved_amount settled_amount unit_price list_price sale_price
    subtotal recorded_success_amount order_paid_amount recorded_difference
    created_at updated_at paid_at shipped_at delivered_at completed_at closed_at
    requested_at refunded_at issued_at captured_at event_time observed_at data_as_of
    deadline estimated_delivery eta source status_source description location
    eligibility rule passed actual expected missing reason invoice_type invoice_number
    document_available next_state available_qty recorded_at returned_quantity is_demo
    received_quantity inspected_quantity accepted_quantity rejected_quantity
'''.split())
CONTAINERS = frozenset('''
    items purchase_items purchase_summary shipment return_shipment return_shipments
    replacement_shipments after_sale after_sales events return_events related
    payment_transactions shipments after_sale_requests invoice_requests
    quantities work_order return_authorization details replacement invoice
    documents issued_invoices skus inventory checks rules requirements
'''.split())
MAPS = frozenset('''
    amounts_by_currency_and_status refund_summary recorded_success_amounts
    spec_snapshot spec_values actual_spec_snapshot specs processes_connected
'''.split())
TEXT_LISTS = frozenset('reasons missing_fields required_materials references'.split())
SCOPE_FIELDS = frozenset('''
    order_ref target_ref after_sale_id order_id platform_code product_id sku_id
    order_item_id item_id shipment_id tracking_number invoice_id query status
    created_range subject_ref quantity
'''.split())
CANDIDATES = frozenset({'find_orders', 'find_after_sales', 'search_products', 'find_my_coupons'})
MAX_ROWS = 10
MAX_DEPTH = 6
MAX_TEXT = 2000
PRIVATE_FIELDS = frozenset('''
    password token token_hash confirmation_token access_token api_key private_notes secret
    phone mobile email recipient address address_detail tax_number document_key download_url
    storage_key ciphertext details_ciphertext title_ciphertext
'''.split())


def project(value, omitted, path='data', depth=0, *, free_map=False):
    """Copy known fields only; every size omission is reported separately."""
    if depth > MAX_DEPTH:
        omitted.append(path)
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        omitted.append(path)
        return None
    if isinstance(value, str):
        if len(value) <= MAX_TEXT:
            return value
        omitted.append(path)
        return '[内容过长，摘要未载入；请核对原始回执。]'
    if isinstance(value, list):
        if len(value) > MAX_ROWS:
            omitted.append(path)
        return [project(v, omitted, f'{path}[{i}]', depth + 1, free_map=free_map)
                for i, v in enumerate(value[:MAX_ROWS])]
    if not isinstance(value, dict):
        omitted.append(path)
        return None
    result = {}
    for key, child in list(value.items())[:100]:
        if key.lower() in PRIVATE_FIELDS or key.lower().endswith(('_ciphertext', '_token')):
            continue
        location = path + '.' + key
        if free_map:
            # Only used for known currency/status, specification and connection maps.
            result[key] = project(child, omitted, location, depth + 1, free_map=True)
        elif key in SCALARS and not isinstance(child, (dict, list)):
            result[key] = project(child, omitted, location, depth + 1)
        elif key in CONTAINERS:
            result[key] = project(child, omitted, location, depth + 1)
        elif key in MAPS:
            result[key] = project(child, omitted, location, depth + 1, free_map=True)
        elif key in TEXT_LISTS and isinstance(child, list):
            result[key] = project([v for v in child if isinstance(v, str)], omitted, location, depth + 1)
    if len(value) > 100:
        omitted.append(path)
    return result


def metadata(result, omitted):
    sources = []
    raw_sources = result.get('sources')
    if isinstance(raw_sources, list):
        if len(raw_sources) > MAX_ROWS:
            omitted.append('sources')
        for source in raw_sources[:MAX_ROWS]:
            if isinstance(source, dict):
                sources.append({k: project(v, omitted, 'sources.' + k, free_map=True)
                                for k, v in source.items() if k in {'kind', 'tables', 'source', 'version'}})
    return dict(is_demo=result.get('is_demo') is True, sources=sources, result_observed_at=project(result.get('observed_at'), omitted, 'observed_at'),
                data_as_of=project(result.get('data_as_of'), omitted, 'data_as_of'),
                completeness=project(result.get('completeness', 'unknown'), omitted, 'completeness'),
                limitations=project(result.get('limitations', []), omitted, 'limitations'),
                missing_fields=project(result.get('missing_fields', []), omitted, 'missing_fields'),
                has_more=bool(result.get('next_cursor')))


def observation(receipt):
    result = receipt.get('result')
    result = result if isinstance(result, dict) else {}
    name = receipt.get('tool')
    omitted = []
    new = 'data' in result
    supported = (name in BY_NAME and BY_NAME[name].domain != 'flow'
                 and name != 'search_policies') if new else name in {'query_order', 'query_logistics'}
    success = result.get('success') is True
    kind = 'candidates' if name in CANDIDATES else 'business_observation'
    data = result.get('data') if new else result.get('order') or result.get('logistics') or result
    if isinstance(data, dict) and data.get('next_state') == 'needs_confirmation':
        kind = 'operation_preparation'
    facts = project(data, omitted) if supported and success and isinstance(data, dict) else {}
    scope = receipt.get('arguments')
    scope = {k: v for k, v in scope.items() if k in SCOPE_FIELDS} if isinstance(scope, dict) else {}
    summary = dict(evidence_kind=kind, success=success, facts=facts,
                   query_scope=project(scope, omitted, 'query_scope', free_map=True),
                   **metadata(result, omitted))
    summary.update(summary_supported=supported, summary_omitted_paths=sorted(set(omitted)),
                   note=('候选记录尚未代表用户选定对象。' if kind == 'candidates' else
                         '仅准备确认内容，尚未提交业务操作。' if kind == 'operation_preparation' else
                         '仅代表所列对象在查询时的记录，不代表当前状态或整单办理结果。'))
    if not facts:
        summary['note'] += '摘要未载入可展示事实，不代表没有业务记录；请核对原始回执。'
    return summary


def operation_result(result):
    """Retain submitted application IDs without claiming downstream completion."""
    if not isinstance(result, dict):
        return {}, []
    omitted = []
    facts = project(result, omitted, 'result')
    if 'success' in result:
        facts['success'] = result['success'] is True
    if isinstance(result.get('data'), dict):
        facts['data'] = project(result['data'], omitted)
    return facts, sorted(set(omitted))
