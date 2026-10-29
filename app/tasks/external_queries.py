"""Bind current business query receipts to explicit, durable polling targets."""
import json
import math
from datetime import datetime, timezone

from app.runtime.outbox import clock
from app.services.commerce.common import ref, unref
from app.tasks.evidence import find_evidence
from app.tasks.external_evidence import ExternalEvidenceError, timestamp
from app.tasks.provenance import context_event_valid
from app.tools.catalog import BY_NAME


QUERY_TOOLS = {'get_logistics', 'get_after_sale_status', 'get_refunds'}


def require(condition, message):
    if not condition:
        raise ExternalEvidenceError(message)


def query_source(tx, runs, record, *, fresh):
    eid = record.waiting_for['evidence_id']
    event = find_evidence(tx, runs.user, eid, task_id=record.task_id, kind='task_tool_result')
    require(event is not None, '等待来源不是本事项的系统查询回执')
    evidence = event['payload']
    require(evidence.get('tool') in QUERY_TOOLS and not evidence.get('late'), '等待来源不是有效的新版查询')
    require(context_event_valid(tx, event['session_id'], event['sequence']), '等待来源已失效')
    require(runs.control_versions_valid(tx, evidence.get('controls')), '等待来源控制版本已改变')
    BY_NAME[evidence['tool']].arguments.model_validate(evidence.get('arguments'))
    checked_data(evidence.get('result'))
    observed = evidence.get('observed_epoch')
    require(type(observed) in (float, int) and (not isinstance(observed, float) or math.isfinite(observed))
            and observed <= clock(tx) and (not fresh or observed >= clock(tx) - 60), '等待来源时间不可用')
    # Waiting/progress increments the task version. Only those transitions may
    # reuse this source; a changed object/goal needs a new query, even if reverted.
    version = evidence.get('task_version')
    require(type(version) is int and 0 < version <= record.version, '等待来源版本不可用')
    changes = tx.all('''SELECT e.payload FROM task_changes c JOIN support_events e
        ON e.session_id=c.session_id AND e.sequence=c.sequence
        WHERE c.user_id=? AND c.task_id=? AND c.version>? ORDER BY c.version''',
        (runs.user, record.task_id, version))
    require(len(changes) == record.version - version, '等待来源版本链不完整')
    for row in changes:
        change = json.loads(row['payload'])
        before, after = change['before'], change['after']
        require(before['goal'] == after['goal'] and before['details'] == after['details'], '事项目标或对象已改变')
    stamp = evidence.get('provenance') or {}
    require(stamp.get('tasks', {}).get(record.task_id) == version, '等待来源凭据不完整')
    require(runs.guard.valid({**stamp, 'tasks': {**stamp['tasks'], record.task_id: record.version}}, tx=tx),
            '等待来源凭据已失效')
    return evidence


def checked_data(result):
    require(isinstance(result, dict) and result.get('ok') is True and result.get('success') is True,
            '业务查询未成功')
    require(any(s.get('kind') == 'mysql' for s in result.get('sources', []) if isinstance(s, dict)),
            '业务查询来源未核实')
    require(timestamp(result.get('observed_at')) <= timestamp(datetime.now(timezone.utc)), '业务查询时间位于未来')
    require(isinstance(result.get('data'), dict), '业务查询结果结构不完整')
    return result['data']


def order_scope(details, row):
    require(row.get('order_id') and row.get('platform_code'), '缺少订单归属')
    for key in ('order_id', 'platform_code'):
        require(details.get(key) in (None, row[key]), '查询与事项订单不一致')
    return {key: row[key] for key in ('order_id', 'platform_code')}


def line_scope(details, rows):
    require(not details.get('item_id') or not details.get('order_item_id')
            or details['item_id'] == details['order_item_id'], '商品行编号相互冲突')
    require(details.get('quantity') is None or type(details['quantity']) is int and details['quantity'] > 0,
            '商品数量未明确')
    fields = {'item_id': 'order_item_id', 'order_item_id': 'order_item_id',
              'sku_id': 'sku_id', 'product_id': 'product_id', 'quantity': 'quantity'}
    selected = {column: details[key] for key, column in fields.items() if details.get(key) is not None}
    if selected:
        require(any(all(r.get(k) == v for k, v in selected.items()) for r in rows), '查询未覆盖事项商品行或数量')
    return selected


def query_binding(evidence, record, originals):
    name, args, result = evidence['tool'], evidence['arguments'], evidence['result']
    data, details = checked_data(result), record.details
    binding = dict(contract=2, evidence_id=evidence['event_id'], scope_details=details,
                   scope_goal=record.goal)
    if name == 'get_logistics':
        target = unref(args['target_ref'], {'order', 'shipment', 'return_shipment', 'after_sale'})
        rows = [data['shipment']] if data.get('shipment') else data.get('items', [])
        candidates = [row for row in rows if row.get('tracking_number')
                      and details.get('tracking_number') in (None, row['tracking_number'])
                      and details.get('shipment_id') in (None, row.get('shipment_id'))
                      and details.get('return_shipment_id') in (None, row.get('return_shipment_id'))]
        require(len(candidates) == 1, '需要明确唯一运单，不能选择多包裹中的第一票')
        row = candidates[0]
        key = 'return_shipment_id' if row.get('return_shipment_id') else 'shipment_id'
        kind = 'return_shipment' if key == 'return_shipment_id' else 'shipment'
        require(row.get(key), '缺少物流对象编号')
        require(target[0] not in {'shipment', 'return_shipment'} or target == [kind, row[key]], '物流引用与回执不一致')
        if target[0] == 'order':
            require(target[1:] == [row.get('platform_code'), row.get('order_id')], '物流订单引用不一致')
        if target[0] == 'after_sale':
            require(row.get('after_sale_id') == target[1], '物流售后引用不一致')
        require(details.get('after_sale_id') in (None, row.get('after_sale_id')), '物流售后范围不一致')
        explicit = target[0] in {'shipment', 'return_shipment'} or bool(details.get('tracking_number') or details.get(key))
        require(explicit or not result.get('next_cursor') and data.get('total', 1) == 1, '分页结果不能代表唯一包裹')
        binding.update(order_scope(details, row), kind='query_logistics', target_ref=ref(kind, row[key]),
                       tracking_number=row['tracking_number'], after_sale_id=row.get('after_sale_id'),
                       line_scope=line_scope(details, row.get('items', [])),
                       completion='tracking_delivered' if explicit and kind == 'shipment' else 'manual')
        if any('未收到' in text or '没收到' in text for text in originals):
            binding['completion'] = 'manual'
    else:
        if name == 'get_after_sale_status':
            sale = data.get('after_sale') or {}
            aid = sale.get('after_sale_id')
            require(aid == args['after_sale_id'], '售后引用与回执不一致')
            scope = order_scope(details, sale)
            selected = line_scope(details, data.get('purchase_items', []))
        else:
            target = unref(args['target_ref'], {'order', 'after_sale'})
            order = unref(data.get('order_ref', ''), {'order'})
            scope = order_scope(details, dict(platform_code=order[1], order_id=order[2]))
            require(target[0] != 'order' or target == order, '退款订单引用不一致')
            ids = {r.get('after_sale_id') for r in data.get('items', [])}
            aid = target[1] if target[0] == 'after_sale' else details.get('after_sale_id')
            if not aid:
                require(not result.get('next_cursor') and len(ids) == 1, '需要明确退款所属的售后申请')
                aid = next(iter(ids))
            require(aid and (not ids or aid in ids), '退款回执未覆盖当前售后申请')
            # Line facts come from a fresh owned after-sale read before publishing.
            selected = {k: v for k, v in details.items() if k in {'item_id', 'order_item_id', 'sku_id', 'product_id', 'quantity'}}
        require(aid and details.get('after_sale_id') in (None, aid), '售后申请范围不一致')
        binding.update(scope, kind='query_sale', after_sale_id=aid, line_scope=selected,
                       completion='commerce_result')
        if name == 'get_refunds' or sale.get('kind') in {'refund_only', 'return_refund', 'price_protection', 'order_cancel'}:
            binding['completion'] = 'refund_channel_result'
            if any('到账' in t or '银行入账' in t for t in [record.goal, *originals]):
                binding['completion'] = 'manual'
            if name == 'get_refunds' and target[0] == 'order' and not details.get('after_sale_id'):
                binding['completion'] = 'manual'
    return binding


def validate_binding(tx, runs, record, binding):
    require(binding.get('contract') == 2 and binding.get('evidence_id') == record.waiting_for.get('evidence_id'),
            '外部等待契约或来源不一致')
    require(binding.get('scope_details') == record.details and binding.get('scope_goal') == record.goal,
            '外部等待对象或目标已改变')
    query_source(tx, runs, record, fresh=False)


def verify_sale(svc, binding, result):
    data = checked_data(result)
    sale = data.get('after_sale') or {}
    require(sale.get('after_sale_id') == binding['after_sale_id'], '售后查询对象改变')
    require(sale.get('status') in {'pending_review', 'approved', 'processing', 'completed', 'rejected', 'cancelled', 'closed'},
            '售后状态尚未核实')
    order_scope(binding, sale)
    line_scope(binding['scope_details'], data.get('purchase_items', []))
    with svc.store.transaction() as tx:
        order = svc.owned_order(tx, ref('order', binding['platform_code'], binding['order_id']))
    require(binding['scope_details'].get('store_id') in (None, order.get('merchant_id')), '商家范围改变')
    return data


def logistics_progress(svc, binding):
    from app.services.commerce.common import at
    from app.tasks.external_evidence import observation
    result = svc.read('get_logistics', {'target_ref': binding['target_ref']})
    data = checked_data(result)
    row = data.get('shipment') or {}
    target = unref(binding['target_ref'], {'shipment', 'return_shipment'})
    key = 'shipment_id' if target[0] == 'shipment' else 'return_shipment_id'
    require(row.get(key) == target[1] and row.get('tracking_number') == binding['tracking_number'], '运单对象改变')
    order_scope(binding, row)
    require(row.get('after_sale_id') == binding.get('after_sale_id'), '运单售后归属改变')
    line_scope(binding['scope_details'], row.get('items', []))
    with svc.store.transaction() as tx:
        order = svc.owned_order(tx, ref('order', binding['platform_code'], binding['order_id']))
    require(binding['scope_details'].get('store_id') in (None, order.get('merchant_id')), '商家范围改变')
    states = {'created': ('submitted', 10, '这票包裹已登记，尚未核实实际发出。'),
              'shipped': ('return_shipped', 20, '这票包裹已有发出记录。'),
              'in_transit': ('in_transit', 25, '这票包裹正在运输，尚未核实签收。'),
              'delivered': ('delivered', 30, '物流系统记录这票包裹已签收。')}
    require(row.get('status') in states, '物流状态需要人工核实')
    phase, rank, content = states[row['status']]
    if phase == 'delivered':
        if target[0] == 'shipment':
            require(row.get('delivered_at') and row.get('shipped_at')
                    and at(row['shipped_at']) <= at(row['delivered_at']) <= at(result['observed_at']),
                    '签收状态缺少有效时间凭据')
        else:
            require(any(e.get('status') == 'delivered' and e.get('source') and e.get('event_time')
                        and at(e['event_time']) <= at(result['observed_at'])
                        for e in row.get('events', [])), '退货签收缺少物流事件凭据')
    return observation(binding, phase=phase, rank=rank, content=content, source='mysql',
                       observed_at=result['observed_at'], reference=target[1])


def refund_progress(svc, binding, sale_result):
    from decimal import Decimal
    from app.services.commerce.common import at, money
    from app.tasks.external_evidence import observation
    data = verify_sale(svc, binding, sale_result)
    sale = data['after_sale']
    result = svc.read('get_refunds', {'target_ref': ref('after_sale', binding['after_sale_id'])})
    refunds = checked_data(result)
    require(unref(refunds.get('order_ref', ''), {'order'}) == ['order', binding['platform_code'], binding['order_id']],
            '退款订单归属不一致')
    rows = refunds.get('items', [])
    require(all(r.get('after_sale_id') == binding['after_sale_id'] for r in rows), '退款申请归属不一致')
    phase, rank, content = 'submitted', 10, '售后申请已登记，渠道退款结果仍待核实。'
    if sale['status'] in {'rejected', 'cancelled', 'closed'}:
        phase, rank, content = 'business_rejected', 90, '售后申请已拒绝、撤回或关闭，需要人工核实。'
    elif any(r.get('status') == 'failed' for r in rows):
        phase, rank, content = 'business_rejected', 90, '退款渠道报告处理失败，需要人工核实。'
    elif rows:
        phase, rank, content = 'refunding', 50, '退款正在核实，尚未确认本次申请的全部渠道退款成功。'
        expected = money(sale.get('requested_amount'))
        with svc.store.transaction() as tx:
            order = svc.owned_order(tx, ref('order', binding['platform_code'], binding['order_id']))
        succeeded = [r for r in rows if r.get('status') == 'succeeded']
        complete = (not result.get('next_cursor') and len(rows) == refunds.get('total')
                    and expected > 0 and len(succeeded) == len(rows)
                    and all(r.get('currency') == order['currency'] and r.get('provider_refund_id')
                            and r.get('completed_at') and at(r['completed_at']) <= at(result['observed_at'])
                            and money(r['amount']) > 0 for r in rows)
                    and sum((money(r['amount']) for r in succeeded), Decimal(0)) == expected)
        if complete:
            phase, rank, content = 'refund_channel_succeeded', 60, '已记录本次售后申请的全部渠道退款成功凭据；银行卡实际入账尚未核实。'
    elif data.get('return_shipment'):
        phase, rank, content = 'return_shipped', 30, '已登记退货运单，仓库验收与退款结果仍待核实。'
    elif data.get('return_authorization'):
        phase, rank, content = 'awaiting_return', 25, '已取得退货授权，退货及退款结果仍待核实。'
    elif sale['status'] in {'approved', 'processing'}:
        phase, rank, content = 'approved', 20, '售后申请正在处理，退款结果仍待核实。'
    return observation(binding, phase=phase, rank=rank, content=content, source='mysql',
                       observed_at=result['observed_at'], reference=binding['after_sale_id'])
