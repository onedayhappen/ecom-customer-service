"""Resource queries for the 48-tool contract; projections contain recorded facts only."""
from decimal import Decimal
from app.services.commerce.common import BusinessError, at, decoded, money, ok, ref, unref, utcnow
from app.services.commerce.reads import ReadService, masked, state, ORDER_STATES, SALE_STATES
from app.services.commerce.rules import RuleService


def totals(rows):
    result = {}
    for row in rows:
        currency = row['currency']
        bucket = result.setdefault(currency, {})
        status = row['status']
        bucket[status] = bucket.get(status, Decimal(0)) + money(row['amount'])
    return result


def filter_specs(rows, specs):
    known = {k for r in rows for k in r.get('spec_values', {})}
    applied = [s for s in specs if s['name'] in known]
    unknown = [s for s in specs if s['name'] not in known]
    return ([r for r in rows if all(str(r['spec_values'].get(s['name'])) == s['value'] for s in applied)], unknown)


class QueryService:
    def replacement_shipments(self,tx,order):
        rows=tx.all('''SELECT w.details,w.after_sale_id FROM service_work_orders w
            JOIN after_sale_requests a ON a.after_sale_id=w.after_sale_id
            WHERE a.platform_code=%s AND a.order_id=%s AND w.kind='exchange' ''',(order['platform_code'],order['order_id']))
        found = {details['replacement_shipment_id']:dict(details,after_sale_id=row['after_sale_id'])
            for row in rows if (details:=decoded(row['details'])).get('replacement_shipment_id')}
        for ship in self.order_rows(tx,'shipments',order):
            if ship.get('shipment_kind')=='exchange':
                found[ship['shipment_id']]=dict(found.get(ship['shipment_id'],{}),after_sale_id=ship['after_sale_id'])
        return found

    def get_my_account(self, tx, **args):
        result = self.get_my_profile(tx)
        platforms = self.list_platform_accounts(tx)
        result['data']['platform_accounts'] = platforms['data']
        result['limitations'].extend(platforms['limitations'])
        return result

    def list_addresses(self, tx, **args):
        rows = tx.all('''SELECT a.address_id,a.province_code,a.city_code,a.district_code,
            a.is_default,a.updated_at,l.label,l.province_name,l.city_name,l.district_name
            FROM user_addresses a LEFT JOIN address_labels l ON l.address_id=a.address_id
            WHERE a.user_id=%s AND a.deleted_at IS NULL ORDER BY a.is_default DESC,a.address_id''', (self.user,))
        query = (args.get('query') or '').casefold()
        fields = ('label','province_name','city_name','district_name','province_code','city_code','district_code')
        rows = [dict(r,address_id=str(r['address_id']),recipient='***',phone='***',detail='***') for r in rows
                if not query or any(query in str(r.get(k) or '').casefold() for k in fields)]
        return self.page(rows,'list_addresses',args,size=10)

    def search_products(self, tx, **args):
        q = args['query'].casefold()
        rows = tx.all('SELECT product_id,merchant_id,name,category,description FROM products ORDER BY product_id')
        found = []
        for row in rows:
            if getattr(self.scope,'store_id',None) and row['merchant_id'] != self.scope.store_id: continue
            matched_fields = [k for k in ('name','category','description') if q in str(row.get(k) or '').casefold()]
            if not matched_fields: continue
            skus = [self.sku(tx,r['sku_id']) for r in tx.all('SELECT sku_id FROM product_skus WHERE product_id=%s ORDER BY sku_id',(row['product_id'],))]
            matched, unknown = filter_specs(skus,args.get('specs',[]))
            cap = args.get('max_price')
            matched = [s for s in matched if s['status']=='active' and (not cap or s['currency']==cap['currency'] and money(s['list_price'])<=money(cap['amount']))]
            if not matched: continue
            found.append(dict(row,product_ref=ref('product',row['product_id']),subject_ref=ref('product',row['product_id']),
                matched_fields=matched_fields,sku_summary=matched[:5],sku_count=len(matched),skus_truncated=len(matched)>5,unapplied_conditions=unknown))
        return self.page(found,'search_products',args,size=10)

    def get_product_details(self, tx, **args):
        kind, ident = unref(args['target_ref'],{'product','sku'})
        if kind=='sku':
            sku=self.sku(tx,ident)
            matched,unknown=filter_specs([sku],args.get('specs',[]))
            return ok({'product':self.public_product(tx,sku['product_id']),'items':matched,'total':len(matched),'unapplied_conditions':unknown})
        product=self.public_product(tx,ident)
        rows=[self.sku(tx,r['sku_id']) for r in tx.all('SELECT sku_id FROM product_skus WHERE product_id=%s ORDER BY sku_id',(ident,))]
        matched,unknown=filter_specs(rows,args.get('specs',[]))
        return self.page(matched,'get_product_details',args,data={'product':dict(product,product_ref=args['target_ref']),'unapplied_conditions':unknown})

    def find_orders(self, tx, **args):
        # Reuse the ownership and date predicates, then add bounded purchase labels.
        result=ReadService.find_orders(self,tx,**dict(args,keyword=args.get('query',args.get('keyword'))))
        for row in result['data']['items']:
            items=self.order_rows(tx,'order_items',row)
            row['purchase_summary']=[{'name':i['title_snapshot'],'specs':decoded(i['spec_snapshot']),'quantity':i['quantity']} for i in items[:3]]
            row['item_count']=len(items)
        return result

    def get_order(self, tx, **args):
        result=ReadService.get_order(self,tx,**args)
        order=self.owned_order(tx,args['order_ref'])
        items=self.order_rows(tx,'order_items',order)
        progress=self.get_order_items(tx,order_ref=args['order_ref'])['data']['items']
        result['data'].update(items=progress[:5],item_count=len(items),items_truncated=len(items)>5)
        for table,key,kind in [('payment_transactions','payment_id','payment'),('shipments','shipment_id','shipment'),('after_sale_requests','after_sale_id','after_sale'),('invoice_requests','invoice_id','invoice')]:
            rows=self.order_rows(tx,table,order)
            result['data']['related'][table]={'count':len(rows),'truncated':len(rows)>5,'items':[{'id':r[key],'target_ref':ref(kind,r[key]),'status':r['status']} for r in rows[:5]]}
        return result

    def get_order_items(self, tx, **args):
        result=ReadService.get_order_items(self,tx,**args)
        order=self.owned_order(tx,args['order_ref'])
        sales=self.order_rows(tx,'after_sale_requests',order)
        ships=self.order_rows(tx,'shipments',order)
        replacements=self.replacement_shipments(tx,order)
        for item in result['data']['items']:
            shipped=0; replaced=0; delivered=0; occupied=0; unknown=False; item_sales=[]
            for ship in ships:
                line=tx.one('SELECT quantity FROM shipment_items WHERE shipment_id=%s AND order_item_id=%s',(ship['shipment_id'],item['order_item_id']))
                if line and ship['shipped_at'] and ship['status'] in {'shipped','in_transit','delivered'}:
                    if ship['shipment_id'] in replacements: replaced+=line['quantity']
                    else:
                        shipped+=line['quantity']
                        if ship['status']=='delivered' and ship['delivered_at']: delivered+=line['quantity']
            for sale in sales:
                line=tx.one('SELECT quantity FROM after_sale_items WHERE after_sale_id=%s AND order_item_id=%s',(sale['after_sale_id'],item['order_item_id']))
                if line:
                    item_sales.append({k:sale.get(k) for k in ('after_sale_id','kind','status','stage','completed_at') } | {'quantity':line['quantity']})
                if sale['kind']=='price_protection' or sale['status'] in {'cancelled','rejected','closed'}: continue
                if sale['status'] not in SALE_STATES: unknown=True
                if line: occupied+=line['quantity']
            item.update(recorded_shipped=shipped,recorded_replacement_shipped=replaced,after_sale_occupied=None if unknown else occupied)
            base=self.order_base_status(order)
            fulfillment=('received' if delivered>=item['quantity'] else 'partially_received' if delivered else
                         'pending_receipt' if shipped>=item['quantity'] else 'partially_shipped' if shipped else
                         base if base in {'pending_payment','cancelled','closed'} else
                         'paid' if order.get('paid_amount') and base in {'paid','pending_receipt'} else 'unknown')
            active=any(s['status'] in {'pending_review','approved','processing'} for s in item_sales)
            item.update(fulfillment_status=fulfillment,recorded_delivered=delivered,
                        unshipped_quantity=max(0,item['quantity']-shipped),after_sales=item_sales,
                        after_sale_status='processing' if active else 'completed' if any(s['status']=='completed' for s in item_sales) else 'closed' if item_sales else 'none')
        return result

    def get_payments(self,tx,**args):
        result=ReadService.get_payments(self,tx,**args)
        data=result['data']; currency=data['order_currency']; paid=data['order_paid_amount']
        data['recorded_difference']=data['recorded_success_amounts'].get(currency,Decimal(0))-money(paid) if paid is not None and currency else None
        return result

    def get_logistics(self,tx,**args):
        parsed=unref(args['target_ref'],{'order','shipment'})
        if parsed[0]=='shipment':
            return self.get_shipment_tracking(tx,shipment_id=parsed[1],cursor=args.get('cursor'))
        order=self.owned_order(tx,args['target_ref']); rows=[]
        bought={r['order_item_id']:r['quantity'] for r in self.order_rows(tx,'order_items',order)}
        sent={k:0 for k in bought}; replaced={k:0 for k in bought}; replacements=self.replacement_shipments(tx,order)
        for ship in sorted(self.order_rows(tx,'shipments',order),key=lambda r:r['shipment_id']):
            items=tx.all('SELECT * FROM shipment_items WHERE shipment_id=%s ORDER BY order_item_id',(ship['shipment_id'],))
            for item in items:
                if item['order_item_id'] not in bought: raise BusinessError('insufficient_data','包裹明细归属不一致')
                if ship['shipped_at'] and ship['status'] in {'shipped','in_transit','delivered'}:
                    quantities=replaced if ship['shipment_id'] in replacements else sent
                    quantities[item['order_item_id']]+=item['quantity']
            events=tx.all('SELECT * FROM shipment_events WHERE shipment_id=%s ORDER BY event_time DESC,event_id DESC',(ship['shipment_id'],))
            rows.append(dict(ship,target_ref=ref('shipment',ship['shipment_id']),replacement=replacements.get(ship['shipment_id']),items=items,events=events[:3],events_total=len(events),events_truncated=len(events)>3,estimated_delivery=None))
        return self.page(rows,'get_logistics',args,size=5,data={'quantities':[dict(order_item_id=k,purchased=v,recorded_shipped=sent[k],recorded_replacement_shipped=replaced[k]) for k,v in bought.items()]},limitations=['未记录预计送达时间；发出数量不代表签收数量'])

    def find_my_coupons(self,tx,**args):
        subject=self.subject(tx,args.get('subject_ref'),allow_order=False) if args.get('subject_ref') else None
        rows=tx.all('SELECT * FROM user_coupons WHERE user_id=%s ORDER BY expires_at,user_coupon_id',(self.user,)); found=[]
        q=(args.get('query') or '').casefold()
        for row in rows:
            value=ref('coupon_instance',row['user_coupon_id'])
            try: t,i,scopes=self.coupon(tx,value)
            except BusinessError as e:
                if e.code=='not_found': continue
                raise
            t,scopes=self.frozen_coupon(tx,t,i,scopes)
            if subject and not self.eligible_lines(scopes,[subject]): continue
            merchant=tx.one('SELECT name FROM merchants WHERE merchant_id=%s',(t['merchant_id'],)) if t['merchant_id'] else None
            if q and not any(q in str(s or '').casefold() for s in [t['name'],(merchant or {}).get('name')]): continue
            item=masked(self.coupon_state(row))
            if args.get('status','all')!='all' and item['status']!=args['status']: continue
            found.append(dict(item,template=t,rules_basis='claim_snapshot'))
        return self.page(found,'find_my_coupons',args,size=10)

    def check_coupon(self,tx,**args):
        target=args.get('target') or {'order_ref':args.get('order_ref')}
        def evaluate():
            if target.get('order_ref'):
                order=self.owned_order(tx,target['order_ref'])
                if order['status']!='pending_payment': raise BusinessError('not_eligible','该订单不能补用优惠券')
                policy,_=self.policy(tx,'pricing',order)
                lines=[dict(r,amount=r['payable_amount'],merchant_id=order['merchant_id']) for r in self.order_rows(tx,'order_items',order)]
                platform,currency=order['platform_code'],order['currency']
            else:
                estimate=RuleService.estimate_purchase(self,tx,items=target['items'],coupon_refs=[])['data']
                lines=estimate['items']; platform=self.sales_platform(tx); currency=estimate['currency']
                policy={'policy_id':estimate['policy_id'],'version':estimate['policy_version']}
            template,instance,scopes=self.coupon(tx,args['coupon_ref'])
            if not instance: raise BusinessError('invalid_arguments','必须使用本人持券')
            template,scopes=self.frozen_coupon(tx,template,instance,scopes)
            from app.data.membership_store import check_coupon_membership_use
            check_coupon_membership_use(tx,self.user,template)
            matches=self.eligible_lines(scopes,lines)
            if template['merchant_id']: matches=[x for x in matches if x['merchant_id']==template['merchant_id']]
            base=sum((money(x['amount']) for x in matches),Decimal(0)); minimum=money(template['threshold_amount'])
            values=[('instance_status',self.coupon_state(instance)['status'],'available'),
                ('platform',platform,template['platform_code']),('currency',currency,template['currency']),
                ('scope',bool(matches),True),('threshold',base,minimum)]
            checks=[dict(rule=name,status='pass' if (actual>=expected if name=='threshold' else actual==expected) else 'fail',
                actual=actual,expected=expected,source='coupon_claim_rules',user_coupon_id=instance['user_coupon_id']) for name,actual,expected in values]
            passed=all(x['status']=='pass' for x in checks)
            return dict(eligibility='eligible' if passed else 'ineligible',checks=checks,eligible_amount=base,
                discount_amount=self.discount(template,base) if passed else None,threshold_gap=max(minimum-base,Decimal(0)),
                policy_id=policy['policy_id'],policy_version=policy['version'],rules_basis='claim_snapshot')
        result=self.decision(evaluate)
        result['data'].setdefault('checks',[{'rule':'prerequisites','status':'unknown' if result['data']['eligibility']=='unknown' else 'fail','reasons':result['data'].get('reasons',[])}])
        result['data']['combination_checked']=False
        return result

    def find_after_sales(self,tx,**args):
        if args.get('order_ref'): self.owned_order(tx,args['order_ref'])
        rows=tx.all('SELECT * FROM after_sale_requests WHERE user_id=%s ORDER BY created_at DESC,after_sale_id',(self.user,)); found=[]
        q=(args.get('query') or '').casefold(); interval=args.get('created_range')
        for row in rows:
            value=ref('order',row['platform_code'],row['order_id'])
            try: order=self.owned_order(tx,value)
            except BusinessError as e:
                if e.code=='not_found': continue
                raise
            if args.get('order_ref') and value!=args['order_ref']: continue
            if interval and not at(interval['from'])<=at(row['created_at'])<at(interval['to']): continue
            items=tx.all('''SELECT i.title_snapshot,i.spec_snapshot,s.quantity,s.order_item_id
                FROM after_sale_items s JOIN order_items i ON i.order_item_id=s.order_item_id
                WHERE s.after_sale_id=%s ORDER BY s.order_item_id''',(row['after_sale_id'],))
            if q and not any(q in str(v).casefold() for v in [row['after_sale_id'],row['order_id'],*[i['title_snapshot'] for i in items]]): continue
            item=state(masked(row),SALE_STATES)
            if args.get('status','all')!='all' and item['status']!=args['status']: continue
            found.append(dict(item,order_ref=value,target_ref=ref('after_sale',row['after_sale_id']),items=items[:5],item_count=len(items),currency=order['currency']))
        return self.page(found,'find_after_sales',args,size=10)

    def refund_rows(self,tx,target_ref):
        target=unref(target_ref,{'order','after_sale'})
        if target[0]=='after_sale':
            sale,order=self.sale(tx,target[1]); ids=[sale['after_sale_id']]
        else:
            order=self.owned_order(tx,target_ref); ids=[s['after_sale_id'] for s in self.order_rows(tx,'after_sale_requests',order)]
        rows=[]
        for aid in ids: rows.extend(tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s ORDER BY requested_at,refund_id',(aid,)))
        payments={p['payment_id'] for p in self.order_rows(tx,'payment_transactions',order)}
        if any(r['payment_id'] and r['payment_id'] not in payments for r in rows): raise BusinessError('insufficient_data','退款原支付关联无法核实')
        return sorted(rows,key=lambda r:(r['requested_at'],r['refund_id'])),order

    def get_refunds(self,tx,**args):
        target=args.get('target_ref') or ref('after_sale',args['after_sale_id'])
        rows,order=self.refund_rows(tx,target)
        return self.page(rows,'get_refunds',args,data={'amounts_by_currency_and_status':totals(rows),'recorded_success_amount':sum((money(r['amount']) for r in rows if r['status']=='succeeded' and r['currency']==order['currency']),Decimal(0))},limitations=['渠道成功不代表已核实银行卡入账'])

    def get_after_sale_status(self,tx,**args):
        sale,order=self.sale(tx,args['after_sale_id'])
        items=tx.all('SELECT * FROM after_sale_items WHERE after_sale_id=%s ORDER BY order_item_id',(sale['after_sale_id'],))
        allowed={i['order_item_id'] for i in self.order_rows(tx,'order_items',order)}
        if any(i['order_item_id'] not in allowed for i in items): raise BusinessError('insufficient_data','售后明细归属不一致')
        refunds,_=self.refund_rows(tx,ref('after_sale',sale['after_sale_id']))
        work=tx.one('SELECT * FROM service_work_orders WHERE after_sale_id=%s',(sale['after_sale_id'],))
        if work: work=dict(work,details=decoded(work['details']))
        auth=tx.one('SELECT authorization_id,status,deadline,source,updated_at FROM return_authorizations WHERE after_sale_id=%s',(sale['after_sale_id'],))
        ships=tx.all('SELECT * FROM return_shipments WHERE authorization_id=%s ORDER BY created_at,return_shipment_id',(auth['authorization_id'],)) if auth else []
        for ship in ships:
            ship['items']=tx.all('SELECT order_item_id,quantity FROM return_shipment_items WHERE return_shipment_id=%s',(ship['return_shipment_id'],))
            ship['events']=tx.all('SELECT * FROM return_shipment_events WHERE return_shipment_id=%s ORDER BY event_time,event_id',(ship['return_shipment_id'],))
        events=tx.all('SELECT * FROM after_sale_events WHERE after_sale_id=%s ORDER BY created_at,event_id',(sale['after_sale_id'],))
        reverse_events=sorted([event for ship in ships for event in ship['events']],key=lambda e:(e['event_time'],e['event_id']))
        replacements=tx.all("SELECT * FROM shipments WHERE after_sale_id=%s AND shipment_kind='exchange' ORDER BY created_at,shipment_id",(sale['after_sale_id'],))
        for ship in replacements:
            ship['items']=[dict(i,actual_spec_snapshot=decoded(i['actual_spec_snapshot'])) for i in tx.all('SELECT * FROM shipment_items WHERE shipment_id=%s',(ship['shipment_id'],))]
            ship['events']=tx.all('SELECT * FROM shipment_events WHERE shipment_id=%s ORDER BY event_time,event_id',(ship['shipment_id'],))
        return self.page(events,'get_after_sale_status',args,data={'after_sale':state(masked(sale),SALE_STATES),'order_ref':ref('order',order['platform_code'],order['order_id']),
            'purchase_items':items[:20],'item_count':len(items),'refund_summary':totals(refunds),'work_order':work,'return_authorization':auth,
            'return_shipment':ships[0] if len(ships)==1 else None,'return_shipments':ships,'replacement_shipments':replacements,
            'return_events':reverse_events[-3:],'return_events_total':len(reverse_events),'processes_connected':{'return':bool(auth),'service':bool(work)}})

    def get_invoices(self,tx,**args):
        target=unref(args['target_ref'],{'order','invoice'}) if args.get('target_ref') else None
        if target and target[0]=='order': self.owned_order(tx,args['target_ref'])
        rows=tx.all('SELECT invoice_id,platform_code,order_id,invoice_type,status,created_at,issued_at FROM invoice_requests WHERE user_id=%s ORDER BY created_at DESC,invoice_id',(self.user,)); found=[]
        for row in rows:
            order_ref=ref('order',row['platform_code'],row['order_id'])
            try: self.owned_order(tx,order_ref)
            except BusinessError as e:
                if e.code=='not_found': continue
                raise
            if target and (target[0]=='order' and args['target_ref']!=order_ref or target[0]=='invoice' and target[1]!=row['invoice_id']): continue
            issued=tx.all('SELECT * FROM issued_invoices WHERE invoice_id=%s ORDER BY issued_at,issued_invoice_id',(row['invoice_id'],))
            from app.services.commerce.providers import invoice_file
            documents=[]
            for invoice in issued:
                available=False
                if invoice['status']=='issued' and invoice['document_key']:
                    try: invoice_file(invoice); available=True
                    except BusinessError: pass
                documents.append({**{k:v for k,v in invoice.items() if k!='document_key'},'document_available':available})
            found.append(dict(row,target_ref=ref('invoice',row['invoice_id']),title='***',order_ref=order_ref,issued_invoices=documents))
        if target and target[0]=='invoice' and not found: raise BusinessError('not_found','开票申请不存在')
        return self.page(found,'get_invoices',args)
