"""Translate owned business reads into narrowly scoped progress, never writes.

Only adapters called by the server produce observations. There is deliberately
no public endpoint accepting a client-supplied phase or completion flag.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from app.tools.commerce_models import AfterSaleRecord


class ExternalEvidenceError(ValueError):
    pass


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ExternalEvidenceError('外部进度缺少带时区的来源时间')
    return value.astimezone(timezone.utc).isoformat()


def submission_only(text):
    """A conservative explicit user goal; an ordinary refund asks for its result."""
    if re.search(r'不要|不是|不仅|不只|not\s+only|don.t', text, re.I):
        return False
    if re.search(r'(?:只需|只要|仅需|只帮我|只需要).{0,12}(?:提交|登记).{0,12}申请|'
                 r'(?:提交|登记).{0,12}申请(?:就行|即可)|only\s+submit', text, re.I):
        return True
    return bool(re.search(r'(?:提交|登记).{0,12}申请', text) and not re.search(r'到账|跟进|后续|解决|结果|收货', text))


def warehouse_goal(text):
    return bool(re.search(r'(?:确认|核实|跟进).{0,8}仓库.{0,8}收货|仓库收货即可', text)
                and not re.search(r'到账|退款|未收货|没有收货', text))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# These codes are the local business adapter contract, not words inferred from
# a model reply. A generic "completed" is not proof of money reaching an account.
AFTER_SALE_PHASES = {
    'submitted': ('submitted', 10, '售后申请已受理，最终处理结果待确认。'),
    'pending': ('submitted', 10, '售后申请已受理，最终处理结果待确认。'),
    'approved': ('approved', 20, '售后申请已审核，后续办理仍待确认。'),
    'awaiting_return': ('awaiting_return', 25, '售后系统正在等待退货寄回。'),
    'return_shipped': ('return_shipped', 30, '退货已寄回，尚未确认仓库收货。'),
    'warehouse_received': ('warehouse_received', 40, '仓库已确认收货，退款结果仍待确认。'),
    'refunding': ('refunding', 50, '退款正在处理，尚未确认资金到账。'),
    'refunded': ('refunding', 50, '系统记录退款已处理，尚缺资金到账凭据。'),
}


def after_sale_observation(binding, result):
    if result.get('success') is not True or result.get('source') not in {'mysql', 'mysql_demo'}:
        raise ExternalEvidenceError('售后来源不可用')
    rows = []
    for raw in result.get('records', []):
        row = AfterSaleRecord.model_validate(raw).model_dump(mode='json')
        if row['order_id'] != binding['order_id']:
            raise ExternalEvidenceError('售后记录不属于当前订单')
        if binding.get('after_sale_id') and row['after_sale_id'] != binding['after_sale_id']:
            continue
        if binding.get('operation_id') and row.get('request_id') != binding['operation_id']:
            continue
        if binding.get('kind') in {'refund', 'return'} and row['kind'] != binding['kind']:
            continue
        # A line-level matter cannot use an order-wide snapshot as its proof.
        if any(binding.get(key) is not None and row.get(key) != binding[key]
               for key in ('item_id', 'item_name', 'sku_id', 'quantity', 'tracking_number', 'store_id', 'platform_code', 'product_id')):
            continue
        rows.append(row)
    if len(rows) > 1:
        raise ExternalEvidenceError('存在多个售后申请，需要明确申请编号')
    if not rows:
        current = result.get('current_order_refund')
        if (not current or binding.get('after_sale_id') or binding.get('kind') == 'return'
                or binding.get('platform_code', 'pdd') != 'pdd'
                or any(binding.get(key) is not None for key in ('item_id', 'item_name', 'sku_id', 'quantity', 'tracking_number', 'store_id', 'product_id'))):
            raise ExternalEvidenceError('业务接口尚不能核实该申请或商品范围')
        # The original order table proves only the whole-order application.
        if current.get('source') != 'mysql_order_fields' or current.get('status') not in {'submitted', 'pending'}:
            raise ExternalEvidenceError('订单字段不能证明售后最终结果')
        return observation(binding, phase='submitted', rank=10,
            content='退款申请已登记，审核结果和资金到账仍待确认。',
            source='mysql_order_fields', observed_at=result.get('observed_at'),
            reference=binding.get('operation_id') or binding['order_id'],
            is_demo=result.get('is_demo') is True)
    row = rows[0]
    if row['source'] not in {'mysql', 'mysql_demo', 'mysql_after_sales'}:
        raise ExternalEvidenceError('售后记录来源尚未核实')
    if row['status'] not in AFTER_SALE_PHASES:
        raise ExternalEvidenceError('售后状态需要人工核实')
    if datetime.fromisoformat(timestamp(row['observed_at'])) > datetime.fromisoformat(timestamp(result.get('observed_at'))):
        raise ExternalEvidenceError('售后记录时间晚于本次查询')
    phase, rank, content = AFTER_SALE_PHASES[row['status']]
    if row['status'] == 'refunded' and row.get('settled_at') and row.get('transaction_id'):
        if timestamp(row['settled_at']) > timestamp(row['observed_at']):
            raise ExternalEvidenceError('资金凭据时间与记录不一致')
        phase, rank, content = 'funds_settled', 60, '退款业务系统已确认资金到账。'
    return observation(binding, phase=phase, rank=rank, content=content, source=row['source'],
        observed_at=row['observed_at'], reference=row['after_sale_id'], is_demo=row['is_demo'],
        deadline=row.get('deadline'), required_materials=row.get('required_materials') or [],
        transaction_id=row.get('transaction_id') if phase == 'funds_settled' else None)


def observation(binding, *, phase, rank, content, source, observed_at, reference, is_demo=False,
                deadline=None, required_materials=(), transaction_id=None):
    from app.memory.source_policy import memory_safe
    if any(not memory_safe(item) for item in required_materials):
        raise ExternalEvidenceError('材料要求含不应索要的敏感信息')
    value = dict(binding=binding, phase=phase, rank=rank, content=content, source=source,
                 observed_at=timestamp(observed_at), reference=reference, is_demo=is_demo or binding.get('is_demo') is True,
                 deadline=timestamp(deadline) if deadline else None,
                 required_materials=list(required_materials), transaction_id=transaction_id)
    value['event_id'] = digest(value)
    # Repeated reads may have a new observation time, but unchanged progress is
    # not another customer notification.
    value['progress_key'] = digest({k: v for k, v in value.items() if k not in {'observed_at', 'event_id'}})
    return value


def finished(binding, value):
    """Completion is bounded by both the user's goal and a business receipt."""
    goal = binding.get('completion')
    if goal == 'submission':
        return value['phase'] in {'submitted', 'approved', 'awaiting_return', 'return_shipped',
                                  'warehouse_received', 'refunding', 'funds_settled'}
    return (goal == 'refund_settled' and value['phase'] == 'funds_settled'
            or goal == 'warehouse_received' and value['phase'] == 'warehouse_received'
            or goal == 'tracking_delivered' and value['phase'] == 'delivered'
            or goal == 'local_invoice' and value['phase'] == 'issued_local')


class BusinessProgress:
    def read(self, user, binding):
        from app.tools.request_context import get_current_user_id
        from app.config.settings import settings
        if not user or get_current_user_id() != user or not settings.order_db_dsn:
            raise ExternalEvidenceError('缺少已验证的业务身份或数据源')
        if binding['kind'] in {'refund', 'return'}:
            from app.tools.commerce import invoke
            args = {k: binding[k] for k in ('order_id', 'after_sale_id', 'kind') if binding.get(k)}
            return after_sale_observation(binding, invoke('query_after_sale', args))
        if binding['kind'] == 'logistics':
            return self.logistics(user, binding)
        if binding['kind'] == 'invoice':
            from app.tools.commerce import invoke
            result = invoke('query_invoice', dict(order_id=binding['order_id'], invoice_id=binding['invoice_id']))
            invoices = result.get('invoices', [])
            if result.get('success') is not True or len(invoices) != 1:
                raise ExternalEvidenceError('本地发票记录无法核实')
            invoice = invoices[0]
            if (invoice.get('order_id') != binding['order_id'] or invoice.get('invoice_id') != binding['invoice_id']
                    or invoice.get('status') != 'issued_local' or invoice.get('source') != 'mysql_demo'
                    or invoice.get('is_demo') is not True):
                raise ExternalEvidenceError('本地发票记录范围或状态有变化')
            return observation(binding, phase='issued_local', rank=10,
                content='本地演示发票记录已核实，不是税务发票。', source='mysql_demo',
                observed_at=invoice['issued_at'], reference=invoice['invoice_id'], is_demo=True)
        raise ExternalEvidenceError('当前接口无法跟进这类外部结果')

    def logistics(self, user, binding):
        # Fresh DAO read deliberately bypasses the conversational query cache.
        from app.config.settings import settings
        from app.data.logistics_dao import MySqlLogisticsDAO
        tracking = binding.get('tracking_number')
        if not tracking:
            raise ExternalEvidenceError('需先明确要跟进的运单号')
        row = MySqlLogisticsDAO(settings.order_db_dsn).query_by_tracking(user, tracking)
        if not row or row.order_id != binding['order_id'] or row.platform_code != binding.get('platform_code', 'pdd'):
            raise ExternalEvidenceError('运单不属于当前事项')
        stages = {'in_transit': ('in_transit', 10, '这票包裹正在运输中。'),
                  '运输中': ('in_transit', 10, '这票包裹正在运输中。'),
                  'delivering': ('delivering', 20, '这票包裹正在派送中。'),
                  '派送中': ('delivering', 20, '这票包裹正在派送中。'),
                  'delivered': ('delivered', 30, '物流系统记录这票包裹已签收。'),
                  '已签收': ('delivered', 30, '物流系统记录这票包裹已签收。')}
        if row.status not in stages:
            raise ExternalEvidenceError('物流状态需要重新核实')
        phase, rank, content = stages[row.status]
        return observation(binding, phase=phase, rank=rank, content=content,
            source='mysql_logistics', observed_at=datetime.now(timezone.utc), reference=tracking)
