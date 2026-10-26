"""Order edits and reviewed cancellation, bound to confirmed order evidence."""
from decimal import Decimal
from uuid import uuid4

from app.services.commerce.common import BusinessError, at, decoded, digest, dumps, money, utcnow
from app.services.commerce.writes import json_binary


class OrderChangeService:
    def original_shipments(self, tx, order):
        return [s for s in self.order_rows(tx, 'shipments', order)
                if s.get('shipment_kind', 'original') == 'original' and s['status'] != 'cancelled']

    def has_dispatched(self, ships):
        return any(s.get('shipped_at') or s['status'] in
                   {'shipped', 'in_transit', 'delivered', 'refused', 'returned'} for s in ships)

    def check_order_editable(self, tx, order, *, address=False):
        ships = self.original_shipments(tx, order)
        if self.has_dispatched(ships) or order['status'] not in {'pending_payment', 'paid', 'pending_receipt'}:
            raise BusinessError('not_eligible', '已发货、已签收或售后中的订单不能修改')
        if address and order['status'] != 'pending_payment':
            raise BusinessError('not_eligible', '仅待付款订单可修改收货地址')
        if order['status'] == 'pending_payment':
            if money(order['paid_amount']) or any(p['status'] not in {'failed', 'closed'}
                    for p in self.order_rows(tx, 'payment_transactions', order)):
                raise BusinessError('not_eligible', '存在支付中或成功交易，请先核实支付结果')

    def cancellation_facts(self, tx, order):
        items = self.order_rows(tx, 'order_items', order)
        if not items:
            raise BusinessError('insufficient_data', '订单商品缺失')
        constraints = []
        for item in items:
            product = tx.one('SELECT category,specs FROM products WHERE product_id=%s', (item['product_id'],))
            sku = tx.one('SELECT spec_values FROM product_skus WHERE sku_id=%s', (item['sku_id'],))
            if not product or not sku:
                raise BusinessError('insufficient_data', '商品取消条件无法核实')
            snapshot = decoded(item['spec_snapshot']) or {}
            spec = decoded(sku['spec_values']) or {}
            product_spec = decoded(product['specs']) or {}
            allowed = snapshot.get('cancel_allowed', spec.get('cancel_allowed', product_spec.get('cancel_allowed')))
            if allowed is not None and not isinstance(allowed, bool):
                raise BusinessError('insufficient_data', '商品取消条件格式无法核实')
            if allowed is False or (allowed is None and product['category'] in {'定制', '定制商品', '生鲜', '生鲜食品'}):
                raise BusinessError('not_eligible', '订单含下单后不可取消的商品，请按商品说明处理', order_item_id=item['order_item_id'])
            constraints.append([item['order_item_id'], allowed, product['category']])
        ships = self.original_shipments(tx, order)
        if order['status'] == 'received' or any(s['status'] == 'delivered' or s.get('delivered_at') or s.get('receipt_confirmed_at') for s in ships):
            raise BusinessError('not_eligible', '已签收订单不可直接取消，请申请售后退货')
        if order['status'] not in {'pending_payment', 'paid', 'pending_receipt'}:
            raise BusinessError('not_eligible', '订单状态不支持取消或已有售后处理中')
        sales = self.order_rows(tx, 'after_sale_requests', order)
        if any(s['status'] not in {'cancelled', 'rejected', 'closed'} for s in sales):
            raise BusinessError('not_eligible', '订单已有售后或退款，请先核实原申请')
        payments = self.order_rows(tx, 'payment_transactions', order)
        dispatched = self.has_dispatched(ships)
        if order['status'] == 'pending_payment':
            if money(order['paid_amount']) or any(p['status'] not in {'failed', 'closed'} for p in payments) or ships:
                raise BusinessError('not_eligible', '存在支付或履约处理，不能按未付款取消')
            return dict(mode='unpaid', constraints=constraints)
        if any(p['status'] not in {'succeeded', 'failed', 'closed'} for p in payments):
            raise BusinessError('insufficient_data', '存在结果未明的支付交易')
        successful = sorted((p for p in payments if p['status'] == 'succeeded'), key=lambda p: p['payment_id'])
        paid = money(order['paid_amount'])
        if (paid <= 0 or paid != money(order['payable_amount']) or not successful
                or any(p['currency'] != order['currency'] or not p['provider'] or not p['provider_txn_id'] for p in successful)
                or sum((money(p['amount']) for p in successful), Decimal(0)) != paid):
            raise BusinessError('insufficient_data', '全额原路退款所需的原支付凭据或金额不完整')
        for p in successful:
            refunds = tx.all('SELECT * FROM refund_transactions WHERE payment_id=%s', (p['payment_id'],))
            if any(r['status'] not in {'failed', 'cancelled'} for r in refunds):
                raise BusinessError('not_eligible', '原支付已有退款占用，不能重复全单取消')
        shipping = money(order['shipping_amount']) if dispatched else Decimal(0)
        amount = paid - shipping
        if shipping < 0 or amount <= 0:
            raise BusinessError('insufficient_data', '扣除运费后的退款金额无法核实')
        allocations = []
        remaining = amount
        for p in successful:
            value = min(remaining, money(p['amount']))
            if value > 0:
                allocations.append(dict(payment_id=p['payment_id'], amount=value, provider=p['provider'],
                                        provider_txn_id=p['provider_txn_id']))
                remaining -= value
        return dict(mode='refused_return' if dispatched else 'paid_unshipped', amount=amount,
                    shipping_deduction=shipping, currency=order['currency'], payments=allocations,
                    shipment_ids=[s['shipment_id'] for s in ships if self.has_dispatched([s])], constraints=constraints)

    def order_update_plan(self, tx, order, args):
        policy, rule = self.policy(tx, 'change_address', order)
        if not rule.local_authority:
            raise BusinessError('insufficient_data', '未接通订单修改控制')
        self.check_order_editable(tx, order)
        changes = {k: args[k] for k in ('phone', 'recipient_name', 'note', 'invoice') if args.get(k) is not None}
        if not changes:
            raise BusinessError('invalid_arguments', '至少指定一项要修改的信息')
        if order['status'] != 'pending_payment' and changes.keys() - {'phone', 'note'}:
            raise BusinessError('not_eligible', '已支付待发货仅可修改电话和备注')
        old = tx.one('SELECT * FROM order_edit_details WHERE platform_code=%s AND order_id=%s',
                     (order['platform_code'], order['order_id']))
        address = tx.one('SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s',
                         (order['platform_code'], order['order_id']))
        if changes.keys() & {'phone', 'recipient_name'} and not address:
            raise BusinessError('insufficient_data', '订单收货信息缺失')
        return policy, dict(title='修改订单信息', changed_fields=list(changes), impact='仅更新本订单；发票信息保存不等于开票'), dict(old=json_binary(old) if old else None, address=json_binary(address) if address else None)

    def save_order_update(self, tx, order, args, oid):
        now = utcnow()
        key = (order['platform_code'], order['order_id'])
        old = tx.one('SELECT * FROM order_edit_details WHERE platform_code=%s AND order_id=%s FOR UPDATE', key)
        purpose = 'order-edit:' + self.user + ':' + order['platform_code'] + ':' + order['order_id']
        details = decoded(self.cipher.open(old['details_ciphertext'], purpose, old['key_version'])) if old else {}
        for field in ('note', 'invoice'):
            if args.get(field) is not None:
                details[field] = args[field]
        tx.execute('''INSERT INTO order_edit_details VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE details_ciphertext=VALUES(details_ciphertext),key_version=VALUES(key_version),updated_at=VALUES(updated_at)''',
                   (*key, self.cipher.seal(dumps(details), purpose), self.cipher.version, now))
        if any(args.get(k) is not None for k in ('phone', 'recipient_name')):
            address = tx.one('SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s', key)
            replacement = dict(address)
            # Re-encrypt all fields when rotating the row's key version.
            for field in ('phone', 'recipient_name', 'detail'):
                value = args.get(field)
                if value is None:
                    value = self.cipher.open(address[field+'_ciphertext'], 'address:'+self.user+':'+field, address['key_version'])
                replacement[field+'_ciphertext'] = self.cipher.seal(value, 'address:'+self.user+':'+field)
            replacement['key_version'] = self.cipher.version
            tx.execute('UPDATE order_address_snapshots SET phone_ciphertext=%s,recipient_name_ciphertext=%s,detail_ciphertext=%s,key_version=%s,captured_at=%s WHERE platform_code=%s AND order_id=%s',
                       (replacement['phone_ciphertext'], replacement['recipient_name_ciphertext'], replacement['detail_ciphertext'], self.cipher.version, now, *key))
            tx.execute('INSERT INTO order_address_changes VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                       (uuid4().hex, *key, oid, self.cipher.seal(dumps(json_binary(address)), 'address-history:'+self.user),
                        self.cipher.seal(dumps(json_binary(replacement)), 'address-history:'+self.user), self.cipher.version, now))
        # The encrypted operation receipt binds the exact change; events expose only field names.
        tx.execute('UPDATE orders SET updated_at=%s WHERE platform_code=%s AND order_id=%s', (now, *key))

    def submit_paid_cancellation(self, tx, order, args, oid, facts):
        aid, work_id, now = uuid4().hex, uuid4().hex, utcnow()
        tx.execute('''INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,requested_amount,stage,created_at,updated_at)
            VALUES (%s,%s,%s,%s,'order_cancel','pending_review',%s,%s,'awaiting_review',%s,%s)''',
                   (aid, order['platform_code'], order['order_id'], self.user, args['reason'], facts['amount'], now, now))
        # Bind all items so concurrent partial refunds and exchanges cannot reuse them.
        for item in self.order_rows(tx, 'order_items', order):
            tx.execute('INSERT INTO after_sale_items VALUES (%s,%s,%s,%s)',
                       (aid, item['order_item_id'], item['quantity'], item['payable_amount']))
        tx.execute('INSERT INTO service_work_orders VALUES (%s,%s,%s,%s,%s,NULL,%s,%s)',
                   (work_id, aid, 'order_cancel', 'pending_review', dumps(facts), 'local', now))
        for payment in facts['payments']:
            tx.execute('''INSERT INTO refund_transactions(refund_id,after_sale_id,payment_id,provider,amount,currency,status,requested_at)
                VALUES (%s,%s,%s,%s,%s,%s,'pending',%s)''',
                       (uuid4().hex, aid, payment['payment_id'], payment['provider'], payment['amount'], facts['currency'], now))
        self.queue_provider(tx, oid, 'order_cancel', work_id, order,
                            dict(facts, after_sale_id=aid, reason=args['reason'], platform_code=order['platform_code'], order_id=order['order_id']))
        self.sale_event(tx, aid, oid, 'submitted', dict(mode=facts['mode']))
        self.sync_order_after_sale(tx, order)
        return dict(after_sale_id=aid, status='submitted', refund_amount=facts['amount'], shipping_deduction=facts['shipping_deduction'])

    def apply_cancellation_response(self, tx, job, order, response):
        work = tx.one('SELECT * FROM service_work_orders WHERE work_order_id=%s FOR UPDATE', (job['target_id'],))
        if not work or work['kind'] != 'order_cancel':
            raise BusinessError('insufficient_data', '取消工单缺失')
        facts, aid, now = decoded(work['details']), work['after_sale_id'], utcnow()
        status = response['status']
        if status == 'completed':
            evidence = response.get('cancellation')
            if not evidence or not evidence['approved'] or not evidence['fulfillment_stopped']:
                raise BusinessError('insufficient_data', '取消完成缺少审核及履约停止回执')
            ships = self.original_shipments(tx, order)
            if any(s.get('delivered_at') or s['status']=='delivered' for s in ships):
                raise BusinessError('state_changed', '订单已有签收，需人工核实取消回执')
            current_shipped = {s['shipment_id'] for s in ships if self.has_dispatched([s])}
            if current_shipped != set(facts['shipment_ids']):
                raise BusinessError('state_changed', '取消期间发货事实变化，需重新核实退款与运费')
            if facts['mode'] == 'refused_return':
                returned = evidence['returns']
                if len(returned)!=len(current_shipped) or {s['shipment_id'] for s in returned} != current_shipped:
                    raise BusinessError('insufficient_data', '缺少所有包裹的拒收退回验货回执')
                for record in returned:
                    ship = next(s for s in ships if s['shipment_id']==record['shipment_id'])
                    if not ship['shipped_at'] or not at(ship['shipped_at']) <= at(record['refused_at']) <= at(record['returned_at']) <= at(record['inspected_at']) <= now:
                        raise BusinessError('insufficient_data', '拒收、退回或验货时间不一致')
            planned = tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s FOR UPDATE', (aid,))
            receipts = response.get('refunds', [])
            if len(receipts)!=len(planned) or len({r['payment_id'] for r in receipts})!=len(receipts):
                raise BusinessError('insufficient_data', '原路退款回执不完整或重复')
            for pending in planned:
                refund = next((r for r in receipts if r['payment_id']==pending['payment_id']), None)
                payment = tx.one('SELECT * FROM payment_transactions WHERE payment_id=%s FOR UPDATE', (pending['payment_id'],))
                if (not refund or not payment or payment['status']!='succeeded'
                        or payment['platform_code']!=order['platform_code'] or payment['order_id']!=order['order_id']
                        or payment['currency']!=facts['currency'] or payment['provider']!=pending['provider']
                        or money(refund['amount'])!=money(pending['amount']) or refund['currency']!=facts['currency']
                        or not at(pending['requested_at']) <= at(refund['completed_at']) <= now):
                    raise BusinessError('insufficient_data', '原路退款金额、币种、时间或支付归属不一致')
                held = tx.one("SELECT COALESCE(SUM(amount),0) amount FROM refund_transactions WHERE payment_id=%s AND status IN ('pending','processing','succeeded')", (payment['payment_id'],))
                if money(held['amount'])>money(payment['amount']):
                    raise BusinessError('insufficient_data', '退款超过原支付金额')
                tx.execute("UPDATE refund_transactions SET status='succeeded',provider_refund_id=%s,completed_at=%s WHERE refund_id=%s",
                           (refund['provider_refund_id'], at(refund['completed_at']), pending['refund_id']))
            # Only release reservations we still own. Consumed stock is not manufactured back into inventory.
            reservations = self.order_rows(tx, 'inventory_reservations', order)
            if reservations and all(r['status']=='reserved' for r in reservations):
                self.release_reservations(tx, order)
            elif any(r['status']=='reserved' for r in reservations):
                raise BusinessError('insufficient_data', '部分库存仍占用，需核实履约库存')
            tx.execute("UPDATE orders SET status='cancelled',status_before_after_sale=NULL,updated_at=%s WHERE platform_code=%s AND order_id=%s", (now, order['platform_code'], order['order_id']))
        elif status == 'rejected':
            tx.execute("UPDATE refund_transactions SET status='cancelled' WHERE after_sale_id=%s AND status='pending'", (aid,))
        stage = 'completed' if status=='completed' else 'rejected' if status=='rejected' else 'awaiting_return' if facts['mode']=='refused_return' and status=='accepted' else 'awaiting_review'
        tx.execute('UPDATE service_work_orders SET status=%s,provider_reference=%s,source=%s,updated_at=%s WHERE work_order_id=%s',
                   (status, response['reference'], 'provider', now, work['work_order_id']))
        tx.execute('UPDATE after_sale_requests SET status=%s,stage=%s,completed_at=%s,updated_at=%s WHERE after_sale_id=%s',
                   ('completed' if status=='completed' else 'rejected' if status=='rejected' else 'processing', stage, now if status=='completed' else None, now, aid))
        if status!='completed':
            self.sync_order_after_sale(tx, order)
        self.sale_event(tx, aid, digest([job['request_id'], response]), status, dict(reference=response['reference']))
        return status
