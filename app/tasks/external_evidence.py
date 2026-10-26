"""Translate owned business reads into narrowly scoped progress, never writes.

Only adapters called by the server produce observations. There is deliberately
no public endpoint accepting a client-supplied phase or completion flag.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from app.tasks.business_models import AfterSaleRecord


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
            or goal == 'commerce_result' and value['phase'] in {'service_completed','invoice_issued'}
            or goal == 'warehouse_received' and value['phase'] == 'warehouse_received'
            or goal == 'tracking_delivered' and value['phase'] == 'delivered'
            or goal == 'local_invoice' and value['phase'] == 'issued_local')


class BusinessProgress:
    def read(self, user, binding):
        from app.services.commerce import service
        from app.services.commerce.common import ref,decoded
        svc=service(user)
        if binding.get('kind') not in {'commerce_sale','commerce_invoice'}:
            raise ExternalEvidenceError('历史业务接口不支持该进度查询')
        with svc.store.transaction() as tx:
            receipt=svc.receipt(tx,binding['operation_id'])
            value=decoded(receipt['result']) if receipt else {}
            key='after_sale_id' if binding['kind']=='commerce_sale' else 'invoice_id'
            if not value.get(key) or any(value.get(k)!=binding.get(k) for k in (key,'order_id','platform_code')):
                raise ExternalEvidenceError('业务回执与跟进对象不符')
        phase,rank,content='submitted',10,'业务申请已登记，处理结果仍待核实。'
        if binding['kind']=='commerce_invoice':
            result=svc.read('get_invoices',dict(target_ref=ref('invoice',binding['invoice_id'])))
            row=result['data']['items'][0]
            if row['status']=='rejected': phase,rank,content='business_rejected',90,'开票申请被处理方拒绝，需要人工继续核实。'
            elif row['status']=='issued' and any(i['status']=='issued' for i in row['issued_invoices']):
                phase,rank,content='invoice_issued',60,'已记录正式发票，可查询发票文件。'
            elif row['status']=='processing': phase,rank,content='approved',20,'开票申请正在处理，尚未取得正式发票。'
        else:
            result=svc.read('get_after_sale_status',dict(after_sale_id=binding['after_sale_id']))
            data=result['data']; sale=data['after_sale']; work=data.get('work_order') or {}
            if sale['status'] in {'rejected','cancelled','closed'}:
                phase,rank,content='business_rejected',90,'售后申请已拒绝、撤回或关闭，需要核实后续处理。'
            elif sale['kind']=='order_cancel' and sale['status']=='completed' and work.get('provider_reference'):
                phase,rank,content='service_completed',60,'订单已取消，已记录原支付渠道退款成功回执；银行卡入账时间以支付渠道为准。'
            elif sale['kind']=='repair' and sale['status']=='completed' and work.get('provider_reference'):
                phase,rank,content='service_completed',60,'维修处理方已报告工单完成，可查询工单详情。'
            elif sale['kind']=='exchange' and sale['status']=='completed' and work.get('provider_reference'):
                sid=work.get('details',{}).get('replacement_shipment_id')
                if not sid: raise ExternalEvidenceError('缺少换货补发关联')
                with svc.store.transaction() as tx:
                    order=svc.owned_order(tx,ref('order',binding['platform_code'],binding['order_id']))
                    shipment=next((s for s in svc.order_rows(tx,'shipments',order) if s['shipment_id']==sid),None)
                if shipment and shipment['status']=='delivered' and shipment['delivered_at']:
                    phase,rank,content='service_completed',60,'换货补发包裹已有签收记录。'
                else: phase,rank,content='return_shipped',50,'换货处理方已报告补发，签收结果仍待核实。'
            elif sale['status']=='completed':
                phase,rank,content='refunding',50,'售后系统记录处理完成；渠道退款记录可查询，银行卡入账尚未核实。'
            elif data.get('return_shipment'):
                phase,rank,content='return_shipped',30,'已登记退货运单，可查询逆向物流记录。'
            elif data.get('return_authorization'):
                phase,rank,content='awaiting_return',25,'已取得退货授权，请查询退货地址、期限和材料要求。'
            elif sale['status'] in {'approved','processing'}:
                phase,rank,content='approved',20,'售后申请正在处理，最终结果仍待核实。'
        return observation(binding,phase=phase,rank=rank,content=content,source='mysql',observed_at=result['observed_at'],reference=binding[key])
