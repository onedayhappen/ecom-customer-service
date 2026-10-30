# 文件说明：实现持久本地模拟处理方，按配置推进退款、退货和演示票据。
"""Persistent development provider. Outcomes are selected by server configuration only."""
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
from uuid import uuid4

from app.config.settings import settings
from app.services.commerce.common import BusinessError, at, decoded, digest, dumps, utcnow

DEMO_NOTICE = '本次为本地模拟演示，未发生真实资金流转、发货或正式开票。'


def invoice_document(request_id, payload, issued_at):
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    if not settings.commerce_invoice_root:
        raise BusinessError('unsupported_capability', '演示票据文件存储尚未配置')
    key = 'demo/' + digest(request_id) + '.pdf'
    path = Path(settings.commerce_invoice_root).resolve() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    # Invariant output lets a retry after file-write/DB-commit failure verify and reuse it.
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842), invariant=1)
    pdf.setTitle('演示票据，非正式发票')
    pdf.setFont('STSong-Light', 22)
    pdf.drawString(45, 780, '演示票据，非正式发票')
    pdf.setFont('STSong-Light', 12)
    lines = ['DEMO ONLY - NOT A TAX INVOICE',
             '申请编号：' + payload['invoice_id'], '订单编号：' + payload['order_id'],
             '开票抬头：' + payload['title'], '票据类型：' + payload['invoice_type'],
             '金额：' + str(payload['amount']) + ' ' + payload['currency'],
             '生成时间：' + issued_at, DEMO_NOTICE]
    y = 735
    for line in lines:
        for start in range(0, len(line), 38):
            pdf.drawString(45, y, line[start:start + 38]); y -= 24
    pdf.save()
    data = buffer.getvalue(); checksum = sha256(data).hexdigest()
    if path.exists():
        if sha256(path.read_bytes()).hexdigest() != checksum:
            raise BusinessError('insufficient_data', '已有演示票据文件校验失败')
    else:
        temp = path.with_suffix('.' + uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return dict(invoice_number='DEMO-' + digest(request_id)[:24], document_key=key,
                mime_type='application/pdf', sha256=checksum, issued_at=issued_at,
                amount=payload['amount'],currency=payload['currency'],title_digest=digest([payload['title'],payload.get('tax_number')]))


class Simulator:
    def __init__(self, store, user):
        self.store, self.user = store, user

    def call(self, operation, request_id, kind, payload):
        if operation not in {'submit', 'query'} or kind not in {'refund_only', 'return_refund', 'invoice'}:
            raise BusinessError('unsupported_capability', '模拟渠道不支持此操作')
        stable = {k: v for k, v in payload.items() if k not in {'return_shipment', 'return_shipments'}}
        lost = False
        with self.store.transaction(write=True) as tx:
            # Serialize alongside application creation/cancellation, then provider state.
            order = tx.one('SELECT * FROM orders WHERE platform_code=%s AND order_id=%s AND user_id=%s FOR UPDATE',
                           (payload['platform_code'], payload['order_id'], self.user))
            if not order or not order['is_demo'] or payload['user_id'] != self.user or payload['request_id'] != request_id:
                raise BusinessError('not_eligible', '模拟请求与演示订单归属不一致')
            row = tx.one('SELECT * FROM simulated_provider_requests WHERE request_id=%s FOR UPDATE', (request_id,))
            if row and (row['payload_digest'] != digest(stable) or row['kind'] != kind or row['user_id'] != self.user):
                raise BusinessError('invalid_arguments', '相同模拟请求编号的内容不一致')
            now = utcnow()
            if not row:
                if operation == 'query': return {'status': 'not_found'}
                scenario = settings.commerce_simulator_scenario
                if scenario not in {'success', 'reject', 'delay', 'lost_response'}:
                    raise BusinessError('temporarily_unavailable', '模拟场景配置无效')
                step = settings.commerce_simulator_step_seconds
                response = dict(status='accepted', reference='sim-' + digest(request_id)[:32],
                                **{k: payload[k] for k in ('request_id', 'user_id', 'platform_code', 'order_id', 'target_id')},
                                items=payload.get('items', []), message=DEMO_NOTICE)
                tx.execute('''INSERT INTO simulated_provider_requests
                    (request_id,user_id,kind,target_id,payload_digest,scenario,phase,next_at,step_seconds,result,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s)''',
                    (request_id, self.user, kind, payload['target_id'], digest(stable), scenario,
                     now + timedelta(seconds=step * (6 if scenario == 'delay' else 1)), step, dumps(response), now, now))
                lost = scenario == 'lost_response'
            else:
                response = decoded(row['result'])
                if response['status'] not in {'completed', 'rejected'} and now >= at(row['next_at']):
                    phase = row['phase'] + 1
                    response['status'] = 'processing'
                    if row['scenario'] == 'reject':
                        response.update(status='rejected', message='模拟审核拒绝：演示拒绝场景')
                    elif kind == 'return_refund':
                        self.advance_return(tx, row, payload, response, phase, now)
                    elif phase >= 2:
                        response['status'] = 'completed'
                    if response['status'] == 'completed':
                        # Stable time survives an interrupted file write before this transaction commits.
                        completed = at(row['next_at']).isoformat() + 'Z'
                        if kind == 'invoice': response['invoice'] = invoice_document(request_id, payload, completed)
                        else:
                            response['refunds'] = [dict(payment_id=p['payment_id'], amount=p['amount'], currency=payload['currency'],
                                provider_refund_id='sim-refund-' + digest([request_id, p['payment_id']])[:32], completed_at=completed)
                                for p in payload['payments']]
                    tx.execute('UPDATE simulated_provider_requests SET phase=%s,next_at=%s,result=%s,updated_at=%s WHERE request_id=%s',
                               (phase, now + timedelta(seconds=row['step_seconds']), dumps(response), now, request_id))
        if lost:
            raise BusinessError('result_unknown', '模拟渠道已持久受理，但提交响应丢失')
        return decoded(dumps(response))

    def advance_return(self, tx, row, payload, response, phase, now):
        response.setdefault('return_authorization', dict(address={'detail': '演示退货地址，请勿实际寄件'},
            carriers=[dict(code='DEMO', name='演示物流')], requirements=['仅登记演示运单，请勿实际寄件'],
            deadline=(at(row['created_at']) + timedelta(days=7)).isoformat() + 'Z'))
        shipments = payload.get('return_shipments', [])
        events = []; received = {}
        for ship in shipments:
            previous = next((e for e in response.get('return_events', []) if e['return_shipment_id'] == ship['return_shipment_id']), None)
            states = ['collected', 'in_transit', 'received', 'accepted']
            status = states[min(states.index(previous['status']) + 1, 3)] if previous else states[0]
            event = previous if previous and previous['status'] == status else dict(return_shipment_id=ship['return_shipment_id'],
                status=status, description='模拟退货物流／验货：' + status, event_time=now.isoformat() + 'Z')
            events.append(event)
            if status == 'accepted':
                for item in tx.all('SELECT order_item_id,quantity FROM return_shipment_items WHERE return_shipment_id=%s', (ship['return_shipment_id'],)):
                    received[item['order_item_id']] = received.get(item['order_item_id'], 0) + item['quantity']
        response['return_events'] = events
        if received == {i['order_item_id']: i['quantity'] for i in payload['items']}:
            response['status'] = 'completed'
