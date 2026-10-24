"""Lease-based provider processing and expiration of locally created unpaid orders."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from copy import deepcopy
from app.services.commerce.common import BusinessError,at,decoded,digest,dumps,money,ok,ref,utcnow
from app.services.commerce.providers import gateway,ProviderResponse


class FulfillmentService:
    def release_service_inventory(self,tx,wid,consumed=False,quantity=None):
        reservation=tx.one('SELECT * FROM service_inventory_reservations WHERE work_order_id=%s FOR UPDATE',(wid,))
        if not reservation or reservation['status']!='reserved': return
        qty=reservation['quantity'] if quantity is None else quantity; sid=reservation['sku_id']
        if qty<=0 or qty>reservation['quantity']: raise BusinessError('insufficient_data','换货库存消耗数量不一致')
        tx.one('SELECT sku_id FROM sku_inventory WHERE sku_id=%s FOR UPDATE',(sid,))
        restored=0 if consumed else qty
        if tx.execute('UPDATE sku_inventory SET available_qty=available_qty+%s,reserved_qty=reserved_qty-%s,version=version+1,updated_at=%s WHERE sku_id=%s AND reserved_qty>=%s',(restored,qty,utcnow(),sid,qty))!=1:
            raise BusinessError('insufficient_data','换货库存预留不一致')
        if qty<reservation['quantity']:
            tx.execute('UPDATE service_inventory_reservations SET quantity=quantity-%s,updated_at=%s WHERE work_order_id=%s',(qty,utcnow(),wid))
        else:
            tx.execute('UPDATE service_inventory_reservations SET status=%s,updated_at=%s WHERE work_order_id=%s',('consumed' if consumed else 'released',utcnow(),wid))

    def request_dispatch(self,args,request_id):
        self.provider_required('dispatch')
        with self.store.transaction(write=True) as tx:
            order=self.owned_order(tx,args['order_ref'],lock=True)
            prior=self.receipt(tx,request_id)
            if prior:
                if prior['action']!='request_dispatch' or prior['arguments_digest']!=digest(args): raise BusinessError('invalid_arguments','催单请求编号冲突')
            else:
                if order['status'] not in {'paid','pending_receipt'}: raise BusinessError('not_eligible','订单状态不支持催发货')
                # Pending receipt also includes already dispatched orders. Check
                # parcel evidence before asking the merchant to dispatch again.
                if any(s['status'] in {'shipped','in_transit','delivered'} or s['shipped_at'] for s in self.order_rows(tx,'shipments',order)):
                    raise BusinessError('not_eligible','已有发货记录，请查询物流或逐商品核实剩余待发数量')
                self.queue_provider(tx,request_id,'dispatch',order['order_id'],order,{'platform_code':order['platform_code'],'order_id':order['order_id'],'reason':args.get('reason')})
                self.record_receipt(tx,request_id,'request_dispatch',args,{'status':'queued','request_id':request_id})
        self.process_provider_request(request_id)
        with self.store.transaction() as tx:
            row=tx.one('SELECT status,provider_reference,result FROM commerce_provider_requests WHERE request_id=%s AND user_id=%s',(request_id,self.user))
        if row['status'] not in {'accepted','processing','completed','rejected'}:
            raise BusinessError('result_unknown','催单已登记，尚未取得处理方受理回执',request_id=request_id)
        return ok({'request_id':request_id,'status':row['status'],'provider_reference':row['provider_reference'],'receipt':decoded(row['result'])},limitations=['受理催单不等于已经发货'])

    def process_provider_request(self,request_id):
        # Lock the parent order before the job everywhere, including cancellation.
        with self.store.transaction(write=True) as tx:
            row=tx.one('SELECT * FROM commerce_provider_requests WHERE request_id=%s AND user_id=%s',(request_id,self.user))
            if not row: raise BusinessError('not_found','业务请求不存在')
            self.owned_order(tx,ref('order',row['platform_code'],row['order_id']),lock=True)
            row=tx.one('SELECT * FROM commerce_provider_requests WHERE request_id=%s FOR UPDATE',(request_id,))
            if row['status'] in {'completed','rejected','cancelled'}: return
            if row['lease_until'] and at(row['lease_until'])>utcnow(): return
            provider=self.provider_required(row['kind']); lease=uuid4().hex
            tx.execute('UPDATE commerce_provider_requests SET lease_token=%s,lease_until=%s,attempts=attempts+1,updated_at=%s WHERE request_id=%s',(lease,utcnow()+timedelta(seconds=30),utcnow(),request_id))
            payload=decoded(self.cipher.open(row['payload_ciphertext'],'provider:'+request_id,row['key_version']))
            if payload.get('after_sale_id'):
                payload['return_shipments']=tx.all('''SELECT s.return_shipment_id,s.carrier_code,s.tracking_number,s.status FROM return_shipments s
                    JOIN return_authorizations a ON a.authorization_id=s.authorization_id WHERE a.after_sale_id=%s''',(payload['after_sale_id'],))
                payload['return_shipment']=payload['return_shipments'][0] if len(payload['return_shipments'])==1 else None
        try:
            response=provider.call('query' if row['attempts'] else 'submit',request_id,row['kind'],payload)
            response=ProviderResponse.model_validate(response).model_dump(mode='json')
            if response['status']=='not_found':
                response=ProviderResponse.model_validate(provider.call('submit',request_id,row['kind'],payload)).model_dump(mode='json')
            if response['status']=='not_found' or not response['reference']: raise ValueError('missing provider receipt')
        except Exception:
            with self.store.transaction(write=True) as tx:
                tx.execute("UPDATE commerce_provider_requests SET status='unknown',lease_until=NULL,updated_at=%s WHERE request_id=%s AND lease_token=%s",(utcnow(),request_id,lease))
            return
        with self.store.transaction(write=True) as tx:
            order=self.owned_order(tx,ref('order',row['platform_code'],row['order_id']),lock=True)
            locked=tx.one('SELECT * FROM commerce_provider_requests WHERE request_id=%s FOR UPDATE',(request_id,))
            if locked['lease_token']!=lease or locked['status'] in {'completed','rejected','cancelled'}: return
            stored_response=deepcopy(response)
            if stored_response.get('return_authorization'):
                # The warehouse contact/address is stored only in its encrypted
                # authorization; retain a digest for repeated-receipt detection.
                address=stored_response['return_authorization']['address']
                stored_response['return_authorization']['address']={'digest':digest(address)}
            if decoded(locked['result'])!=stored_response:
                applied_status=self.apply_provider_response(tx,locked,order,response)
                if applied_status:
                    response['status']=stored_response['status']=applied_status
            tx.execute('UPDATE commerce_provider_requests SET status=%s,provider_reference=%s,result=%s,lease_until=NULL,updated_at=%s WHERE request_id=%s',
                (response['status'],response['reference'],dumps(stored_response),utcnow(),request_id))

    def apply_provider_response(self,tx,job,order,response):
        now=utcnow(); kind=job['kind']; status=response['status']; target=job['target_id']
        if kind=='dispatch': return
        if kind=='invoice_reissue':
            correction=tx.one('SELECT * FROM invoice_corrections WHERE correction_id=%s FOR UPDATE',(target,))
            if not correction: raise BusinessError('insufficient_data','发票更正关联缺失')
            if status=='completed':
                invoice=response.get('invoice')
                if not invoice or not response['original_voided']: raise BusinessError('insufficient_data','缺少原票红冲或新票凭据')
                required={'invoice_number','document_key','mime_type','sha256','issued_at'}
                if not required<=invoice.keys(): raise BusinessError('insufficient_data','正式票据字段不完整')
                tx.execute("UPDATE issued_invoices SET status='voided',updated_at=%s WHERE issued_invoice_id=%s",(now,correction['issued_invoice_id']))
                tx.execute('INSERT INTO issued_invoices(issued_invoice_id,invoice_id,invoice_number,status,document_key,mime_type,sha256,provider,issued_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (uuid4().hex,correction['invoice_id'],invoice['invoice_number'],'issued',invoice['document_key'],invoice['mime_type'],invoice['sha256'],kind,at(invoice['issued_at']),now))
            tx.execute('UPDATE invoice_corrections SET status=%s,updated_at=%s WHERE correction_id=%s',(status,now,target))
            tx.execute('UPDATE invoice_requests SET status=%s,issued_at=%s WHERE invoice_id=%s',('issued' if status=='completed' else 'rejected' if status=='rejected' else 'processing',now if status=='completed' else None,correction['invoice_id']))
            return
        work=tx.one('SELECT * FROM service_work_orders WHERE work_order_id=%s FOR UPDATE',(target,))
        if not work: raise BusinessError('insufficient_data','服务工单关联缺失')
        aid=work['after_sale_id']; sale,_=self.sale(tx,aid); details=decoded(work['details'])
        auth=response.get('return_authorization')
        if auth:
            if not {'address','carriers','requirements','deadline'}<=auth.keys() or not isinstance(auth['carriers'],list): raise BusinessError('insufficient_data','退货授权字段不完整')
            existing=tx.one('SELECT authorization_id FROM return_authorizations WHERE after_sale_id=%s',(aid,)); ident=existing['authorization_id'] if existing else uuid4().hex
            sealed=self.cipher.seal(dumps(auth['address']),'return-address:'+ident)
            tx.execute('''INSERT INTO return_authorizations(authorization_id,after_sale_id,address_ciphertext,key_version,carriers,requirements,deadline,status,source,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE address_ciphertext=VALUES(address_ciphertext),key_version=VALUES(key_version),carriers=VALUES(carriers),requirements=VALUES(requirements),deadline=VALUES(deadline),updated_at=VALUES(updated_at)''',
                (ident,aid,sealed,self.cipher.version,dumps(auth['carriers']),dumps(auth['requirements']),at(auth['deadline']),'active',response['reference'],now))
        if response.get('return_events'):
            returned=tx.all('SELECT s.return_shipment_id FROM return_shipments s JOIN return_authorizations a ON a.authorization_id=s.authorization_id WHERE a.after_sale_id=%s',(aid,))
            for event in response['return_events']:
                rid=event.get('return_shipment_id') or (returned[0]['return_shipment_id'] if len(returned)==1 else None)
                if rid not in {r['return_shipment_id'] for r in returned}: raise BusinessError('insufficient_data','逆向物流事件未明确对应寄回包裹')
                if not {'status','event_time','description'}<=event.keys(): raise BusinessError('insufficient_data','逆向物流事件缺少字段')
                eid=digest([job['request_id'],rid,event])
                tx.execute('INSERT IGNORE INTO return_shipment_events(event_id,return_shipment_id,status,description,source,event_time) VALUES (%s,%s,%s,%s,%s,%s)',(eid,rid,event['status'],event['description'],response['reference'],at(event['event_time'])))
                latest=tx.one('SELECT status FROM return_shipment_events WHERE return_shipment_id=%s ORDER BY event_time DESC,event_id DESC LIMIT 1',(rid,))
                tx.execute('UPDATE return_shipments SET status=%s,updated_at=%s WHERE return_shipment_id=%s',(latest['status'],now,rid))
        if status=='completed' and kind=='price_protection':
            refund=response.get('refund')
            if not refund or not {'payment_id','provider_refund_id','amount','currency','completed_at'}<=refund.keys(): raise BusinessError('insufficient_data','价保完成缺少真实退款凭据')
            if money(refund['amount'])!=money(sale['requested_amount']) or refund['currency']!=order['currency']: raise BusinessError('insufficient_data','退款金额或币种与申请不一致')
            payments={p['payment_id']:p for p in self.order_rows(tx,'payment_transactions',order)}
            payment=payments.get(refund['payment_id'])
            if not payment or payment['status']!='succeeded': raise BusinessError('insufficient_data','原支付凭据无法核实')
            existing=tx.all('SELECT amount,status,currency FROM refund_transactions WHERE payment_id=%s FOR UPDATE',(payment['payment_id'],))
            if payment['currency']!=order['currency'] or any(r['currency']!=order['currency'] or r['status'] not in {'pending','processing','succeeded','failed','cancelled'} for r in existing):
                raise BusinessError('insufficient_data','原支付退款口径未核实')
            held=sum((money(r['amount']) for r in existing if r['status'] in {'pending','processing','succeeded'}),Decimal(0))
            if held+money(refund['amount'])>money(payment['amount']): raise BusinessError('insufficient_data','退款超过原支付未退金额')
            tx.execute('''INSERT INTO refund_transactions(refund_id,after_sale_id,payment_id,provider,provider_refund_id,amount,currency,status,requested_at,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(uuid4().hex,aid,payment['payment_id'],kind,refund['provider_refund_id'],refund['amount'],refund['currency'],'succeeded',now,at(refund['completed_at'])))
        if kind=='exchange' and (response.get('replacement_shipment') or status=='completed'):
            shipment=response.get('replacement_shipment')
            if shipment:
                if shipment['sku_id']!=details['replacement_sku_id'] or shipment['quantity']>details['quantity']: raise BusinessError('insufficient_data','补发规格数量不一致')
                sent=at(shipment['shipped_at']); delivered=at(shipment['delivered_at']) if shipment.get('delivered_at') else None
                confirmed=at(shipment['receipt_confirmed_at']) if shipment.get('receipt_confirmed_at') else None
                if sent>now or delivered and not sent<=delivered<=now or confirmed and (not delivered or not delivered<=confirmed<=now):
                    raise BusinessError('insufficient_data','换货签收与确认时间不一致')
                prior=tx.one("SELECT * FROM shipments WHERE after_sale_id=%s AND carrier=%s AND tracking_number=%s",(aid,shipment['carrier'],shipment['tracking_number']))
                if prior:
                    sid=prior['shipment_id']
                    line=tx.one('SELECT * FROM shipment_items WHERE shipment_id=%s AND order_item_id=%s',(sid,details['order_item_id']))
                    if not line or line['actual_sku_id']!=shipment['sku_id'] or line['quantity']!=shipment['quantity']: raise BusinessError('insufficient_data','重复补发回执明细变化')
                    tx.execute('UPDATE shipments SET status=%s,delivered_at=COALESCE(%s,delivered_at),receipt_confirmed_at=COALESCE(%s,receipt_confirmed_at) WHERE shipment_id=%s',('delivered' if delivered or prior['delivered_at'] else 'shipped',delivered,confirmed,sid))
                else:
                    prior_qty=tx.one("SELECT COALESCE(SUM(i.quantity),0) qty FROM shipment_items i JOIN shipments s ON s.shipment_id=i.shipment_id WHERE s.after_sale_id=%s AND s.shipment_kind='exchange'",(aid,))['qty']
                    if prior_qty+shipment['quantity']>details['quantity']: raise BusinessError('insufficient_data','补发总量超过换货数量')
                    sid=uuid4().hex; sku=self.sku(tx,shipment['sku_id'])
                    tx.execute('INSERT INTO shipments(shipment_id,platform_code,order_id,tracking_number,carrier,status,shipped_at,delivered_at,created_at,shipment_kind,after_sale_id,receipt_confirmed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(sid,order['platform_code'],order['order_id'],shipment['tracking_number'],shipment['carrier'],'delivered' if delivered else 'shipped',sent,delivered,now,'exchange',aid,confirmed))
                    tx.execute('INSERT INTO shipment_items(shipment_id,order_item_id,quantity,actual_sku_id,actual_spec_snapshot) VALUES (%s,%s,%s,%s,%s)',(sid,details['order_item_id'],shipment['quantity'],shipment['sku_id'],dumps(sku['spec_values'])))
                    self.release_service_inventory(tx,target,consumed=True,quantity=shipment['quantity'])
                details['replacement_shipment_id']=sid
            confirmed_qty=tx.one("SELECT COALESCE(SUM(i.quantity),0) qty FROM shipment_items i JOIN shipments s ON s.shipment_id=i.shipment_id WHERE s.after_sale_id=%s AND s.shipment_kind='exchange' AND s.receipt_confirmed_at IS NOT NULL",(aid,))['qty']
            if status=='completed' and confirmed_qty<details['quantity']: status='processing'
        if status=='rejected': self.release_service_inventory(tx,target)
        tx.execute('UPDATE service_work_orders SET status=%s,details=%s,provider_reference=%s,source=%s,updated_at=%s WHERE work_order_id=%s',(status,dumps(details),response['reference'],'provider',now,target))
        returned=tx.all('SELECT s.status FROM return_shipments s JOIN return_authorizations a ON a.authorization_id=s.authorization_id WHERE a.after_sale_id=%s',(aid,))
        return_stage=('awaiting_replacement' if kind=='exchange' else 'inspecting') if returned and all(r['status']=='accepted' for r in returned) else 'inspecting' if returned and all(r['status'] in {'accepted','received','delivered','inspecting'} for r in returned) else 'return_in_transit' if returned else None
        if returned:
            expected=tx.one('SELECT COALESCE(SUM(quantity),0) qty FROM after_sale_items WHERE after_sale_id=%s',(aid,))['qty']
            registered=tx.one('SELECT COALESCE(SUM(quantity),0) qty FROM return_shipment_items WHERE after_sale_id=%s',(aid,))['qty']
            if 0<registered<expected: return_stage='awaiting_return'
            elif all(r['status']=='registered' for r in returned): return_stage='awaiting_collection'
        stage='completed' if status=='completed' else 'rejected' if status=='rejected' else 'replacement_in_transit' if kind=='exchange' and details.get('replacement_shipment_id') else return_stage or ('awaiting_return' if auth else sale.get('stage'))
        tx.execute('UPDATE after_sale_requests SET status=%s,stage=%s,completed_at=%s,updated_at=%s WHERE after_sale_id=%s',('completed' if status=='completed' else 'rejected' if status=='rejected' else 'approved' if auth else 'processing',stage,now if status=='completed' else None,now,aid))
        self.sync_order_after_sale(tx,order)
        eid=digest([job['request_id'],response])
        tx.execute('INSERT IGNORE INTO after_sale_events(event_id,after_sale_id,operation_id,kind,source,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(eid,aid,eid,status,'provider',dumps({'reference':response['reference'],'message':response.get('message')}),now))
        return status

    def expire_order(self,platform,order_id):
        with self.store.transaction(write=True) as tx:
            order=self.owned_order(tx,ref('order',platform,order_id),lock=True)
            expiry=tx.one('SELECT * FROM order_expirations WHERE platform_code=%s AND order_id=%s FOR UPDATE',(platform,order_id))
            if not expiry or expiry['status']!='active' or at(expiry['expires_at'])>utcnow(): return False
            if order['status']!='pending_payment':
                tx.execute("UPDATE order_expirations SET status='inactive' WHERE platform_code=%s AND order_id=%s",(platform,order_id)); return False
            if money(order['paid_amount']) or any(p['status'] not in {'failed','closed'} for p in self.order_rows(tx,'payment_transactions',order)) or any(s['status']!='cancelled' for s in self.order_rows(tx,'shipments',order)): return False
            self.release_reservations(tx,order)
            for redemption in sorted(self.order_rows(tx,'coupon_redemptions',order),key=lambda r:r['user_coupon_id']):
                if redemption['status']=='released': continue
                if redemption['status']!='reserved': raise BusinessError('insufficient_data','优惠券核销状态无法释放')
                coupon=tx.one('SELECT * FROM user_coupons WHERE user_coupon_id=%s AND user_id=%s FOR UPDATE',(redemption['user_coupon_id'],self.user))
                if not coupon or coupon['status']!='reserved': raise BusinessError('insufficient_data','优惠券占用不一致')
                tx.execute('UPDATE user_coupons SET status=%s,version=version+1 WHERE user_coupon_id=%s',('expired' if at(coupon['expires_at'])<=utcnow() else 'available',coupon['user_coupon_id']))
                tx.execute("UPDATE coupon_redemptions SET status='released',released_at=%s WHERE redemption_id=%s",(utcnow(),redemption['redemption_id']))
            tx.execute("UPDATE orders SET status='closed',updated_at=%s WHERE platform_code=%s AND order_id=%s",(utcnow(),platform,order_id))
            tx.execute("UPDATE order_expirations SET status='expired' WHERE platform_code=%s AND order_id=%s",(platform,order_id))
            tx.execute('INSERT INTO order_events(event_id,platform_code,order_id,operation_id,kind,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(uuid4().hex,platform,order_id,'expire:'+platform+':'+order_id,'payment_expired','{}',utcnow()))
            return True
