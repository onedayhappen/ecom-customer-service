"""Prepared mutations and transactional, request-bound business receipts."""
import base64
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from app.services.commerce.common import BusinessError,at,decoded,digest,dumps,money,ok,ref,unref,utcnow

PREPARES={'prepare_after_sale','prepare_invoice','prepare_order_address_change','prepare_order_cancel','prepare_after_sale_cancel'}

def json_binary(row):
    return {k:base64.b64encode(bytes(v)).decode() if isinstance(v,(bytes,bytearray,memoryview)) else v for k,v in row.items()}

class WriteService:
    def private_review(self,tx,action,args):
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
    def receipt(self,tx,oid):
        r=tx.one('SELECT * FROM business_operation_receipts WHERE operation_id=%s AND user_id=%s',(oid,self.user))
        return r
    def record_receipt(self,tx,oid,action,args,result):
        tx.execute('INSERT INTO business_operation_receipts(operation_id,user_id,action,arguments_digest,result,created_at) VALUES (%s,%s,%s,%s,%s,%s)',(oid,self.user,action,digest(args),dumps(result),utcnow()))
    def claim_coupon(self,args,request_id):
        _,cid=unref(args['coupon_ref'],{'coupon_template'})
        with self.store.transaction(write=True) as tx:
            t=tx.one('SELECT * FROM coupon_templates WHERE coupon_id=%s FOR UPDATE',(cid,))
            if not t: raise BusinessError('not_found','优惠券不存在')
            self.coupon_scope_check(t)
            prior=self.receipt(tx,request_id)
            if prior:
                if prior['action']!='claim_coupon' or prior['arguments_digest']!=digest(args): raise BusinessError('invalid_arguments','请求编号与原操作不一致')
                return decoded(prior['result'])
            self.claim_eligibility(tx,t)
            scopes=tx.all('SELECT scope_type,scope_id FROM coupon_scopes WHERE coupon_id=%s ORDER BY scope_type,scope_id',(cid,))
            if not scopes or any(s['scope_type'] not in {'all','merchant','product','sku'} for s in scopes): raise BusinessError('insufficient_data','券适用范围不完整')
            self.discount(t,max(money(t['threshold_amount']),Decimal(1)))
            uid=uuid4().hex; now=utcnow(); expires=at(t['fixed_expires_at']) if t['fixed_expires_at'] else now+timedelta(days=t['valid_days'])
            tx.execute('INSERT INTO user_coupons(user_coupon_id,coupon_id,user_id,status,claimed_at,expires_at,used_at,version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(uid,cid,self.user,'available',now,expires,None,0))
            tx.execute('INSERT INTO coupon_claim_rules(user_coupon_id,rules_snapshot,captured_at) VALUES (%s,%s,%s)',(uid,dumps({'template':t,'scopes':scopes}),now))
            result=decoded(dumps(ok({'coupon_ref':ref('coupon_instance',uid),'status':'available','expires_at':expires})))
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
            policy=self.policy(tx,args['kind'],order)[0]
        elif action=='prepare_after_sale_cancel':
            sale,_=self.sale(tx,args['after_sale_id'])
            policy,r=self.policy(tx,'after_sale_cancel',order)
            if not r.local_authority: raise BusinessError('insufficient_data','售后撤回控制未接通')
            if sale['status']!='pending_review': raise BusinessError('not_eligible','仅可撤回尚未受理的申请')
            events=tx.all('SELECT * FROM after_sale_events WHERE after_sale_id=%s ORDER BY created_at,event_id',(sale['after_sale_id'],))
            if not events or any(e['source']!='local' or e['kind']!='submitted' for e in events): raise BusinessError('insufficient_data','缺少可核实的本地售后过程')
            refunds=tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s',(sale['after_sale_id'],))
            if any(r['status'] not in {'failed','cancelled'} for r in refunds): raise BusinessError('not_eligible','已有退款处理，不可撤回')
            extra={'sale':sale,'events':events,'refunds':refunds}; review.update(title='撤销售后申请',after_sale_id=sale['after_sale_id'],reason=args['reason'])
        else:
            assessment=action.removeprefix('prepare_order_') if action.startswith('prepare_order_') else 'invoice'
            assessment={'cancel':'cancel_order','address_change':'change_address'}.get(assessment,assessment)
            review.update(self.assess_order(tx,order,assessment)); policy=self.policy(tx,assessment,order)[0]
            if action=='prepare_invoice': review.update(title='提交开票申请',invoice_type=args['invoice_type'],invoice_title='***',tax_number='***' if args.get('tax_number') else None)
            if action=='prepare_order_address_change':
                address=tx.one('SELECT * FROM user_addresses WHERE address_id=%s AND user_id=%s AND deleted_at IS NULL',(args['address_id'],self.user))
                old=tx.one('SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s',(order['platform_code'],order['order_id']))
                if not address or not old: raise BusinessError('not_found','地址或订单收货快照不存在')
                extra={'address':json_binary(address),'previous':json_binary(old)}
                review.update(title='修改订单收货地址',address_id=args['address_id'],region=[address[k] for k in ['province_code','city_code','district_code']],impact='仅更新此订单地址，保留变更历史')
            if action=='prepare_order_cancel':
                self.validate_reservations(tx,order)
                review.update(title='取消未支付订单',reason=args['reason'],impact='取消订单并释放该订单库存与券占用')
        version=digest({'order':self.snapshot(tx,order),'policy':policy,'extra':extra,'args':args})
        return review,version
    def preview(self,action,args):
        if action not in PREPARES: raise BusinessError('unsupported_capability','不支持的业务操作')
        with self.store.transaction() as tx:
            order=self.target_order(tx,action,args); review,version=self.plan(tx,action,args,order)
            private=self.private_review(tx,action,args)
            sealed=self.cipher.seal(dumps({'action':action,'arguments':args,'user_id':self.user,'private_review':private}), 'operation:'+self.user)
            return decoded(dumps({'action':action,'arguments':{'order_id':order['order_id'],'platform_code':order['platform_code'],'sealed':base64.b64encode(sealed).decode(),'key_version':self.cipher.version},'business_version':version,'review':review}))
    def submit(self,action,arguments,oid,version):
        if action not in PREPARES: raise BusinessError('unsupported_capability','不支持的业务操作')
        body=decoded(self.cipher.open(base64.b64decode(arguments['sealed']),'operation:'+self.user,arguments['key_version']))
        if body['action']!=action or body['user_id']!=self.user: raise BusinessError('invalid_arguments','操作绑定不匹配')
        args=body['arguments']
        with self.store.transaction(write=True) as tx:
            order=self.target_order(tx,action,args,lock=True)
            prior=self.receipt(tx,oid)
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
                self.sale_event(tx,aid,oid,'submitted',{}); result['after_sale_id']=aid
            elif action=='prepare_invoice':
                iid=uuid4().hex
                title=self.cipher.seal(args['title'],'invoice-title:'+self.user)
                tax=self.cipher.seal(args['tax_number'],'invoice-tax:'+self.user) if args.get('tax_number') else None
                tx.execute('INSERT INTO invoice_requests(invoice_id,platform_code,order_id,user_id,invoice_type,title_ciphertext,tax_number_ciphertext,key_version,status,request_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(iid,order['platform_code'],order['order_id'],self.user,args['invoice_type'],title,tax,self.cipher.version,'pending',oid,now))
                result['invoice_id']=iid
            elif action=='prepare_order_address_change':
                address=tx.one('SELECT * FROM user_addresses WHERE address_id=%s AND user_id=%s AND deleted_at IS NULL FOR UPDATE',(args['address_id'],self.user))
                old=tx.one('SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s',(order['platform_code'],order['order_id']))
                # Recheck the locked address before changing the snapshot.
                _,locked_version=self.plan(tx,action,args,order)
                if locked_version!=version: raise BusinessError('state_changed','地址已变化，请重新准备')
                fields=['recipient_name_ciphertext','phone_ciphertext','province_code','city_code','district_code','detail_ciphertext','key_version']
                tx.execute('INSERT INTO order_address_changes(change_id,platform_code,order_id,operation_id,previous_ciphertext,replacement_ciphertext,key_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(uuid4().hex,order['platform_code'],order['order_id'],oid,self.cipher.seal(dumps(json_binary(old)),'address-history:'+self.user),self.cipher.seal(dumps(json_binary(address)),'address-history:'+self.user),self.cipher.version,now))
                tx.execute('UPDATE order_address_snapshots SET '+','.join(f+'=%s' for f in fields)+',captured_at=%s WHERE platform_code=%s AND order_id=%s',tuple(address[f] for f in fields)+(now,order['platform_code'],order['order_id']))
                result['status']='updated'
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
                tx.execute("UPDATE after_sale_requests SET status='cancelled',closed_at=%s,updated_at=%s WHERE after_sale_id=%s AND user_id=%s",(now,now,args['after_sale_id'],self.user))
                self.sale_event(tx,args['after_sale_id'],oid,'cancelled',{'reason':args['reason']});result.update(after_sale_id=args['after_sale_id'],status='cancelled')
            tx.execute('INSERT INTO order_events(event_id,platform_code,order_id,operation_id,kind,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(uuid4().hex,order['platform_code'],order['order_id'],oid,action.removeprefix('prepare_'),dumps({'status':result['status']}),now))
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
