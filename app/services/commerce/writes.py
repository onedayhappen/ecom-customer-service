"""Prepared mutations and transactional, request-bound business receipts."""
import base64
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from app.services.commerce.common import BusinessError,at,decoded,digest,dumps,money,ok,ref,unref,utcnow

PREPARES={'prepare_after_sale','prepare_invoice','prepare_order_address_change','prepare_order_update','prepare_order_cancel','prepare_after_sale_cancel',
    'prepare_price_protection','prepare_exchange','prepare_return_shipment','prepare_repair','prepare_invoice_reissue','prepare_purchase',
    'claim_coupon','request_dispatch'}

def json_binary(row):
    return {k:base64.b64encode(bytes(v)).decode() if isinstance(v,(bytes,bytearray,memoryview)) else v for k,v in row.items()}

class WriteService:
    def private_review(self,tx,action,args):
        if action=='prepare_order_update':
            return {'changes':{k:v for k,v in args.items() if k!='order_ref' and v is not None}}
        if action=='prepare_invoice':
            return {'invoice_title':args['title'],'tax_number':args.get('tax_number')}
        if action=='prepare_order_address_change':
            row=tx.one('SELECT * FROM user_addresses WHERE address_id=%s AND user_id=%s AND deleted_at IS NULL',(args['address_id'],self.user))
            if not row: raise BusinessError('not_found','地址不存在')
            address={field:self.cipher.open(row[field+'_ciphertext'],'address:'+self.user+':'+field,row['key_version']) for field in ('recipient_name','phone','detail')}
            address['region']=[row[k] for k in ('province_code','city_code','district_code')]
            return {'address':address}
        return {}
    def confirmation_review(self,prepared):
        args=prepared['arguments']
        body=decoded(self.cipher.open(base64.b64decode(args['sealed']),'operation:'+self.user,args['key_version']))
        if body['user_id']!=self.user or body['action']!=prepared['action']:
            raise BusinessError('invalid_arguments','确认内容绑定不匹配')
        return {**prepared['review'],**body.get('private_review',{})}
    def receipt(self,tx,oid,*,lock=False):
        r=tx.one('SELECT * FROM business_operation_receipts WHERE operation_id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),(oid,self.user))
        return r
    def record_receipt(self,tx,oid,action,args,result):
        tx.execute('INSERT INTO business_operation_receipts(operation_id,user_id,action,arguments_digest,result,created_at) VALUES (%s,%s,%s,%s,%s,%s)',(oid,self.user,action,digest(args),dumps(result),utcnow()))
    def claim_plan(self,tx,args):
        t,instance,scopes=self.coupon(tx,args['coupon_ref'])
        if instance: raise BusinessError('invalid_arguments','领取需要券模板引用')
        self.claim_eligibility(tx,t)
        if not scopes or any(s['scope_type'] not in {'all','merchant','product','sku'} for s in scopes):
            raise BusinessError('insufficient_data','券适用范围不完整')
        self.discount(t,max(money(t['threshold_amount']),Decimal(1)))
        review=dict(title='领取优惠券',coupon_ref=args['coupon_ref'],name=t['name'],platform_code=t['platform_code'],
                    order_id='coupon:'+t['coupon_id'],impact='确认后领取此券并占用领取额度',
                    threshold_amount=t['threshold_amount'],discount_amount=t['discount_amount'],currency=t['currency'],
                    kind=t['kind'],discount_bps=t.get('discount_bps'),cap_amount=t.get('cap_amount'),
                    valid_days=t['valid_days'],fixed_expires_at=t['fixed_expires_at'])
        return review,digest({'template':t,'scopes':scopes})

    def dispatch_plan(self,tx,args):
        self.provider_required('dispatch')
        order=self.owned_order(tx,args['order_ref'])
        if order['status'] not in {'paid','pending_receipt'}: raise BusinessError('not_eligible','订单状态不支持催发货')
        ships=self.order_rows(tx,'shipments',order)
        if any(s['status'] in {'shipped','in_transit','delivered'} or s['shipped_at'] for s in ships):
            raise BusinessError('not_eligible','已有发货记录，请查询物流或逐商品核实剩余待发数量')
        return dict(title='提交催发货请求',order_id=order['order_id'],platform_code=order['platform_code'],
                    reason=args.get('reason'),impact='确认后登记催单，接收方受理及发货结果仍需核实'),digest({'order':order,'shipments':ships,'args':args})

    def claim_coupon(self,args,request_id,version=None):
        _,cid=unref(args['coupon_ref'],{'coupon_template'})
        with self.store.transaction(write=True) as tx:
            t=tx.one('SELECT * FROM coupon_templates WHERE coupon_id=%s FOR UPDATE',(cid,))
            if not t: raise BusinessError('not_found','优惠券不存在')
            self.coupon_scope_check(t)
            prior=self.receipt(tx,request_id)
            if prior:
                if prior['action']!='claim_coupon' or prior['arguments_digest']!=digest(args): raise BusinessError('invalid_arguments','请求编号与原操作不一致')
                return decoded(prior['result'])
            if version is not None and self.claim_plan(tx,args)[1]!=version:
                raise BusinessError('state_changed','优惠券规则已变化，请重新确认')
            eligibility=self.claim_eligibility(tx,t)
            scopes=tx.all('SELECT scope_type,scope_id FROM coupon_scopes WHERE coupon_id=%s ORDER BY scope_type,scope_id',(cid,))
            if not scopes or any(s['scope_type'] not in {'all','merchant','product','sku'} for s in scopes): raise BusinessError('insufficient_data','券适用范围不完整')
            self.discount(t,max(money(t['threshold_amount']),Decimal(1)))
            uid=uuid4().hex; now=utcnow(); expires=at(t['fixed_expires_at']) if t['fixed_expires_at'] else now+timedelta(days=t['valid_days'])
            membership=eligibility.get('membership')
            if membership: expires=min(expires,membership['expires_at'])
            tx.execute('INSERT INTO user_coupons(user_coupon_id,coupon_id,user_id,status,claimed_at,expires_at,used_at,version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(uid,cid,self.user,'available',now,expires,None,0))
            if membership:
                from app.data.membership_store import consume_coupon_entitlement
                membership['entitlement_id']=consume_coupon_entitlement(tx,self.user,membership,uid,now)
            tx.execute('INSERT INTO coupon_claim_rules(user_coupon_id,rules_snapshot,captured_at) VALUES (%s,%s,%s)',(uid,dumps({'template':t,'scopes':scopes,'membership':membership}),now))
            if t.get('coupon_type')=='newcomer':
                tx.execute('UPDATE users SET newcomer_coupon_eligible=FALSE WHERE user_id=%s',(self.user,))
            result=decoded(dumps(ok({'coupon_ref':ref('coupon_instance',uid),'status':'available','expires_at':expires})))
            result.update(request_id=request_id,order_id='coupon:'+cid,platform_code=t['platform_code'],status='available')
            self.record_receipt(tx,request_id,'claim_coupon',args,result)
            return result
    def target_order(self,tx,action,args,lock=False):
        if action=='prepare_after_sale':
            item=tx.one('SELECT platform_code,order_id FROM order_items WHERE order_item_id=%s',(args['items'][0]['order_item_id'],))
            if not item: raise BusinessError('not_found','购买明细不存在')
            value=ref('order',item['platform_code'],item['order_id'])
        elif action=='prepare_after_sale_cancel':
            sale=tx.one('SELECT platform_code,order_id FROM after_sale_requests WHERE after_sale_id=%s AND user_id=%s',(args['after_sale_id'],self.user))
            if not sale: raise BusinessError('not_found','售后申请不存在')
            value=ref('order',sale['platform_code'],sale['order_id'])
        else: value=args['order_ref']
        return self.owned_order(tx,value,lock=lock)
    def plan(self,tx,action,args,order):
        policy=None; review={'action':action,'order_ref':ref('order',order['platform_code'],order['order_id']),'order_id':order['order_id'],'platform_code':order['platform_code'],'impact':'确认后提交此申请；申请提交不等于审核或资金处理完成'}
        extra={}
        if action=='prepare_after_sale':
            review.update(self.sale_assessment(tx,args['kind'],args['items']))
            review['title']='申请仅退款' if args['kind']=='refund_only' else '申请退货退款'
            review['reason']=args['reason']
            policy=tx.one('SELECT * FROM business_policy_versions WHERE policy_id=%s AND version=%s',(review['policy_id'],review['policy_version']))
        elif action=='prepare_after_sale_cancel':
            sale,_=self.sale(tx,args['after_sale_id'])
            policy,r=self.policy(tx,'after_sale_cancel',order)
            if not r.local_authority: raise BusinessError('insufficient_data','售后撤回控制未接通')
            if sale['status']!='pending_review': raise BusinessError('not_eligible','仅可撤回尚未受理的申请')
            events=tx.all('SELECT * FROM after_sale_events WHERE after_sale_id=%s ORDER BY created_at,event_id',(sale['after_sale_id'],))
            if not events or any(e['source']!='local' or e['kind']!='submitted' for e in events): raise BusinessError('insufficient_data','缺少可核实的本地售后过程')
            refunds=tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s',(sale['after_sale_id'],))
            if any(r['status'] not in {'pending','failed','cancelled'} for r in refunds): raise BusinessError('not_eligible','已有退款处理，不可撤回')
            work=tx.one('SELECT work_order_id FROM service_work_orders WHERE after_sale_id=%s',(sale['after_sale_id'],))
            if work:
                jobs=tx.all('SELECT status,attempts FROM commerce_provider_requests WHERE target_id=%s',(work['work_order_id'],))
                if any(j['status']!='queued' or j['attempts'] for j in jobs): raise BusinessError('not_eligible','业务已提交处理方，不能本地撤回')
            if any(r['status']=='pending' for r in refunds) and (not work or not jobs):
                raise BusinessError('not_eligible','待处理退款缺少未发出任务证明，不能本地撤回')
            extra={'sale':sale,'events':events,'refunds':refunds,'work':work}; review.update(title='撤销售后申请',after_sale_id=sale['after_sale_id'],reason=args.get('reason'))
        elif action=='prepare_order_update':
            policy,changes,extra=self.order_update_plan(tx,order,args)
            review.update(changes)
        else:
            assessment=action.removeprefix('prepare_order_') if action.startswith('prepare_order_') else 'invoice'
            assessment={'cancel':'cancel_order','address_change':'change_address'}.get(assessment,assessment)
            review.update(self.assess_order(tx,order,assessment)); policy=self.policy(tx,assessment,order)[0]
            if action=='prepare_invoice': review.update(title='提交开票申请',invoice_type=args['invoice_type'],invoice_title='***',tax_number='***' if args.get('tax_number') else None)
            if action=='prepare_order_address_change':
                address=tx.one('SELECT * FROM user_addresses WHERE address_id=%s AND user_id=%s AND deleted_at IS NULL',(args['address_id'],self.user))
                old=tx.one('SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s',(order['platform_code'],order['order_id']))
                if not address or not old: raise BusinessError('not_found','地址或订单收货快照不存在')
                if not old['city_code'] or (address['province_code'],address['city_code'])!=(old['province_code'],old['city_code']):
                    raise BusinessError('not_eligible','收货地址仅限同城修改')
                items=self.order_rows(tx,'order_items',order)
                delivery=self.delivery_for_address(tx,items,address,order['platform_code'])['data']
                if delivery['currency']!=order['currency']:
                    raise BusinessError('insufficient_data','配送币种与订单不同')
                extra={'address':json_binary(address),'previous':json_binary(old),'delivery':delivery}
                review.update(title='修改订单收货地址',address_id=args['address_id'],region=[address[k] for k in ['province_code','city_code','district_code']],impact='仅更新此订单地址，保留变更历史')
                payable=money(order['payable_amount'])-money(order['shipping_amount'])+money(delivery['shipping_amount'])
                if payable<0: raise BusinessError('insufficient_data','订单金额无法核实')
                review.update(original_shipping_amount=order['shipping_amount'],shipping_amount=delivery['shipping_amount'],payable_amount=payable,currency=delivery['currency'],delivery_days=delivery['delivery_days'])
            if action=='prepare_order_cancel':
                facts=self.cancellation_facts(tx,order); extra['cancellation']=facts
                if facts['mode']=='unpaid':
                    self.validate_reservations(tx,order)
                    review.update(title='取消未支付订单',reason=args.get('reason'),impact='取消订单并释放该订单库存与券占用')
                else:
                    if not args.get('reason'): raise BusinessError('invalid_arguments','已付款取消需要退款原因')
                    self.provider_required('order_cancel')
                    review.update(title='申请取消已付款订单',reason=args['reason'],cancellation_mode=facts['mode'],refund_amount=facts['amount'],shipping_deduction=facts['shipping_deduction'],currency=facts['currency'],impact='审核通过后全额原路退款' if facts['mode']=='paid_unshipped' else '拒收并退回商家验货后扣除订单运费原路退款；申请不等于已经取消')
        if action in {'prepare_after_sale','prepare_invoice'}:
            extra['processing']=self.processing_plan(tx,order,args['kind'] if action=='prepare_after_sale' else 'invoice',review)
        version=digest({'order':self.snapshot(tx,order),'policy':policy,'extra':extra,'args':args})
        return review,version
    def preview(self,action,args):
        if action not in PREPARES: raise BusinessError('unsupported_capability','不支持的业务操作')
        with self.store.transaction() as tx:
            if action in {'claim_coupon','request_dispatch'}:
                review,version=(self.claim_plan if action=='claim_coupon' else self.dispatch_plan)(tx,args)
                review['action']=action
                target_id=review['order_id']
                if action=='claim_coupon': review.pop('order_id')
                sealed=self.cipher.seal(dumps({'action':action,'arguments':args,'user_id':self.user}),'operation:'+self.user)
                return decoded(dumps({'action':action,'arguments':{'order_id':target_id,'platform_code':review['platform_code'],
                    'sealed':base64.b64encode(sealed).decode(),'key_version':self.cipher.version},'business_version':version,'review':review}))
            order=self.target_order(tx,action,args); review,version=self.plan(tx,action,args,order)
            private=self.private_review(tx,action,args)
            sealed=self.cipher.seal(dumps({'action':action,'arguments':args,'user_id':self.user,'private_review':private}), 'operation:'+self.user)
            return decoded(dumps({'action':action,'arguments':{'order_id':order['order_id'],'platform_code':order['platform_code'],'sealed':base64.b64encode(sealed).decode(),'key_version':self.cipher.version},'business_version':version,'review':review}))
    def submit(self,action,arguments,oid,version):
        if action not in PREPARES: raise BusinessError('unsupported_capability','不支持的业务操作')
        body=decoded(self.cipher.open(base64.b64decode(arguments['sealed']),'operation:'+self.user,arguments['key_version']))
        if body['action']!=action or body['user_id']!=self.user: raise BusinessError('invalid_arguments','操作绑定不匹配')
        args=body['arguments']
        if action=='claim_coupon': return self.claim_coupon(args,oid,version=version)
        if action=='request_dispatch': return self.request_dispatch(args,oid,version=version,enqueue_only=True)
        with self.store.transaction(write=True) as tx:
            # Establish the repeatable-read snapshot only after acquiring the
            # parent lock; item lookup before it can retain a pre-wait snapshot.
            order=self.owned_order(tx,ref('order',arguments['platform_code'],arguments['order_id']),lock=True)
            target=self.target_order(tx,action,args)
            if (target['platform_code'],target['order_id'])!=(order['platform_code'],order['order_id']):
                raise BusinessError('invalid_arguments','确认对象与密封申请内容不一致')
            prior=self.receipt(tx,oid,lock=True)
            if prior:
                if prior['action']!=action or prior['arguments_digest']!=digest(args): raise BusinessError('invalid_arguments','操作编号冲突')
                return decoded(prior['result'])
            review,current=self.plan(tx,action,args,order)
            if current!=version: raise BusinessError('state_changed','业务状态或规则已变化，请重新准备')
            now=utcnow(); result={'success':True,'ok':True,'request_id':oid,'order_id':order['order_id'],'platform_code':order['platform_code'],'status':'submitted'}
            if action=='prepare_after_sale':
                aid=uuid4().hex
                tx.execute('INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,requested_amount,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(aid,order['platform_code'],order['order_id'],self.user,args['kind'],'pending_review',args['reason'],review['amount'],now,now))
                for item in review['items']:
                    tx.execute('INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity,requested_amount) VALUES (%s,%s,%s,%s)',(aid,item['order_item_id'],item['quantity'],item['amount']))
                tx.execute("UPDATE after_sale_requests SET stage='awaiting_review' WHERE after_sale_id=%s",(aid,))
                self.sale_event(tx,aid,oid,'submitted',{}); result['after_sale_id']=aid
                details=self.processing_plan(tx,order,args['kind'],review)
                result['work_order_id']=self.enqueue_standard_sale(tx,order,oid,aid,args,details)
            elif action=='prepare_invoice':
                iid=uuid4().hex
                title=self.cipher.seal(args['title'],'invoice-title:'+self.user)
                tax=self.cipher.seal(args['tax_number'],'invoice-tax:'+self.user) if args.get('tax_number') else None
                tx.execute('INSERT INTO invoice_requests(invoice_id,platform_code,order_id,user_id,invoice_type,title_ciphertext,tax_number_ciphertext,key_version,status,request_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(iid,order['platform_code'],order['order_id'],self.user,args['invoice_type'],title,tax,self.cipher.version,'pending',oid,now))
                result['invoice_id']=iid
                details=self.processing_plan(tx,order,'invoice',review)
                tx.execute('UPDATE invoice_requests SET is_demo=%s WHERE invoice_id=%s',(review['is_demo'],iid))
                self.queue_provider(tx,oid,'invoice',iid,order,dict(details,invoice_id=iid,title=args['title'],invoice_type=args['invoice_type'],tax_number=args.get('tax_number')))
            elif action=='prepare_order_update':
                self.save_order_update(tx,order,args,oid)
                result['status']='updated'
            elif action=='prepare_order_address_change':
                address=tx.one('SELECT * FROM user_addresses WHERE address_id=%s AND user_id=%s AND deleted_at IS NULL FOR UPDATE',(args['address_id'],self.user))
                old=tx.one('SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s',(order['platform_code'],order['order_id']))
                # Recheck the locked address before changing the snapshot.
                _,locked_version=self.plan(tx,action,args,order)
                if locked_version!=version: raise BusinessError('state_changed','地址已变化，请重新准备')
                fields=['recipient_name_ciphertext','phone_ciphertext','province_code','city_code','district_code','detail_ciphertext','key_version']
                tx.execute('INSERT INTO order_address_changes(change_id,platform_code,order_id,operation_id,previous_ciphertext,replacement_ciphertext,key_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(uuid4().hex,order['platform_code'],order['order_id'],oid,self.cipher.seal(dumps(json_binary(old)),'address-history:'+self.user),self.cipher.seal(dumps(json_binary(address)),'address-history:'+self.user),self.cipher.version,now))
                tx.execute('UPDATE order_address_snapshots SET '+','.join(f+'=%s' for f in fields)+',captured_at=%s WHERE platform_code=%s AND order_id=%s',tuple(address[f] for f in fields)+(now,order['platform_code'],order['order_id']))
                tx.execute('UPDATE orders SET shipping_amount=%s,payable_amount=%s,updated_at=%s WHERE platform_code=%s AND order_id=%s',(review['shipping_amount'],review['payable_amount'],now,order['platform_code'],order['order_id']))
                result['status']='updated'
            elif action=='prepare_order_cancel' and order['status']!='pending_payment':
                result.update(self.submit_paid_cancellation(tx,order,args,oid,self.cancellation_facts(tx,order)))
            elif action=='prepare_order_cancel':
                self.release_reservations(tx,order)
                for red in self.order_rows(tx,'coupon_redemptions',order):
                    if red['status']=='released': continue
                    if red['status']!='reserved': raise BusinessError('not_eligible','存在无法释放的优惠券核销')
                    coupon=tx.one('SELECT * FROM user_coupons WHERE user_coupon_id=%s AND user_id=%s FOR UPDATE',(red['user_coupon_id'],self.user))
                    if not coupon or coupon['status']!='reserved': raise BusinessError('insufficient_data','券占用与订单不一致')
                    tx.execute('UPDATE user_coupons SET status=%s,version=version+1 WHERE user_coupon_id=%s',('expired' if at(coupon['expires_at'])<=now else 'available',coupon['user_coupon_id']))
                    tx.execute("UPDATE coupon_redemptions SET status='released',released_at=%s WHERE redemption_id=%s",(now,red['redemption_id']))
                tx.execute("UPDATE orders SET status='cancelled',updated_at=%s WHERE platform_code=%s AND order_id=%s",(now,order['platform_code'],order['order_id']));result['status']='cancelled'
            else:
                tx.execute("UPDATE after_sale_requests SET status='cancelled',stage='cancelled',closed_at=%s,updated_at=%s WHERE after_sale_id=%s AND user_id=%s",(now,now,args['after_sale_id'],self.user))
                work=tx.one('SELECT work_order_id FROM service_work_orders WHERE after_sale_id=%s',(args['after_sale_id'],))
                if work:
                    self.release_service_inventory(tx,work['work_order_id'])
                    tx.execute("UPDATE service_work_orders SET status='cancelled',updated_at=%s WHERE work_order_id=%s",(now,work['work_order_id']))
                    tx.execute("UPDATE commerce_provider_requests SET status='cancelled',updated_at=%s WHERE target_id=%s AND status='queued'",(now,work['work_order_id']))
                    tx.execute("UPDATE refund_transactions SET status='cancelled' WHERE after_sale_id=%s AND status='pending'",(args['after_sale_id'],))
                self.sale_event(tx,args['after_sale_id'],oid,'cancelled',{'reason':args.get('reason')});result.update(after_sale_id=args['after_sale_id'],status='cancelled')
            if action in {'prepare_after_sale','prepare_after_sale_cancel'}:
                self.sync_order_after_sale(tx,order)
            tx.execute('INSERT INTO order_events(event_id,platform_code,order_id,operation_id,kind,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(uuid4().hex,order['platform_code'],order['order_id'],oid,action.removeprefix('prepare_'),dumps({'status':result['status']}),now))
            if review.get('is_demo'):
                result.update(is_demo=True,source='simulated_provider')
            result=decoded(dumps(result))
            self.record_receipt(tx,oid,action,args,result)
            return result
    def sale_event(self,tx,aid,oid,kind,payload):
        tx.execute('INSERT INTO after_sale_events(event_id,after_sale_id,operation_id,kind,source,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(uuid4().hex,aid,oid,kind,'local',dumps(payload),utcnow()))
    def validate_reservations(self,tx,order):
        items=self.order_rows(tx,'order_items',order); reservations=self.order_rows(tx,'inventory_reservations',order)
        known={r['order_item_id']:r for r in items}
        if not items or any(r['order_item_id'] not in known or r['sku_id']!=known[r['order_item_id']]['sku_id'] or r['status']!='reserved' for r in reservations): raise BusinessError('insufficient_data','库存预留归属无法核实')
        if any(sum(r['quantity'] for r in reservations if r['order_item_id']==item['order_item_id'])!=item['quantity'] for item in items): raise BusinessError('insufficient_data','库存预留数量不完整')
        return reservations
    def release_reservations(self,tx,order):
        rows=self.validate_reservations(tx,order); totals={}
        for r in rows: totals[r['sku_id']]=totals.get(r['sku_id'],0)+r['quantity']
        for sid,qty in sorted(totals.items()):
            inv=tx.one('SELECT * FROM sku_inventory WHERE sku_id=%s FOR UPDATE',(sid,))
            if not inv or inv['reserved_qty']<qty: raise BusinessError('insufficient_data','库存预留汇总不一致')
            if tx.execute('UPDATE sku_inventory SET available_qty=available_qty+%s,reserved_qty=reserved_qty-%s,version=version+1,updated_at=%s WHERE sku_id=%s AND version=%s',(qty,qty,utcnow(),sid,inv['version']))!=1: raise BusinessError('state_changed','库存已变化')
        tx.execute("UPDATE inventory_reservations SET status='released',version=version+1,updated_at=%s WHERE platform_code=%s AND order_id=%s",(utcnow(),order['platform_code'],order['order_id']))
