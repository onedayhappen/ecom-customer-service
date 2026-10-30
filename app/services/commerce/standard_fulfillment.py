# 文件说明：核验并原子应用普通退款、退货与首次开票的渠道回执。
"""Ordinary refunds and first invoices: checked, transactional provider outcomes."""
from decimal import Decimal
from uuid import uuid4

from app.services.commerce.common import BusinessError, at, decoded, digest, dumps, money, utcnow
from app.services.commerce.providers import invoice_file, provider_mode
from app.services.commerce.simulator import DEMO_NOTICE


class StandardFulfillment:
    def processing_plan(self, tx, order, kind, review):
        self.provider_required(kind)
        mode = provider_mode()
        if mode == 'simulator' and not order.get('is_demo'):
            raise BusinessError('not_eligible', '模拟渠道只允许显式标记的演示订单')
        if mode == 'http' and order.get('is_demo'):
            raise BusinessError('not_eligible', '演示订单不能发送到真实渠道')
        if kind == 'invoice':
            from app.config.settings import settings
            if mode == 'simulator' and not settings.commerce_invoice_root:
                raise BusinessError('unsupported_capability', '演示票据文件目录尚未配置')
            details = dict(amount=money(order['paid_amount']), currency=order['currency'])
            review.update(details, amount_kind='invoice_total')
        else:
            remaining = money(review['amount']); allocations = []
            payments = self.order_rows(tx, 'payment_transactions', order)
            for payment in sorted(payments, key=lambda p: p['payment_id']):
                if payment['status'] != 'succeeded': continue
                if payment['currency'] != order['currency'] or not payment.get('provider_txn_id'):
                    raise BusinessError('insufficient_data', '原支付币种或流水凭据不完整')
                refunds = tx.all('SELECT amount,status,currency FROM refund_transactions WHERE payment_id=%s', (payment['payment_id'],))
                if any(r['currency'] != order['currency'] or r['status'] not in {'pending','processing','succeeded','failed','cancelled'} for r in refunds):
                    raise BusinessError('insufficient_data', '原支付退款记录不完整')
                held = sum((money(r['amount']) for r in refunds if r['status'] in {'pending','processing','succeeded'}), Decimal(0))
                available = money(payment['amount']) - held
                if available < 0: raise BusinessError('insufficient_data', '原支付退款占用超额')
                amount = min(remaining, available)
                if amount > 0:
                    allocations.append(dict(payment_id=payment['payment_id'], amount=amount))
                    remaining -= amount
            if remaining or not allocations:
                raise BusinessError('insufficient_data', '缺少足额且可核实的原支付流水')
            details = dict(amount=review['amount'], currency=order['currency'], items=review['items'], payments=allocations)
        review.update(is_demo=mode == 'simulator', source='simulated_provider' if mode == 'simulator' else 'provider')
        if mode == 'simulator': review['impact'] += ' ' + DEMO_NOTICE
        return dict(details, provider_mode=mode)

    def enqueue_standard_sale(self, tx, order, oid, aid, args, details):
        demo = details['provider_mode'] == 'simulator'; now = utcnow(); wid = uuid4().hex
        tx.execute('UPDATE after_sale_requests SET is_demo=%s WHERE after_sale_id=%s', (demo, aid))
        tx.execute('INSERT INTO service_work_orders(work_order_id,after_sale_id,kind,status,details,source,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                   (wid, aid, args['kind'], 'pending_review', dumps(details), 'simulated_provider' if demo else 'local', now))
        # Reserve every payment allocation before handing the job to a worker.
        for p in details['payments']:
            tx.execute('''INSERT INTO refund_transactions(refund_id,after_sale_id,payment_id,provider,amount,currency,status,requested_at,is_demo)
                VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s)''',
                (digest([oid, p['payment_id']]), aid, p['payment_id'], 'simulated_provider' if demo else 'provider', p['amount'], details['currency'], now, demo))
        self.queue_provider(tx, oid, args['kind'], wid, order, dict(details, after_sale_id=aid, work_order_id=wid))
        return wid

    def apply_standard_response(self, tx, job, order, response):
        payload = decoded(self.cipher.open(job['payload_ciphertext'], 'provider:' + job['request_id'], job['key_version']))
        if any(response.get(k) != payload.get(k) for k in ('request_id','user_id','platform_code','order_id','target_id')):
            raise BusinessError('insufficient_data', '处理回执与原请求、用户或对象不一致')
        demo = job['provider_mode'] == 'simulator'; source = 'simulated_provider' if demo else 'provider'
        now = utcnow(); status = response['status']
        if job['kind'] == 'invoice':
            invoice = tx.one('SELECT * FROM invoice_requests WHERE invoice_id=%s AND user_id=%s FOR UPDATE', (job['target_id'], self.user))
            if not invoice or invoice['request_id'] != job['request_id']:
                raise BusinessError('insufficient_data', '开票申请归属不一致')
            if status == 'completed':
                doc = response.get('invoice')
                if not doc or at(doc['issued_at']) > now or at(doc['issued_at']) < at(invoice['created_at']):
                    raise BusinessError('insufficient_data', '缺少有效票据及生成时间')
                if (doc.get('amount') is None or money(doc['amount'])!=money(payload['amount'])
                    or doc.get('currency')!=payload['currency']
                    or doc.get('title_digest')!=digest([payload['title'],payload.get('tax_number')])):
                    raise BusinessError('insufficient_data', '票据金额、币种或抬头与已确认内容不一致')
                invoice_file(doc)
                tx.execute('''INSERT INTO issued_invoices(issued_invoice_id,invoice_id,invoice_number,status,document_key,mime_type,sha256,provider,issued_at,updated_at,is_demo)
                    VALUES (%s,%s,%s,'issued',%s,%s,%s,%s,%s,%s,%s)''',
                    (digest(job['request_id']), invoice['invoice_id'], doc['invoice_number'], doc['document_key'], doc['mime_type'], doc['sha256'], source, at(doc['issued_at']), now, demo))
            tx.execute('UPDATE invoice_requests SET status=%s,issued_at=%s WHERE invoice_id=%s',
                       ('issued' if status == 'completed' else 'rejected' if status == 'rejected' else 'processing',
                        at(response['invoice']['issued_at']) if status == 'completed' else None, invoice['invoice_id']))
            return status
        work = tx.one('SELECT * FROM service_work_orders WHERE work_order_id=%s FOR UPDATE', (job['target_id'],))
        if not work or work['after_sale_id'] != payload['after_sale_id']:
            raise BusinessError('insufficient_data', '售后工单关联不一致')
        aid = work['after_sale_id']; sale, _ = self.sale(tx, aid)
        expected = {i['order_item_id']: i['quantity'] for i in payload['items']}
        actual = response.get('items', [])
        if len(actual) != len(expected) or {i.get('order_item_id'): i.get('quantity') for i in actual} != expected:
            raise BusinessError('insufficient_data', '售后回执商品行或数量不一致')
        if job['kind'] == 'return_refund':
            self.apply_demo_return(tx, job, aid, response, source)
        pending = tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s FOR UPDATE', (aid,))
        if status == 'completed':
            if job['kind'] == 'return_refund':
                received = tx.all('''SELECT i.order_item_id,SUM(i.quantity) quantity FROM return_shipment_items i
                    JOIN return_shipments s ON s.return_shipment_id=i.return_shipment_id
                    WHERE i.after_sale_id=%s AND s.status='accepted' GROUP BY i.order_item_id''', (aid,))
                if {i['order_item_id']: i['quantity'] for i in received} != expected:
                    raise BusinessError('insufficient_data', '退货尚未按申请商品和数量完整验收')
            refunds = response.get('refunds', [])
            by_payment = {r['payment_id']: r for r in refunds}
            if len(refunds) != len(pending) or len(by_payment) != len(pending):
                raise BusinessError('insufficient_data', '退款完成凭据数量不一致')
            if len({r['provider_refund_id'] for r in refunds})!=len(refunds):
                raise BusinessError('insufficient_data', '退款凭据编号重复')
            if sum((money(r['amount']) for r in pending), Decimal(0)) != money(sale['requested_amount']):
                raise BusinessError('insufficient_data', '退款占用与申请金额不一致')
            for row in pending:
                refund = by_payment.get(row['payment_id'])
                payment = tx.one('SELECT * FROM payment_transactions WHERE payment_id=%s', (row['payment_id'],))
                if (not refund or not payment or payment['user_id'] != self.user
                    or (payment['platform_code'], payment['order_id']) != (order['platform_code'], order['order_id'])
                    or payment['status'] != 'succeeded' or payment['currency'] != row['currency']
                    or money(refund['amount']) != money(row['amount']) or refund['currency'] != row['currency']
                    or not at(sale['created_at']) <= at(refund['completed_at']) <= now):
                    raise BusinessError('insufficient_data', '退款金额、币种、原支付或时间不一致')
                held = tx.one("SELECT COALESCE(SUM(amount),0) amount FROM refund_transactions WHERE payment_id=%s AND status IN ('pending','processing','succeeded')", (row['payment_id'],))['amount']
                if money(held) > money(payment['amount']): raise BusinessError('insufficient_data', '退款总额超过原支付')
                reused=tx.one('SELECT refund_id FROM refund_transactions WHERE provider=%s AND provider_refund_id=%s',(source,refund['provider_refund_id']))
                if reused and reused['refund_id']!=row['refund_id']:
                    raise BusinessError('insufficient_data','退款凭据已用于另一笔退款')
                tx.execute("UPDATE refund_transactions SET status='succeeded',provider_refund_id=%s,completed_at=%s WHERE refund_id=%s",
                           (refund['provider_refund_id'], at(refund['completed_at']), row['refund_id']))
        elif status == 'rejected':
            tx.execute("UPDATE refund_transactions SET status='cancelled' WHERE after_sale_id=%s AND status IN ('pending','processing')", (aid,))
        else:
            tx.execute("UPDATE refund_transactions SET status='processing' WHERE after_sale_id=%s AND status='pending'", (aid,))
        stage = 'completed' if status == 'completed' else 'rejected' if status == 'rejected' else 'refunding'
        if job['kind'] == 'return_refund' and status not in {'completed', 'rejected'}:
            stage = 'inspecting' if response.get('return_events') else 'awaiting_return' if response.get('return_authorization') else 'awaiting_review'
        tx.execute('UPDATE service_work_orders SET status=%s,provider_reference=%s,source=%s,updated_at=%s WHERE work_order_id=%s',
                   (status, response['reference'], source, now, job['target_id']))
        tx.execute('UPDATE after_sale_requests SET status=%s,stage=%s,completed_at=%s,updated_at=%s WHERE after_sale_id=%s',
                   ('completed' if status == 'completed' else 'rejected' if status == 'rejected' else 'processing', stage, now if status == 'completed' else None, now, aid))
        self.sync_order_after_sale(tx, order)
        eid = digest([job['request_id'], response])
        tx.execute('INSERT IGNORE INTO after_sale_events(event_id,after_sale_id,operation_id,kind,source,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                   (eid, aid, eid, status, source, dumps(dict(reference=response['reference'], message=response.get('message'), is_demo=demo)), now))
        return status

    def apply_demo_return(self, tx, job, aid, response, source):
        auth = response.get('return_authorization'); now = utcnow()
        if auth:
            ident = digest([job['request_id'], 'return'])
            tx.execute('''INSERT INTO return_authorizations(authorization_id,after_sale_id,address_ciphertext,key_version,carriers,requirements,deadline,status,source,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s) ON DUPLICATE KEY UPDATE authorization_id=authorization_id''',
                (ident, aid, self.cipher.seal(dumps(auth['address']), 'return-address:' + ident), self.cipher.version,
                 dumps(auth['carriers']), dumps(auth['requirements']), at(auth['deadline']), source, now))
        for event in response.get('return_events', []):
            ship = tx.one('''SELECT s.* FROM return_shipments s JOIN return_authorizations a ON a.authorization_id=s.authorization_id
                WHERE s.return_shipment_id=%s AND a.after_sale_id=%s''', (event['return_shipment_id'], aid))
            if not ship or not at(ship['created_at']) <= at(event['event_time']) <= now:
                raise BusinessError('insufficient_data', '逆向物流包裹归属或事件时间不一致')
            latest = tx.one('SELECT event_time FROM return_shipment_events WHERE return_shipment_id=%s ORDER BY event_time DESC LIMIT 1', (ship['return_shipment_id'],))
            if latest and at(latest['event_time']) > at(event['event_time']): continue
            ranks={s:i for i,s in enumerate(('registered','collected','in_transit','delivered','received','inspecting','accepted','rejected'))}
            if ranks.get(event['status'],-1)<ranks.get(ship['status'],-1): continue
            tx.execute('INSERT IGNORE INTO return_shipment_events(event_id,return_shipment_id,status,description,source,event_time) VALUES (%s,%s,%s,%s,%s,%s)',
                       (digest([job['request_id'], event]), ship['return_shipment_id'], event['status'], event['description'], source, at(event['event_time'])))
            tx.execute('UPDATE return_shipments SET status=%s,updated_at=%s WHERE return_shipment_id=%s', (event['status'], now, ship['return_shipment_id']))
