"""Extended read models, delivery calculation and immutable checkout quotes."""
import math
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from app.services.commerce.common import BusinessError, at, decoded, digest, dumps, money, ok, ref, unref, utcnow
from app.services.commerce.writes import json_binary


class ExtendedService:
    def address(self,tx,address_id,lock=False):
        row=tx.one('SELECT * FROM user_addresses WHERE address_id=%s AND user_id=%s AND deleted_at IS NULL'+(' FOR UPDATE' if lock or tx.write else ''),(address_id,self.user))
        if not row: raise BusinessError('not_found','收货地址不存在')
        return row

    def quote_delivery(self,tx,**args):
        address=self.address(tx,args['address_id']); platform=self.sales_platform(tx)
        if not platform: raise BusinessError('insufficient_data','销售平台未明确')
        groups={}; merchant=None; currency=None; merged={}
        for item in args['items']: merged[item['sku_id']]=merged.get(item['sku_id'],0)+item['quantity']
        for sid,qty in sorted(merged.items()):
            if qty>999: raise BusinessError('invalid_arguments','合并后数量超过上限')
            sku=self.sku(tx,sid)
            if merchant and (merchant,currency)!=(sku['merchant_id'],sku['currency']): raise BusinessError('invalid_arguments','请按商家和币种分别报价')
            merchant,currency=sku['merchant_id'],sku['currency']
            pack=tx.one('SELECT * FROM sku_fulfillment WHERE sku_id=%s'+(' FOR UPDATE' if tx.write else ''),(sid,))
            if not pack: raise BusinessError('insufficient_data','缺少商品重量或仓库信息')
            group=groups.setdefault(pack['warehouse_id'],{'weight_grams':0,'items':[]})
            group['weight_grams']+=pack['weight_grams']*qty
            group['items'].append(dict(sku_id=sid,quantity=qty,fulfillment=pack))
        quotes=[]; now=utcnow()
        for warehouse,group in sorted(groups.items()):
            rules=tx.all("SELECT * FROM delivery_rules WHERE platform_code=%s AND merchant_id=%s AND warehouse_id=%s AND status='active' AND starts_at<=%s AND (ends_at IS NULL OR ends_at>%s) ORDER BY rule_id,version"+(' FOR UPDATE' if tx.write else ''),(platform,merchant,warehouse,now,now))
            matches=[r for r in rules if all(r[k]=='*' or r[k]==address[k] for k in ('province_code','city_code','district_code'))]
            if len(matches)!=1: raise BusinessError('insufficient_data','配送地区没有唯一有效运费规则')
            r=matches[0]
            if r['currency']!=currency: raise BusinessError('insufficient_data','运费与商品币种不一致')
            steps=max(0,math.ceil((group['weight_grams']-r['base_grams'])/r['step_grams']))
            fee=money(r['base_fee'])+steps*money(r['step_fee'])
            quotes.append(dict(group,warehouse_id=warehouse,amount=fee,rule=r))
        return ok({'currency':currency,'shipping_amount':sum((q['amount'] for q in quotes),Decimal(0)),
            'packages':quotes,'region':[address[k] for k in ('province_code','city_code','district_code')],
            'delivery_days':{'min':max(q['rule']['min_days'] for q in quotes),'max':max(q['rule']['max_days'] for q in quotes)}})

    def get_restock_plan(self,tx,**args):
        self.sku(tx,args['sku_id'])
        rows=tx.all('SELECT * FROM restock_plans WHERE sku_id=%s ORDER BY updated_at DESC,plan_id',(args['sku_id'],))
        return ok({'items':rows[:20],'total':len(rows),'truncated':len(rows)>20},limitations=['补货计划时间不是承诺送达时间'])

    def point_rows(self,tx):
        platform=self.sales_platform(tx)
        if not platform: raise BusinessError('insufficient_data','请明确会员所属平台')
        return tx.all('SELECT * FROM points_ledger WHERE user_id=%s AND platform_code=%s ORDER BY created_at,entry_id',(self.user,platform)),platform

    def list_points(self,tx,**args):
        rows,platform=self.point_rows(tx)
        return self.page([{k:v for k,v in r.items() if k!='user_id'} for r in rows],'list_points',args,
            data={'platform_code':platform,'ledger_balance':sum(r['delta'] for r in rows)},limitations=['余额按已入账流水汇总；过期扣减由积分账务系统入账'])

    def get_membership(self,tx,**args):
        points,platform=self.point_rows(tx)
        from app.data.membership_store import membership_facts
        facts=membership_facts(tx,self.user,platform,store_id=getattr(self.scope,'store_id',None))
        return ok({**facts,'ledger_balance':sum(p['delta'] for p in points)},
            limitations=['会员券配置为planned时尚未开放领取，不能视为已持有或可使用的优惠券'])

    def item_order(self,tx,item_id,lock=False):
        item=tx.one('SELECT * FROM order_items WHERE order_item_id=%s',(item_id,))
        if not item: raise BusinessError('not_found','购买明细不存在')
        order=self.owned_order(tx,ref('order',item['platform_code'],item['order_id']),lock=lock)
        return item,order

    def price_protection(self,tx,item_id):
        item,order=self.item_order(tx,item_id)
        policy,r=self.policy(tx,'price_protection',order)
        if order['status'] not in r.allowed_statuses and self.order_base_status(order) not in r.allowed_statuses: raise BusinessError('not_eligible','订单状态不符合价保规则')
        if r.requires_reason_review: raise BusinessError('insufficient_data','价保规则需要人工审核')
        benefit=tx.one('SELECT * FROM order_item_benefits WHERE order_item_id=%s',(item_id,))
        if not benefit or not benefit['price_protection_until'] or benefit['price_basis']!='cash_per_unit': raise BusinessError('insufficient_data','缺少成交时价保权益和可比价格口径')
        if not benefit['eligible'] or at(benefit['price_protection_until'])<=utcnow(): raise BusinessError('not_eligible','订单行不在价保期内')
        prices=tx.all('SELECT * FROM price_protection_prices WHERE sku_id=%s AND platform_code=%s AND starts_at<=%s AND ends_at>%s ORDER BY price_id',(item['sku_id'],order['platform_code'],utcnow(),utcnow()))
        if len(prices)!=1: raise BusinessError('insufficient_data','缺少唯一可核实的当前可比价格')
        price=prices[0]
        if price['currency']!=order['currency'] or price['price_basis']!=benefit['price_basis']: raise BusinessError('insufficient_data','价保比较口径不一致')
        sales=self.order_rows(tx,'after_sale_requests',order); prior=Decimal(0)
        for sale in sales:
            line=tx.one('SELECT * FROM after_sale_items WHERE after_sale_id=%s AND order_item_id=%s',(sale['after_sale_id'],item_id))
            if not line: continue
            if sale['status'] in {'cancelled','rejected','closed'}:
                refunds=tx.all('SELECT status FROM refund_transactions WHERE after_sale_id=%s',(sale['after_sale_id'],))
                if any(r['status'] not in {'failed','cancelled'} for r in refunds):
                    raise BusinessError('insufficient_data','已结束售后存在退款记录，需核实剩余商品的价保口径')
                continue
            if sale['kind']!='price_protection': raise BusinessError('not_eligible','该商品存在其他有效售后，请先处理原申请')
            if sale['status'] not in {'pending_review','approved','processing','completed'}: raise BusinessError('insufficient_data','价保申请状态未知')
            prior+=money(line['requested_amount'])
        difference=(money(item['payable_amount'])-money(price['amount'])*item['quantity']-prior).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        if difference<=0: raise BusinessError('not_eligible','没有可申请的价保差额')
        # Include every existing refund reservation, not just previous price protections.
        reserved=Decimal(0)
        for sale in sales:
            refunds=tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s',(sale['after_sale_id'],))
            if any(x['currency']!=order['currency'] or x['status'] not in {'pending','processing','succeeded','failed','cancelled'} for x in refunds):
                raise BusinessError('insufficient_data','退款币种或状态无法核实')
            actual=sum((money(x['amount']) for x in refunds if x['status'] in {'pending','processing','succeeded'}),Decimal(0))
            reserved+=max(actual,money(sale['requested_amount']) if sale['status'] in {'pending_review','approved','processing','completed'} else Decimal(0))
        if difference>money(order['paid_amount'])-reserved: raise BusinessError('not_eligible','价保金额超过未占用实付余额')
        return {'eligibility':'eligible','amount':difference,'currency':order['currency'],'order_item_id':item_id,
            'quantity':item['quantity'],'policy_id':policy['policy_id'],'policy_version':policy['version'],
            'price_source':price,'benefit':benefit,'prior_amount':prior}

    def assess_price_protection(self,tx,**args):
        return self.decision(lambda:self.price_protection(tx,args['order_item_id']))

    def return_authorization(self,tx,aid):
        sale,order=self.sale(tx,aid)
        auth=tx.one('SELECT * FROM return_authorizations WHERE after_sale_id=%s',(aid,))
        if not auth: raise BusinessError('insufficient_data','尚无真实退货授权')
        if sale['status'] not in {'approved','processing'} or auth['status']!='active' or at(auth['deadline'])<=utcnow(): raise BusinessError('not_eligible','退货授权未生效或已过期')
        return auth,sale,order

    def get_return_instructions(self,tx,**args):
        auth,_,_=self.return_authorization(tx,args['after_sale_id'])
        address=decoded(self.cipher.open(auth['address_ciphertext'],'return-address:'+auth['authorization_id'],auth['key_version']))
        return ok({'authorization_id':auth['authorization_id'],'address':address,'deadline':auth['deadline'],
            'carriers':decoded(auth['carriers']),'requirements':decoded(auth['requirements']),'source':auth['source']})

    def issued_invoice(self,tx,iid):
        row=tx.one('''SELECT f.*,i.platform_code,i.order_id FROM issued_invoices f
            JOIN invoice_requests i ON i.invoice_id=f.invoice_id WHERE f.issued_invoice_id=%s AND i.user_id=%s''',(iid,self.user))
        if not row: raise BusinessError('not_found','正式发票不存在')
        order=self.owned_order(tx,ref('order',row['platform_code'],row['order_id']))
        return row,order

    def get_invoice_document(self,tx,**args):
        invoice,_=self.issued_invoice(tx,args['issued_invoice_id'])
        if invoice['status']!='issued' or not invoice['document_key']: raise BusinessError('insufficient_data','正式发票文件尚未就绪')
        from app.services.commerce.providers import invoice_file
        # A configured private storage root is validated before issuing the authenticated URL.
        invoice_file(invoice)
        return ok({'issued_invoice_id':invoice['issued_invoice_id'],'invoice_number':invoice['invoice_number'],
            'mime_type':invoice['mime_type'],'sha256':invoice['sha256'],
            'download_url':'/api/commerce/invoices/'+invoice['issued_invoice_id']+'/document'})

    def checkout_snapshot(self,tx,args):
        pricing=self.estimate_purchase(tx,items=args['items'],coupon_refs=args.get('coupon_refs',[]))['data']
        delivery=self.quote_delivery(tx,items=args['items'],address_id=args['address_id'])['data']
        address=self.address(tx,args['address_id']); platform=self.sales_platform(tx)
        policy,r=self.policy(tx,'purchase',platform=platform)
        if not r.local_authority or not r.inventory_managed or not r.coupons_managed: raise BusinessError('insufficient_data','下单、库存和优惠券管理尚未接通')
        for field in ('recipient_name','phone','detail'):
            self.cipher.open(address[field+'_ciphertext'],'address:'+self.user+':'+field,address['key_version'])
        skus=[self.sku(tx,i['sku_id']) for i in pricing['items']]
        # Available stock is checked again at submission, but unrelated stock changes do not invalidate the quote.
        sku_terms=[{k:v for k,v in s.items() if k!='inventory'} for s in skus]
        coupon_terms=[]
        for value in args.get('coupon_refs',[]):
            t,i,scopes=self.coupon(tx,value); t,scopes=self.frozen_coupon(tx,t,i,scopes)
            coupon_terms.append({'instance':i,'template':t,'scopes':scopes})
        return {'arguments':args,'platform_code':platform,'merchant_id':skus[0]['merchant_id'],
            'pricing':pricing,'delivery':delivery,'address_digest':digest(json_binary(address)),
            'sku_terms':sku_terms,'coupon_terms':coupon_terms,'purchase_policy':policy,
            'payable_amount':money(pricing['goods_after_discount'])+money(delivery['shipping_amount']),
            'payment_timeout_minutes':r.payment_timeout_minutes,'quote_ttl_seconds':r.quote_ttl_seconds}

    def quote_checkout(self,args):
        # A quote reserves nothing. Capture a consistent read, then persist it;
        # confirmation performs all current reads under the stock/coupon locks.
        with self.store.transaction() as tx:
            snapshot=self.checkout_snapshot(tx,args)
        with self.store.transaction(write=True) as tx:
            now=utcnow(); qid=uuid4().hex
            expires=now+timedelta(seconds=snapshot['quote_ttl_seconds'])
            tx.execute('INSERT INTO checkout_quotes(quote_id,user_id,platform_code,order_id,snapshot,snapshot_digest,status,created_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (qid,self.user,snapshot['platform_code'],uuid4().hex,dumps(snapshot),digest(snapshot),'active',now,expires))
            return ok({'quote_ref':ref('quote',qid),'currency':snapshot['pricing']['currency'],'goods_amount':snapshot['pricing']['goods_amount'],
                'goods_after_discount':snapshot['pricing']['goods_after_discount'],'shipping_amount':snapshot['delivery']['shipping_amount'],
                'payable_amount':snapshot['payable_amount'],'items':snapshot['pricing']['items'],'expires_at':expires})

    def owned_quote(self,tx,value,lock=False):
        _,qid=unref(value,{'quote'})
        row=tx.one('SELECT * FROM checkout_quotes WHERE quote_id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),(qid,self.user))
        if not row: raise BusinessError('not_found','报价不存在')
        snapshot=decoded(row['snapshot']); self.scope_check({'platform_code':row['platform_code'],'merchant_id':snapshot['merchant_id']})
        return row,snapshot
