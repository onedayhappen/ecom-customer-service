"""Confirmed extended operations with locked revalidation and durable provider work."""
import base64
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from app.services.commerce.common import BusinessError,at,decoded,digest,dumps,money,ref,unref,utcnow
from app.services.commerce.writes import WriteService,json_binary

EXTENDED_PREPARES={'prepare_price_protection','prepare_exchange','prepare_return_shipment',
    'prepare_repair','prepare_invoice_reissue','prepare_purchase'}


class ExtendedWriteService(WriteService):
    def provider_required(self,kind):
        from app.services.commerce.providers import gateway
        provider=gateway(kind)
        if provider is None: raise BusinessError('unsupported_capability','该业务的实际处理渠道尚未配置')
        return provider

    def queue_provider(self,tx,oid,kind,target_id,order,payload):
        self.provider_required(kind)
        tx.execute('''INSERT INTO commerce_provider_requests(request_id,user_id,platform_code,order_id,kind,target_id,
            payload_ciphertext,key_version,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (oid,self.user,order['platform_code'],order['order_id'],kind,target_id,
             self.cipher.seal(dumps(payload),'provider:'+oid),self.cipher.version,'queued',utcnow(),utcnow()))

    def extended_target(self,tx,action,args,lock=False):
        if action=='prepare_purchase':
            quote,snapshot=self.owned_quote(tx,args['quote_ref'],lock=lock)
            return {'order_id':quote['order_id'],'platform_code':quote['platform_code'],'merchant_id':snapshot['merchant_id'],'user_id':self.user}
        if action=='prepare_return_shipment':
            _,order=self.sale(tx,args['after_sale_id'],lock=lock); return order
        if action=='prepare_invoice_reissue':
            _,order=self.issued_invoice(tx,args['issued_invoice_id'])
            return self.owned_order(tx,ref('order',order['platform_code'],order['order_id']),lock=lock)
        return self.item_order(tx,args['order_item_id'],lock=lock)[1]

    def extended_plan(self,tx,action,args,order):
        kind=action.removeprefix('prepare_'); extra={}; private={}
        review={'action':action,'title':{'price_protection':'申请价格保护','exchange':'申请换货','repair':'申请维修',
            'return_shipment':'登记退货运单','invoice_reissue':'申请发票更正','purchase':'创建待支付订单'}[kind],
            'order_id':order['order_id'],'platform_code':order['platform_code'],
            'impact':'确认后提交；申请提交不代表审核通过、物流收件或资金到账'}
        if kind=='purchase':
            quote,saved=self.owned_quote(tx,args['quote_ref'])
            if quote['status']!='active' or at(quote['expires_at'])<=utcnow(): raise BusinessError('state_changed','报价已失效，请重新报价')
            current=self.checkout_snapshot(tx,saved['arguments'])
            if digest(current)!=quote['snapshot_digest']: raise BusinessError('state_changed','价格、地址、权益或规则发生变化，请重新报价')
            extra={'quote_digest':quote['snapshot_digest'],'expires_at':quote['expires_at']}
            review.update(currency=current['pricing']['currency'],amount=current['payable_amount'],amount_kind='purchase_total',
                items=[dict(i,name=self.sku(tx,i['sku_id'])['product_name']) for i in current['pricing']['items']],
                shipping_amount=current['delivery']['shipping_amount'],quote_ref=args['quote_ref'],impact='创建待支付订单并占用库存和所选优惠券；不会自动支付')
            private=self.private_review(tx,'prepare_order_address_change',{'address_id':saved['arguments']['address_id']})
        elif kind=='return_shipment':
            auth,sale,_=self.return_authorization(tx,args['after_sale_id'])
            carriers=decoded(auth['carriers'])
            if args['carrier_code'] not in {r['code'] for r in carriers}: raise BusinessError('not_eligible','承运商不在退货授权范围内')
            existing=tx.all('SELECT * FROM return_shipments WHERE authorization_id=%s',(auth['authorization_id'],))
            requested=tx.all('SELECT order_item_id,quantity FROM after_sale_items WHERE after_sale_id=%s',(sale['after_sale_id'],))
            remaining={i['order_item_id']:i['quantity'] for i in requested}
            for ship in existing:
                lines=tx.all('SELECT order_item_id,quantity FROM return_shipment_items WHERE return_shipment_id=%s',(ship['return_shipment_id'],))
                if not lines: raise BusinessError('insufficient_data','已有寄回包裹缺少商品明细，无法核实剩余可寄数量')
                for line in lines: remaining[line['order_item_id']]=remaining.get(line['order_item_id'],0)-line['quantity']
            items=args.get('items') or [{'order_item_id':i,'quantity':q} for i,q in remaining.items() if q>0]
            if not items or len({i['order_item_id'] for i in items})!=len(items) or any(i['quantity']<=0 or i['quantity']>remaining.get(i['order_item_id'],0) for i in items):
                raise BusinessError('not_eligible','寄回商品或数量超过售后剩余可寄范围')
            extra={'authorization':json_binary(auth),'sale':sale,'remaining':remaining}
            review.update(after_sale_id=sale['after_sale_id'],carrier_code=args['carrier_code'],tracking_number=args['tracking_number'],items=items)
        elif kind=='invoice_reissue':
            self.provider_required(kind); invoice,_=self.issued_invoice(tx,args['issued_invoice_id'])
            policy,r=self.policy(tx,kind,order)
            if (order['status'] not in r.allowed_statuses and self.order_base_status(order) not in r.allowed_statuses) or invoice['status']!='issued': raise BusinessError('not_eligible','原票或订单状态不支持更正')
            existing=tx.all('SELECT * FROM invoice_corrections WHERE issued_invoice_id=%s ORDER BY correction_id',(invoice['issued_invoice_id'],))
            if any(x['status'] not in {'rejected','cancelled','failed'} for x in existing): raise BusinessError('not_eligible','原票已有更正申请')
            extra={'invoice':invoice,'policy':policy,'corrections':existing}
            review.update(issued_invoice_id=invoice['issued_invoice_id'],invoice_type=args['invoice_type'],invoice_title='***',tax_number='***' if args.get('tax_number') else None)
            private={'invoice_title':args['title'],'tax_number':args.get('tax_number')}
        else:
            self.provider_required(kind)
            item,_=self.item_order(tx,args['order_item_id'])
            policy,r=self.policy(tx,kind,order,items=[item])
            if kind=='price_protection':
                assessment=self.price_protection(tx,args['order_item_id']); review.update(assessment,amount_kind='price_protection'); extra={'assessment':assessment,'policy':policy}
            else:
                assessment=self.sale_assessment(tx,kind,[{'order_item_id':args['order_item_id'],'quantity':args['quantity']}])
                item,_=self.item_order(tx,args['order_item_id']); extra={'assessment':assessment,'policy':policy}
                review.update(order_item_id=item['order_item_id'],items=[{'name':item['title_snapshot'],'quantity':args['quantity']}],quantity=args['quantity'],currency=order['currency'],amount=Decimal(0),amount_kind='service_request',reason=args.get('reason') or args.get('problem'))
                if kind=='exchange':
                    replacement=self.sku(tx,args['replacement_sku_id'])
                    if replacement['product_id']!=item['product_id'] or replacement['merchant_id']!=order['merchant_id'] or replacement['currency']!=order['currency']:
                        raise BusinessError('not_eligible','换货规格须为同商家同商品同币种')
                    if replacement['status']!='active' or not replacement['inventory'] or replacement['inventory']['available_qty']<args['quantity']:
                        raise BusinessError('not_eligible','换货规格不可售或库存不足')
                    difference=money(replacement['list_price'])*args['quantity']-money(assessment['amount'])
                    if difference and not r.allow_exchange_price_difference: raise BusinessError('not_eligible','当前规则不支持有价差换货')
                    extra['replacement']={k:v for k,v in replacement.items() if k!='inventory'}
                    review.update(replacement_sku_id=args['replacement_sku_id'],price_difference=difference)
        version=digest({'action':action,'args':args,'extra':extra,'order':None if kind=='purchase' else self.snapshot(tx,order)})
        return review,version,private

    def preview(self,action,args):
        if action not in EXTENDED_PREPARES: return super().preview(action,args)
        with self.store.transaction() as tx:
            order=self.extended_target(tx,action,args); review,version,private=self.extended_plan(tx,action,args,order)
            sealed=self.cipher.seal(dumps({'action':action,'arguments':args,'user_id':self.user,'private_review':private}),'operation:'+self.user)
            return decoded(dumps({'action':action,'arguments':{'order_id':order['order_id'],'platform_code':order['platform_code'],
                'sealed':base64.b64encode(sealed).decode(),'key_version':self.cipher.version},'business_version':version,'review':review}))

    def submit(self,action,arguments,oid,version):
        if action not in EXTENDED_PREPARES: return super().submit(action,arguments,oid,version)
        body=decoded(self.cipher.open(base64.b64decode(arguments['sealed']),'operation:'+self.user,arguments['key_version']))
        if body['action']!=action or body['user_id']!=self.user: raise BusinessError('invalid_arguments','操作绑定不匹配')
        args=body['arguments']; kind=action.removeprefix('prepare_')
        with self.store.transaction(write=True) as tx:
            order=self.extended_target(tx,action,args,lock=True)
            prior=self.receipt(tx,oid)
            if prior:
                if prior['action']!=action or prior['arguments_digest']!=digest(args): raise BusinessError('invalid_arguments','操作编号冲突')
                return decoded(prior['result'])
            # Stock/coupon/address locks precede the second quotation and all writes.
            if kind=='purchase':
                _,saved=self.owned_quote(tx,args['quote_ref']); purchase_args=saved['arguments']
                self.address(tx,purchase_args['address_id'],lock=True)
                for sid in sorted({i['sku_id'] for i in purchase_args['items']}):
                    tx.one('SELECT sku_id FROM product_skus WHERE sku_id=%s FOR UPDATE',(sid,))
                    tx.one('SELECT sku_id FROM sku_inventory WHERE sku_id=%s FOR UPDATE',(sid,))
                for value in sorted(purchase_args.get('coupon_refs',[])):
                    _,cid=unref(value,{'coupon_instance'})
                    tx.one('SELECT user_coupon_id FROM user_coupons WHERE user_coupon_id=%s AND user_id=%s FOR UPDATE',(cid,self.user))
            if kind=='exchange':
                tx.one('SELECT sku_id FROM product_skus WHERE sku_id=%s FOR UPDATE',(args['replacement_sku_id'],))
                tx.one('SELECT sku_id FROM sku_inventory WHERE sku_id=%s FOR UPDATE',(args['replacement_sku_id'],))
            review,current,_=self.extended_plan(tx,action,args,order)
            if current!=version: raise BusinessError('state_changed','业务状态或规则已变化，请重新准备')
            now=utcnow(); result={'success':True,'ok':True,'status':'submitted','request_id':oid,'order_id':order['order_id'],'platform_code':order['platform_code']}
            if kind=='purchase':
                self.create_purchase(tx,args,order,oid); result['status']='pending_payment'; result['order_ref']=ref('order',order['platform_code'],order['order_id'])
            elif kind=='return_shipment':
                auth,_,_=self.return_authorization(tx,args['after_sale_id']); rid=uuid4().hex
                tx.execute('INSERT INTO return_shipments(return_shipment_id,authorization_id,carrier_code,tracking_number,status,operation_id,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(rid,auth['authorization_id'],args['carrier_code'],args['tracking_number'],'registered',oid,now,now))
                for item in review['items']:
                    tx.execute('INSERT INTO return_shipment_items(return_shipment_id,after_sale_id,order_item_id,quantity) VALUES (%s,%s,%s,%s)',(rid,args['after_sale_id'],item['order_item_id'],item['quantity']))
                tx.execute("UPDATE after_sale_requests SET stage='awaiting_collection',updated_at=%s WHERE after_sale_id=%s",(now,args['after_sale_id']))
                self.sale_event(tx,args['after_sale_id'],oid,'return_registered',{'return_shipment_id':rid})
                result.update(return_shipment_id=rid,after_sale_id=args['after_sale_id'],status='registered')
            elif kind=='invoice_reissue':
                iid=uuid4().hex; correction=uuid4().hex
                tx.execute('''INSERT INTO invoice_requests(invoice_id,platform_code,order_id,user_id,invoice_type,title_ciphertext,tax_number_ciphertext,key_version,status,request_id,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(iid,order['platform_code'],order['order_id'],self.user,args['invoice_type'],self.cipher.seal(args['title'],'invoice-title:'+self.user),self.cipher.seal(args['tax_number'],'invoice-tax:'+self.user) if args.get('tax_number') else None,self.cipher.version,'pending',oid,now))
                tx.execute('INSERT INTO invoice_corrections(correction_id,issued_invoice_id,invoice_id,status,operation_id,updated_at) VALUES (%s,%s,%s,%s,%s,%s)',(correction,args['issued_invoice_id'],iid,'pending',oid,now))
                self.queue_provider(tx,oid,kind,correction,order,dict(args,invoice_id=iid,correction_id=correction))
                result.update(invoice_id=iid,correction_id=correction)
            else:
                aid=uuid4().hex; wid=uuid4().hex; quantity=review['quantity']; amount=review.get('amount',Decimal(0))
                tx.execute('''INSERT INTO after_sale_requests(after_sale_id,platform_code,order_id,user_id,kind,status,reason,requested_amount,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(aid,order['platform_code'],order['order_id'],self.user,kind,'pending_review',(args.get('reason') or args.get('problem') or '')[:255],amount,now,now))
                tx.execute('INSERT INTO after_sale_items(after_sale_id,order_item_id,quantity,requested_amount) VALUES (%s,%s,%s,%s)',(aid,args['order_item_id'],quantity,amount))
                tx.execute("UPDATE after_sale_requests SET stage='awaiting_review' WHERE after_sale_id=%s",(aid,))
                self.sync_order_after_sale(tx,order)
                details={**args,'amount':amount,'price_difference':review.get('price_difference'),'currency':order['currency']}
                tx.execute('INSERT INTO service_work_orders(work_order_id,after_sale_id,kind,status,details,source,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(wid,aid,kind,'pending_review',dumps(details),'local',now))
                if kind=='exchange':
                    sid=args['replacement_sku_id']; qty=args['quantity']
                    if tx.execute('UPDATE sku_inventory SET available_qty=available_qty-%s,reserved_qty=reserved_qty+%s,version=version+1,updated_at=%s WHERE sku_id=%s AND available_qty>=%s',(qty,qty,now,sid,qty))!=1: raise BusinessError('state_changed','换货库存不足')
                    tx.execute('INSERT INTO service_inventory_reservations(work_order_id,sku_id,quantity,status,updated_at) VALUES (%s,%s,%s,%s,%s)',(wid,sid,qty,'reserved',now))
                self.sale_event(tx,aid,oid,'submitted',{'work_order_id':wid})
                self.queue_provider(tx,oid,kind,wid,order,dict(details,after_sale_id=aid,work_order_id=wid))
                result.update(after_sale_id=aid,work_order_id=wid)
            tx.execute('INSERT INTO order_events(event_id,platform_code,order_id,operation_id,kind,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',(uuid4().hex,order['platform_code'],order['order_id'],oid,kind,dumps({'status':result['status']}),now))
            self.record_receipt(tx,oid,action,args,result)
            return result

    def create_purchase(self,tx,args,order,oid):
        quote,saved=self.owned_quote(tx,args['quote_ref']); current=self.checkout_snapshot(tx,saved['arguments'])
        if digest(current)!=quote['snapshot_digest']: raise BusinessError('state_changed','报价已变化')
        pricing=current['pricing']; now=utcnow(); platform=order['platform_code']; order_id=order['order_id']
        tx.execute('''INSERT INTO orders(platform_code,order_id,user_id,status,created_at,merchant_id,currency,goods_amount,discount_amount,shipping_amount,payable_amount,paid_amount,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)''',(platform,order_id,self.user,'pending_payment',now,current['merchant_id'],pricing['currency'],pricing['goods_amount'],money(pricing['goods_amount'])-money(pricing['goods_after_discount']),current['delivery']['shipping_amount'],current['payable_amount'],now))
        address=self.address(tx,saved['arguments']['address_id'])
        fields=['recipient_name_ciphertext','phone_ciphertext','province_code','city_code','district_code','detail_ciphertext','key_version']
        tx.execute('INSERT INTO order_address_snapshots(platform_code,order_id,'+','.join(fields)+',captured_at) VALUES ('+','.join(['%s']*10)+')',(platform,order_id,*[address[f] for f in fields],now))
        for line in pricing['items']:
            sid=line['sku_id']; qty=line['quantity']; sku=self.sku(tx,sid); iid=uuid4().hex
            if tx.execute('UPDATE sku_inventory SET available_qty=available_qty-%s,reserved_qty=reserved_qty+%s,version=version+1,updated_at=%s WHERE sku_id=%s AND available_qty>=%s',(qty,qty,now,sid,qty))!=1: raise BusinessError('state_changed','库存不足')
            tx.execute('''INSERT INTO order_items(order_item_id,platform_code,order_id,sku_id,product_id,title_snapshot,spec_snapshot,quantity,unit_price,discount_amount,payable_amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(iid,platform,order_id,sid,sku['product_id'],sku['product_name'],dumps(dict(sku['spec_values'],cancel_allowed=sku['cancel_allowed'])),qty,sku['list_price'],money(sku['list_price'])*qty-money(line['amount']),line['amount']))
            tx.execute('''INSERT INTO inventory_reservations(reservation_id,platform_code,order_id,order_item_id,sku_id,quantity,status,request_id,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(uuid4().hex,platform,order_id,iid,sid,qty,'reserved',digest([oid,sid]),now,now))
        for coupon in pricing['coupons']:
            _,cid=unref(coupon['coupon_ref'],{'coupon_instance'})
            if tx.execute("UPDATE user_coupons SET status='reserved',version=version+1 WHERE user_coupon_id=%s AND user_id=%s AND status='available' AND expires_at>%s",(cid,self.user,now))!=1: raise BusinessError('state_changed','优惠券已不可用')
            tx.execute('''INSERT INTO coupon_redemptions(redemption_id,user_coupon_id,platform_code,order_id,discount_amount,currency,status,request_id,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(uuid4().hex,cid,platform,order_id,coupon['discount_amount'],pricing['currency'],'reserved',digest([oid,cid]),now))
        tx.execute("UPDATE checkout_quotes SET status='used' WHERE quote_id=%s",(quote['quote_id'],))
        tx.execute('INSERT INTO order_expirations(platform_code,order_id,expires_at,status) VALUES (%s,%s,%s,%s)',(platform,order_id,now+timedelta(minutes=current['payment_timeout_minutes']),'active'))
