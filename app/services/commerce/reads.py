# 文件说明：读取当前客户拥有的订单、支付、物流及关联业务记录。
"""Customer-owned reads over the normalized business schema."""
from datetime import timedelta
from decimal import Decimal
from app.services.commerce.common import BusinessError,at,decoded,money,ok,ref,utcnow

ORDER_STATES={'pending_payment','paid','pending_receipt','received','after_sale','cancelled','closed'}
SALE_STATES={'pending_review','approved','rejected','processing','completed','cancelled','closed'}

def state(row,valid):
    value=row.get('status')
    return {**row,'status':value if value in valid else 'unknown','source_status':value}

def masked(row):
    return {k:v for k,v in row.items() if not k.endswith('_ciphertext') and k not in {'lookup_digest','key_version','user_id'}}

class ReadService:
    def get_my_profile(self,tx,**args):
        user=tx.one('SELECT user_id,username,membership_platform_code,growth_level_code,vip_plan_code,vip_starts_at,vip_expires_at,membership_updated_at FROM users WHERE user_id=%s',(self.user,))
        if not user: raise BusinessError('not_found','用户不存在')
        profile=tx.one('SELECT display_name,avatar_url,updated_at FROM user_profiles WHERE user_id=%s',(self.user,))
        contacts=tx.all('SELECT contact_type,verified_at FROM user_contacts WHERE user_id=%s ORDER BY contact_id',(self.user,))
        if user['membership_platform_code']:
            from app.data.membership_store import membership_facts
            platform=getattr(self.scope,'platform_code',None) or user['membership_platform_code']
            facts=membership_facts(tx,self.user,platform,store_id=getattr(self.scope,'store_id',None))
            sub=facts['vip']['subscription']; growth=facts['growth_membership']
            user.update(membership_platform_code=platform,growth_level_code=growth['level_code'] if growth else None,
                vip_plan_code=sub['plan_code'] if sub else None,vip_starts_at=sub['starts_at'] if sub else None,
                vip_expires_at=sub['ends_at'] if sub else None,vip_status=facts['vip']['status'])
        return ok({'username':user['username'],'membership_summary':{k:v for k,v in user.items() if k not in {'user_id','username'}},'membership_summary_authoritative':False,'profile':profile,'contacts':[dict(r,value='***') for r in contacts[:20]],'contacts_total':len(contacts)},limitations=['联系方式仅展示验证状态']+(['联系方式超过摘要上限'] if len(contacts)>20 else []))
    def list_platform_accounts(self,tx,**args):
        rows=tx.all('SELECT platform_code,status,verified_at FROM user_platform_accounts WHERE user_id=%s ORDER BY platform_code,id',(self.user,))
        return ok({'items':rows[:20],'total':len(rows)},limitations=['平台账号内容已隐藏']+(['超过摘要上限，请在账号页面查看'] if len(rows)>20 else []))
    def get_product(self,tx,**args):
        p=self.public_product(tx,args['product_id'])
        rows=tx.all('SELECT sku_id,sku_code,spec_values,list_price,currency,status FROM product_skus WHERE product_id=%s ORDER BY sku_id',(p['product_id'],))
        rows=[dict(r,spec_values=decoded(r['spec_values'],{}),subject_ref=ref('sku',r['sku_id'])) for r in rows]
        return self.page(rows,'get_product',args,data={'product':dict(p,subject_ref=ref('product',p['product_id']))})
    def get_sku(self,tx,**args):
        row=self.sku(tx,args['sku_id'])
        return ok(row,limitations=[] if row['inventory'] else ['库存记录缺失，不能认定缺货'])
    def compare_skus(self,tx,**args):
        return ok({'items':[self.sku(tx,s) for s in args['sku_ids']]},limitations=['仅比较已有商品事实，不代表质量排名或最终到手价'])
    def get_price_history(self,tx,**args):
        self.sku(tx,args['sku_id']); end=utcnow(); start=end-timedelta(days=args.get('days',30))
        rows=tx.all('SELECT * FROM sku_price_history WHERE sku_id=%s AND effective_from<%s AND (effective_until IS NULL OR effective_until>%s) ORDER BY effective_from,price_id',(args['sku_id'],end,start))
        minima={}
        for r in rows:
            k=r['price_type']+':'+r['currency']; minima[k]=min(minima.get(k,money(r['amount'])),money(r['amount']))
        return self.page(rows,'get_price_history',args,data={'recorded_minimum':minima,'window_start':start,'window_end':end},limitations=['仅本地已记录价格；不能证明窗口内无采集缺口'])
    def get_merchant(self,tx,**args):
        self.subject(tx,ref('merchant',args['merchant_id']))
        return ok(tx.one('SELECT merchant_id,name,category,description,status,store_type FROM merchants WHERE merchant_id=%s',(args['merchant_id'],)))
    def find_orders(self,tx,**args):
        clauses=['o.user_id=%s']; values=[self.user]
        for k,f in [('platform_code','platform_code'),('store_id','merchant_id')]:
            if getattr(self.scope,k,None): clauses.append('o.'+f+'=%s');values.append(getattr(self.scope,k))
        if args.get('keyword'):
            q='%'+args['keyword'].replace('!','!!').replace('%','!%').replace('_','!_')+'%'
            clauses.append("(o.order_id LIKE %s ESCAPE '!' OR EXISTS (SELECT 1 FROM order_items i WHERE i.platform_code=o.platform_code AND i.order_id=o.order_id AND i.title_snapshot LIKE %s ESCAPE '!'))"); values += [q,q]
        if args.get('created_range'):
            clauses += ['o.created_at>=%s','o.created_at<%s']; values += [at(args['created_range']['from']),at(args['created_range']['to'])]
        rows=tx.all('SELECT o.* FROM orders o WHERE '+' AND '.join(clauses)+' ORDER BY o.created_at DESC,o.platform_code,o.order_id',tuple(values))
        rows=[dict(state(masked(r),ORDER_STATES),order_ref=ref('order',r['platform_code'],r['order_id'])) for r in rows]
        if args.get('status','all')!='all': rows=[r for r in rows if r['status']==args['status']]
        return self.page(rows,'find_orders',args,size=10)
    def get_order(self,tx,**args):
        o=self.owned_order(tx,args['order_ref']); address=tx.one('SELECT province_code,city_code,district_code,captured_at FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s',(o['platform_code'],o['order_id']))
        related={}
        for table,key,kind in [('payment_transactions','payment_id','payment'),('shipments','shipment_id','shipment'),('after_sale_requests','after_sale_id','after_sale'),('invoice_requests','invoice_id','invoice')]:
            params=(o['platform_code'],o['order_id'])
            where=' FROM '+table+' WHERE platform_code=%s AND order_id=%s'
            count=tx.one('SELECT COUNT(*) n'+where,params)['n']
            rows=tx.all('SELECT '+key+',status'+where+' ORDER BY '+key+' LIMIT 5',params)
            related[table]={'count':count,'truncated':count>5,'items':[{'id':r[key],'target_ref':ref(kind,r[key]),'status':r['status']} for r in rows]}
        return ok(dict(state(masked(o),ORDER_STATES),order_ref=args['order_ref'],address=address,related=related))
    def get_order_items(self,tx,**args):
        o=self.owned_order(tx,args['order_ref']); rows=self.order_rows(tx,'order_items',o)
        return self.page([dict(r,spec_snapshot=decoded(r['spec_snapshot'])) for r in sorted(rows,key=lambda x:x['order_item_id'])],'get_order_items',args)
    def get_payments(self,tx,**args):
        o=self.owned_order(tx,args['order_ref']); rows=sorted(self.order_rows(tx,'payment_transactions',o),key=lambda x:str(x['created_at'])+x['payment_id'])
        totals={}
        for r in rows:
            if r['status']=='succeeded': totals[r['currency']]=totals.get(r['currency'],Decimal(0))+money(r['amount'])
        return self.page([masked(r) for r in rows],'get_payments',args,data={'recorded_success_amounts':totals,'order_paid_amount':o.get('paid_amount'),'order_currency':o.get('currency')},limitations=['仅已记录渠道流水，不推断银行卡余额或未同步扣款'])
    def get_shipments(self,tx,**args):
        o=self.owned_order(tx,args['order_ref']); bought={r['order_item_id']:r['quantity'] for r in self.order_rows(tx,'order_items',o)}; sent={k:0 for k in bought}; rows=[]
        for ship in sorted(self.order_rows(tx,'shipments',o),key=lambda x:x['shipment_id']):
            items=tx.all('SELECT * FROM shipment_items WHERE shipment_id=%s ORDER BY order_item_id',(ship['shipment_id'],))
            for item in items:
                if item['order_item_id'] not in bought: raise BusinessError('insufficient_data','包裹明细归属不一致')
                if ship['status'] in {'shipped','in_transit','delivered'} and ship['shipped_at']:
                    sent[item['order_item_id']]+=item['quantity']
            for item in items or [None]: rows.append(dict(ship,item=item))
        counts=[{'order_item_id':k,'purchased':v,'recorded_shipped':sent[k],'without_dispatch_record':max(v-sent[k],0),'remaining_fulfillment':None} for k,v in bought.items()]
        return self.page(rows,'get_shipments',args,data={'quantities':counts},limitations=['待履约数量还需要取消及售后分摊依据']+(['发出数量超出购买数量，需核实重发或数据冲突'] if any(sent[k]>v for k,v in bought.items()) else []))
    def get_shipment_tracking(self,tx,**args):
        ship=tx.one('SELECT * FROM shipments WHERE shipment_id=%s',(args['shipment_id'],))
        if not ship: raise BusinessError('not_found','包裹不存在')
        self.owned_order(tx,ref('order',ship['platform_code'],ship['order_id']))
        return self.sql_page(tx,'SELECT * FROM shipment_events WHERE shipment_id=%s',[args['shipment_id']],
            'get_shipment_tracking',args,keys=('event_time','event_id'),data={'shipment':ship},limitations=['没有预计到达依据时不预测送达日期'])
    def list_after_sales(self,tx,**args):
        if args.get('order_ref'): self.owned_order(tx,args['order_ref'])
        rows=tx.all('SELECT * FROM after_sale_requests WHERE user_id=%s ORDER BY created_at DESC,after_sale_id',(self.user,)); found=[]
        for r in rows:
            try: self.owned_order(tx,ref('order',r['platform_code'],r['order_id']))
            except BusinessError: continue
            if args.get('order_ref') and args['order_ref']!=ref('order',r['platform_code'],r['order_id']): continue
            r=state(masked(r),SALE_STATES)
            if args.get('status','all')!='all' and r['status']!=args['status']: continue
            found.append(r)
        return self.page(found,'list_after_sales',args)
    def get_after_sale(self,tx,**args):
        sale,o=self.sale(tx,args['after_sale_id'])
        rows=tx.all('SELECT * FROM after_sale_items WHERE after_sale_id=%s ORDER BY order_item_id',(sale['after_sale_id'],))
        ids={i['order_item_id'] for i in self.order_rows(tx,'order_items',o)}
        if any(r['order_item_id'] not in ids for r in rows): raise BusinessError('insufficient_data','售后明细归属不一致')
        refunds=tx.all('SELECT refund_id,status,amount,currency FROM refund_transactions WHERE after_sale_id=%s ORDER BY refund_id',(sale['after_sale_id'],))
        return self.page(rows,'get_after_sale',args,data={'after_sale':state(masked(sale),SALE_STATES),'currency':o['currency'],'refunds':{'count':len(refunds),'references':[r['refund_id'] for r in refunds[:20]]}},limitations=['审核意见、退货授权地址和验货过程尚未接入'])
    def list_invoice_requests(self,tx,**args):
        if args.get('order_ref'): self.owned_order(tx,args['order_ref'])
        rows=tx.all('SELECT invoice_id,platform_code,order_id,user_id,invoice_type,status,created_at,issued_at FROM invoice_requests WHERE user_id=%s ORDER BY created_at DESC,invoice_id',(self.user,)); result=[]
        for r in rows:
            try: self.owned_order(tx,ref('order',r['platform_code'],r['order_id']))
            except BusinessError: continue
            if args.get('order_ref') and args['order_ref']!=ref('order',r['platform_code'],r['order_id']): continue
            result.append(dict(masked(r),title='***'))
        return self.page(result,'list_invoice_requests',args)
    def get_invoice_request(self,tx,**args):
        row=tx.one('SELECT invoice_id,platform_code,order_id,user_id,invoice_type,status,created_at,issued_at FROM invoice_requests WHERE invoice_id=%s AND user_id=%s',(args['invoice_id'],self.user))
        if not row: raise BusinessError('not_found','发票申请不存在')
        self.owned_order(tx,ref('order',row['platform_code'],row['order_id']))
        return ok(dict(masked(row),title='***',tax_number='***'),limitations=['仅开票申请，没有正式票据文件'])
