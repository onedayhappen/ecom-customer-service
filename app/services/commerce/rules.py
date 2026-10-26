"""Deterministic policy evaluation. Unknown rules never imply eligibility."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.services.commerce.common import BusinessError,at,decoded,money,ok,ref,unref,utcnow

class Rules(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    schema_version: Literal[1]
    time_basis: Literal['request','order_created','delivered']='request'
    allowed_statuses: list[str]=Field(default_factory=list,max_length=20)
    window_days: int | None=Field(None,ge=0,le=3650)
    requires_reason_review: bool=False
    refund_shipping: bool=False
    local_authority: bool=False
    inventory_managed: bool=False
    coupons_managed: bool=False
    threshold_basis: Literal['goods_after_promotions']='goods_after_promotions'
    coupon_stacking: bool=False
    promotions_with_coupons: bool=False
    rag_policy_id: str | None=None
    rag_policy_version: str | None=None
    quote_ttl_seconds: int=Field(600,ge=30,le=3600)
    payment_timeout_minutes: int=Field(30,ge=1,le=1440)
    allow_exchange_price_difference: bool=False

class RuleService:
    def item_delivery_times(self,tx,order,items):
        """Original fulfilment only; unrelated lines and replacement parcels cannot extend a window."""
        times={}
        for item in items:
            parcels=tx.all('''SELECT s.shipment_id,s.delivered_at,s.status,i.quantity FROM shipment_items i
                JOIN shipments s ON s.shipment_id=i.shipment_id
                WHERE i.order_item_id=%s AND s.platform_code=%s AND s.order_id=%s
                AND s.shipment_kind='original' AND s.status<>'cancelled' ''',
                (item['order_item_id'],order['platform_code'],order['order_id']))
            if (not parcels or sum(p['quantity'] for p in parcels)!=item['quantity']
                    or any(p['status']!='delivered' or not p['delivered_at'] for p in parcels)):
                raise BusinessError('insufficient_data','申请商品缺少完整的原始包裹签收记录')
            # With no per-unit parcel selection, use the earliest delivery conservatively.
            times[item['order_item_id']]=min(at(p['delivered_at']) for p in parcels)
        return times
    def policy(self,tx,action,order=None,platform=None,items=None):
        platform=platform or (order or {}).get('platform_code') or self.sales_platform(tx)
        if not platform: raise BusinessError('insufficient_data','销售平台尚未核实')
        rows=tx.all("SELECT * FROM business_policy_versions WHERE action=%s AND platform_code=%s AND status='active' ORDER BY version DESC"+(' FOR UPDATE' if getattr(tx,'write',False) else ''),(action,platform))
        eligible=[]
        for row in rows:
            if row['channel'] is not None: continue  # No trusted channel exists in this harness yet.
            try: rule=Rules.model_validate(decoded(row['rules']))
            except (ValidationError,ValueError): raise BusinessError('insufficient_data','业务规则格式未识别') from None
            instant=utcnow()
            if rule.time_basis=='order_created': instant=at((order or {}).get('created_at'))
            if rule.time_basis=='delivered':
                if items:
                    instants=list(self.item_delivery_times(tx,order,items).values())
                    if not all(at(row['effective_from'])<=v and (not row['effective_until'] or v<at(row['effective_until'])) for v in instants):
                        continue
                    instant=instants[0]
                else:
                    ships=[s for s in self.order_rows(tx,'shipments',order) if s.get('shipment_kind')=='original' and s['status']!='cancelled'] if order else []
                    instant=max(at(x['delivered_at']) for x in ships) if ships and all(x['delivered_at'] for x in ships) else None
            if instant is None: raise BusinessError('insufficient_data','政策适用时间缺失')
            if at(row['effective_from'])<=instant and (not row['effective_until'] or instant<at(row['effective_until'])):
                eligible.append((row,rule))
        if len(eligible)!=1: raise BusinessError('insufficient_data','没有唯一有效的业务规则')
        return eligible[0]
    def decision(self, operation):
        try: return ok(operation())
        except BusinessError as e:
            if e.code not in {'insufficient_data','not_eligible'}: raise
            return ok({'eligibility':'ineligible' if e.code=='not_eligible' else 'unknown','reasons':[e.message],'amount':None,**e.details},limitations=[e.message] if e.code=='insufficient_data' else [])
    def assess_order(self,tx,order,action):
        p,r=self.policy(tx,action,order)
        if order['status'] not in r.allowed_statuses and self.order_base_status(order) not in r.allowed_statuses: raise BusinessError('not_eligible','订单状态不满足规则')
        ships=self.order_rows(tx,'shipments',order)
        if action in {'cancel_order','change_address'}:
            if not r.local_authority: raise BusinessError('insufficient_data','未接通该订单的履约控制接口')
        if action=='change_address':
            self.check_order_editable(tx,order,address=True)
        if action=='cancel_order':
            facts=self.cancellation_facts(tx,order)
            if facts['mode']=='unpaid' and (not r.inventory_managed or not r.coupons_managed): raise BusinessError('insufficient_data','库存与券占用尚未纳入本地事务管理')
        if action=='invoice':
            if money(order['paid_amount'])<=0: raise BusinessError('not_eligible','订单没有已核实支付金额')
            if any(x['status'] not in {'cancelled','rejected','closed'} for x in self.order_rows(tx,'invoice_requests',order)): raise BusinessError('not_eligible','订单已有有效开票申请')
        return {'eligibility':'eligible','policy_id':p['policy_id'],'policy_version':p['version'],'action':action}
    def assess_order_action(self,tx,**args):
        o=self.owned_order(tx,args['order_ref'])
        return self.decision(lambda:self.assess_order(tx,o,args['action']))
    def sale_assessment(self,tx,kind,items):
        rows=[]; order=None
        for item in items:
            row=tx.one('SELECT * FROM order_items WHERE order_item_id=%s',(item['order_item_id'],))
            if not row: raise BusinessError('not_found','购买明细不存在')
            owned=self.owned_order(tx,ref('order',row['platform_code'],row['order_id']))
            if order and (owned['platform_code'],owned['order_id'])!=(order['platform_code'],order['order_id']): raise BusinessError('invalid_arguments','售后明细须属于同一订单')
            order=owned; rows.append((row,item['quantity']))
        p,r=self.policy(tx,kind,order,items=[row for row,_ in rows])
        if order['status'] not in r.allowed_statuses and self.order_base_status(order) not in r.allowed_statuses: raise BusinessError('not_eligible','订单状态不支持该售后方式')
        if r.requires_reason_review: raise BusinessError('insufficient_data','该规则需要原因或质检审核，需人工核实')
        if r.window_days is not None:
            times=self.item_delivery_times(tx,order,[row for row,_ in rows])
            if any(utcnow()>t+timedelta(days=r.window_days) for t in times.values()):
                raise BusinessError('not_eligible','申请商品超出售后申请期限；同一商品分包时按最早签收时间校验')
        all_sales=self.order_rows(tx,'after_sale_requests',order)
        if any(x['status'] not in {'pending_review','approved','processing','completed','cancelled','rejected','closed'} for x in all_sales): raise BusinessError('insufficient_data','存在未知售后状态')
        allocations=[]
        for row,qty in rows:
            used=0
            for sale in all_sales:
                if sale['status'] in {'cancelled','rejected','closed'} or sale['kind']=='price_protection': continue
                consumed=tx.one('SELECT quantity FROM after_sale_items WHERE after_sale_id=%s AND order_item_id=%s',(sale['after_sale_id'],row['order_item_id']))
                used += consumed['quantity'] if consumed else 0
            if qty>row['quantity']-used: raise BusinessError('not_eligible','申请数量超过可售后余量')
            # Cumulative allocation makes successive partial refunds sum to exactly the line total.
            total=money(row['payable_amount'])
            amount=(total*(used+qty)/row['quantity']).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)-(total*used/row['quantity']).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            allocations.append({'order_item_id':row['order_item_id'],'name':row['title_snapshot'],'quantity':qty,'amount':amount,'remaining_quantity':row['quantity']-used})
        amount=sum((x['amount'] for x in allocations),Decimal(0))
        shipping=Decimal(0)
        if r.refund_shipping: raise BusinessError('insufficient_data','运费退款需要单独的分摊依据')
        paid=money(order['paid_amount']); reserved=Decimal(0)
        for sale in all_sales:
            refunds=tx.all('SELECT * FROM refund_transactions WHERE after_sale_id=%s',(sale['after_sale_id'],))
            if any(x['currency']!=order['currency'] or x['status'] not in {'pending','processing','succeeded','failed','cancelled'} for x in refunds): raise BusinessError('insufficient_data','退款币种或状态无法核实')
            actual=sum((money(x['amount']) for x in refunds if x['status'] in {'pending','processing','succeeded'}),Decimal(0))
            reserved+=max(actual,money(sale['requested_amount']) if sale['status'] in {'pending_review','approved','processing','completed'} else Decimal(0))
        if amount>paid-reserved: raise BusinessError('not_eligible','申请金额超过未占用的可退余额')
        return {'eligibility':'eligible','order_ref':ref('order',order['platform_code'],order['order_id']),'items':allocations,'quantity':sum(x['quantity'] for x in allocations),'amount':amount,'amount_kind':'refund_estimate','shipping_amount':shipping,'currency':order['currency'],'policy_id':p['policy_id'],'policy_version':p['version']}
    def assess_after_sale(self,tx,**args):
        return self.decision(lambda:self.sale_assessment(tx,args['kind'],args['items']))
    def coupon_state(self,row):
        expired=at(row['expires_at'])<=utcnow(); raw=row['status']
        status='expired' if raw=='available' and expired else raw
        if status not in {'available','reserved','used','expired','invalid'}: status='unknown'
        return dict(row,status=status,source_status=raw,is_expired=expired,coupon_ref=ref('coupon_instance',row['user_coupon_id']))
    def coupon(self,tx,value):
        typ,cid=unref(value,{'coupon_template','coupon_instance'}); instance=None
        if typ=='coupon_instance':
            instance=tx.one('SELECT * FROM user_coupons WHERE user_coupon_id=%s AND user_id=%s'+(' FOR UPDATE' if tx.write else ''),(cid,self.user))
            if not instance: raise BusinessError('not_found','优惠券不存在')
            cid=instance['coupon_id']
        template=tx.one('SELECT * FROM coupon_templates WHERE coupon_id=%s',(cid,))
        if not template: raise BusinessError('not_found','优惠券不存在')
        self.coupon_scope_check(template)
        scopes=tx.all('SELECT scope_type,scope_id FROM coupon_scopes WHERE coupon_id=%s ORDER BY scope_type,scope_id',(cid,))
        return template,instance,scopes
    def frozen_coupon(self,tx,template,instance,scopes):
        if not instance: return template,scopes
        saved=tx.one('SELECT rules_snapshot FROM coupon_claim_rules WHERE user_coupon_id=%s',(instance['user_coupon_id'],))
        if not saved: raise BusinessError('insufficient_data','持券缺少领取时规则快照')
        value=decoded(saved['rules_snapshot']); frozen=dict(value['template'])
        if value.get('membership'):
            frozen['membership_requirement']=value['membership']
        return frozen,value['scopes']
    def eligible_lines(self,scopes,lines):
        if not scopes: raise BusinessError('insufficient_data','优惠适用范围缺失')
        if any(s['scope_type'] not in {'all','merchant','product','sku'} for s in scopes): raise BusinessError('insufficient_data','未知优惠适用范围')
        def match(line):
            return any(s['scope_type']=='all' and s['scope_id']=='*' or s['scope_type']!='all' and str(line.get(s['scope_type']+'_id'))==s['scope_id'] for s in scopes)
        return [line for line in lines if match(line)]
    def discount(self,rule,base):
        minimum=money(rule.get('threshold_amount',rule.get('minimum_amount')))
        if base<minimum: return Decimal(0)
        if rule['kind']=='amount_off': amount=money(rule['discount_amount'])
        elif rule['kind']=='percent_off':
            bps=rule.get('discount_bps')
            if not isinstance(bps,int) or not 0<=bps<=10000: raise BusinessError('insufficient_data','折扣比例无效')
            amount=(base*Decimal(bps)/10000).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        else: raise BusinessError('insufficient_data','优惠类型未识别')
        if rule.get('cap_amount') is not None: amount=min(amount,money(rule['cap_amount']))
        return min(base,max(Decimal(0),amount))
    def list_promotions(self,tx,**args):
        subject=self.subject(tx,args.get('subject_ref'),allow_order=False); rows=tx.all("SELECT * FROM promotion_rules WHERE status='active' AND starts_at<=%s AND ends_at>%s ORDER BY priority DESC,promotion_id,version",(utcnow(),utcnow()))
        rows=[r for r in rows if (not subject.get('platform_code') or r['platform_code']==subject['platform_code']) and (r['merchant_id'] is None or not subject.get('merchant_id') or r['merchant_id']==subject['merchant_id'])]
        result=[]
        for r in rows:
            scopes=tx.all('SELECT scope_type,scope_id FROM promotion_scopes WHERE promotion_id=%s AND version=%s ORDER BY scope_type,scope_id',(r['promotion_id'],r['version']))
            if args.get('subject_ref') and scopes and not subject.get('order') and not self.eligible_lines(scopes,[subject]): continue
            result.append(dict(r,scopes=scopes[:20],scope_count=len(scopes),scope_complete=bool(scopes),scopes_truncated=len(scopes)>20))
        return self.page(result,'list_promotions',args,limitations=['活动展示不代表所有商品满足适用条件'])
    def claim_eligibility(self,tx,template):
        if template.get('coupon_type')=='newcomer':
            user=tx.one('SELECT newcomer_coupon_eligible,status FROM users WHERE user_id=%s'+(' FOR UPDATE' if tx.write else ''),(self.user,))
            if not user or user['status']!='active' or not user['newcomer_coupon_eligible']:
                raise BusinessError('not_eligible','仅首次注册且尚未领取新人权益的用户可领')
        if template['status']!='active' or not at(template['claim_starts_at'])<=utcnow()<at(template['claim_ends_at']): raise BusinessError('not_eligible','不在有效领券期间')
        if (template['valid_days'] is None)==(template['fixed_expires_at'] is None): raise BusinessError('insufficient_data','券有效期配置不完整')
        if template['fixed_expires_at'] and at(template['fixed_expires_at'])<=utcnow(): raise BusinessError('not_eligible','券已过期')
        if template['valid_days'] is not None and template['valid_days']<=0: raise BusinessError('insufficient_data','券有效天数无效')
        total=tx.one('SELECT COUNT(*) AS n FROM user_coupons WHERE coupon_id=%s',(template['coupon_id'],))['n']
        mine=tx.one('SELECT COUNT(*) AS n FROM user_coupons WHERE coupon_id=%s AND user_id=%s',(template['coupon_id'],self.user))['n']
        if template['total_limit'] is not None and total>=template['total_limit'] or mine>=template['per_user_limit']: raise BusinessError('not_eligible','领取数量已达到限制')
        from app.data.membership_store import coupon_membership_claim
        membership=coupon_membership_claim(tx,self.user,template)
        remaining=template['per_user_limit']-mine
        if membership: remaining=min(remaining,membership['remaining_count'])
        return {'remaining_total':None if template['total_limit'] is None else template['total_limit']-total,
            'remaining_user':remaining,'membership':membership}
    def list_claimable_coupons(self,tx,**args):
        subject=self.subject(tx,args.get('subject_ref'),allow_order=False); platform=subject.get('platform_code')
        if not platform: raise BusinessError('insufficient_data','请先明确销售平台')
        rows=tx.all('SELECT * FROM coupon_templates WHERE platform_code=%s ORDER BY coupon_id',(platform,)); found=[]
        for row in rows:
            if row['merchant_id'] and subject.get('merchant_id') and row['merchant_id']!=subject['merchant_id']: continue
            scopes=tx.all('SELECT scope_type,scope_id FROM coupon_scopes WHERE coupon_id=%s',(row['coupon_id'],))
            try:
                if not scopes: continue
                if args.get('subject_ref') and not self.eligible_lines(scopes,[subject]): continue
                availability=self.claim_eligibility(tx,row)
            except BusinessError as e:
                if e.code in {'not_eligible','insufficient_data'}: continue
                raise
            found.append(dict(row,**availability,coupon_ref=ref('coupon_template',row['coupon_id'])))
        return self.page(found,'list_claimable_coupons',args,limitations=['仅列出规则完整且当前满足基本领取条件的券，领取时再次校验'])
    def list_my_coupons(self,tx,**args):
        rows=tx.all('SELECT * FROM user_coupons WHERE user_id=%s ORDER BY expires_at,user_coupon_id',(self.user,)); result=[]
        for row in rows:
            if args.get('exclude_expired') and (row['status']=='expired' or at(row['expires_at'])<=utcnow()): continue
            try: template,instance,scopes=self.coupon(tx,ref('coupon_instance',row['user_coupon_id']))
            except BusinessError: continue
            row=self.coupon_state(row); row.pop('user_id',None)
            if args.get('coupon_type') is not None:
                current_type=template.get('coupon_type') or 'general'
                try: template,_=self.frozen_coupon(tx,template,instance,scopes)
                except BusinessError: pass
                row['coupon_type']=template.get('coupon_type') or current_type
                if args['coupon_type']!='all' and row['coupon_type']!=args['coupon_type']: continue
            if args.get('status','all')=='all' or row['status']==args['status']: result.append(row)
        return self.page(result,'list_my_coupons',args)
    def get_coupon(self,tx,**args):
        t,i,scopes=self.coupon(tx,args['coupon_ref']); limits=[]; basis='current_template'
        if i:
            try: t,scopes=self.frozen_coupon(tx,t,i,scopes); basis='claim_snapshot'
            except BusinessError: limits.append('缺少领取时规则快照，展示当前模板规则')
        rows=[dict(s,record_type='scope') for s in scopes]
        if i:
            records=tx.all('SELECT * FROM coupon_redemptions WHERE user_coupon_id=%s ORDER BY created_at,redemption_id',(i['user_coupon_id'],))
            for r in records:
                self.owned_order(tx,ref('order',r['platform_code'],r['order_id']))
                r.pop('request_id',None); rows.append(dict(r,record_type='redemption'))
        instance=self.coupon_state(i) if i else None
        if instance: instance.pop('user_id',None)
        return self.page(rows,'get_coupon',args,data={'template':t,'instance':instance,'rules_basis':basis},limitations=limits)
    def coupon_amount(self,tx,value,lines,platform,currency):
        t,i,scopes=self.coupon(tx,value)
        if not i: raise BusinessError('invalid_arguments','必须使用本人持券，不能使用模板')
        if self.coupon_state(i)['status']!='available': raise BusinessError('not_eligible','券不可用或已被占用')
        t,scopes=self.frozen_coupon(tx,t,i,scopes)
        from app.data.membership_store import check_coupon_membership_use
        check_coupon_membership_use(tx,self.user,t)
        if t['platform_code']!=platform or t['currency']!=currency: raise BusinessError('not_eligible','券平台或币种不匹配')
        matching=self.eligible_lines(scopes,lines)
        if t['merchant_id']: matching=[x for x in matching if x['merchant_id']==t['merchant_id']]
        base=sum((money(x['amount']) for x in matching),Decimal(0))
        minimum=money(t['threshold_amount'])
        if base<minimum or not matching: raise BusinessError('not_eligible','商品范围或用券门槛不满足',threshold_gap=format(max(minimum-base,Decimal(0)),'.2f'))
        return {'coupon_ref':value,'eligible_amount':base,'discount_amount':self.discount(t,base),'threshold_gap':Decimal(0)}
    def estimate_purchase(self,tx,**args):
        platform=self.sales_platform(tx)
        p,r=self.policy(tx,'pricing',platform=platform); merged={}
        for item in args['items']: merged[item['sku_id']]=merged.get(item['sku_id'],0)+item['quantity']
        lines=[]
        for sid,qty in sorted(merged.items()):
            sku=self.sku(tx,sid)
            if qty>999: raise BusinessError('invalid_arguments','合并后数量超过上限')
            if sku['status']!='active': raise BusinessError('not_eligible','规格不可售')
            if not sku['inventory']: raise BusinessError('insufficient_data','缺少库存数据')
            if sku['inventory']['available_qty']<qty: raise BusinessError('not_eligible','库存不足')
            lines.append(dict(sku_id=sid,product_id=sku['product_id'],merchant_id=sku['merchant_id'],currency=sku['currency'],quantity=qty,amount=money(sku['list_price'])*qty))
        if len({(x['merchant_id'],x['currency']) for x in lines})!=1: raise BusinessError('invalid_arguments','请按商家和币种分别试算')
        currency=lines[0]['currency']; original=sum((x['amount'] for x in lines),Decimal(0)); applied=[]; groups=set()
        promos=tx.all("SELECT * FROM promotion_rules WHERE platform_code=%s AND status='active' AND starts_at<=%s AND ends_at>%s ORDER BY priority DESC,promotion_id,version DESC"+(' FOR UPDATE' if tx.write else ''),(platform,utcnow(),utcnow()))
        seen=set()
        for promo in promos:
            if promo['merchant_id'] and promo['merchant_id']!=lines[0]['merchant_id']: continue
            if promo['currency']!=currency: continue
            if promo['channel'] is not None: raise BusinessError('insufficient_data','活动需要可信渠道上下文')
            if promo['promotion_id'] in seen: raise BusinessError('insufficient_data','活动有效版本冲突')
            seen.add(promo['promotion_id'])
            scopes=tx.all('SELECT scope_type,scope_id FROM promotion_scopes WHERE promotion_id=%s AND version=%s',(promo['promotion_id'],promo['version']))
            eligible=self.eligible_lines(scopes,lines); base=sum((x['amount'] for x in eligible),Decimal(0))
            if promo['exclusive_group'] and promo['exclusive_group'] in groups: continue
            amount=self.discount(promo,base)
            if not amount: continue
            if args.get('coupon_refs') and not promo.get('allows_coupon',False):
                raise BusinessError('not_eligible','该活动不允许与优惠券叠加')
            self.allocate_discount(eligible,amount)
            applied.append({'promotion_id':promo['promotion_id'],'version':promo['version'],'discount_amount':amount})
            if promo['exclusive_group']: groups.add(promo['exclusive_group'])
        coupons=args.get('coupon_refs',[])
        if len(coupons)>1 and not r.coupon_stacking: raise BusinessError('not_eligible','规则不允许多券叠加')
        if coupons and applied and not r.promotions_with_coupons: raise BusinessError('not_eligible','规则不允许活动与券叠加')
        coupon_results=[]
        for value in coupons:
            result=self.coupon_amount(tx,value,lines,platform,currency); coupon_results.append(result)
            t,i,scopes=self.coupon(tx,value); t,scopes=self.frozen_coupon(tx,t,i,scopes)
            eligible=self.eligible_lines(scopes,lines)
            if t['merchant_id']: eligible=[x for x in eligible if x['merchant_id']==t['merchant_id']]
            self.allocate_discount(eligible,result['discount_amount'])
        return ok({'currency':currency,'goods_amount':original,'goods_after_discount':sum((x['amount'] for x in lines),Decimal(0)),
            'shipping_amount':None,'payable_amount':None,'promotions':applied,'coupons':coupon_results,'items':lines,'policy_id':p['policy_id'],'policy_version':p['version']},limitations=['仅指定商品和用券组合的商品金额估算，运费未知'])
    def allocate_discount(self,lines,amount):
        total=sum((x['amount'] for x in lines),Decimal(0)); cumulative=Decimal(0); allocated=Decimal(0)
        if amount<0 or amount>total or any(x['amount']<0 for x in lines):
            raise BusinessError('insufficient_data','优惠分摊金额超出商品金额')
        if amount==0: return
        for line in sorted(lines,key=lambda x:x['sku_id']):
            cumulative+=line['amount']; target=(amount*cumulative/total).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            line['amount']-=target-allocated; allocated=target
    def search_policies(self,tx,**args):
        from app.rag.policy import PolicyContext
        from app.rag.service import search_policy
        from datetime import timezone
        subject=self.subject(tx,args.get('subject_ref')); order=subject.get('order')
        ships=self.order_rows(tx,'shipments',order) if order else []
        delivered=max(at(s['delivered_at']) for s in ships) if ships and all(s['delivered_at'] for s in ships) else None
        context=PolicyContext(platform_code=subject.get('platform_code'),store_id=subject.get('merchant_id'),category=subject.get('category'),request_at=utcnow().replace(tzinfo=timezone.utc),
            order_id=order['order_id'] if order else None,order_evidence_id='mysql:'+self.snapshot(tx,order) if order else None,
            delivered_at=delivered.replace(tzinfo=timezone.utc) if delivered else None,
            order_created_at=at(order['created_at']).replace(tzinfo=timezone.utc) if order and order.get('created_at') else None)
        result=search_policy(args['query'],3,context)
        if not result.get('success'): raise BusinessError('insufficient_data','没有可核实的政策检索结果')
        checks=[]; limitations=['政策说明不能代替订单资格规则执行']
        rows=tx.all("SELECT DISTINCT action FROM business_policy_versions WHERE platform_code=%s AND status='active'",(context.platform_code,)) if context.platform_code else []
        evidence={(e.get('source_id'),str(e.get('version'))) for e in result.get('results',[])}
        for row in rows:
            try:
                p,r=self.policy(tx,row['action'],order,context.platform_code)
                if not r.rag_policy_id: continue
                related=any(source==r.rag_policy_id for source,_ in evidence)
                if not related: continue
                matched=(r.rag_policy_id,r.rag_policy_version) in evidence
                checks.append({'action':row['action'],'policy_id':p['policy_id'],'policy_version':p['version'],'text_version_matches':matched})
                if not matched: limitations.append('政策文本与可执行业务规则版本不一致，需人工核实')
            except BusinessError as e:
                limitations.append(e.message)
        if not checks: limitations.append('尚未核实检索条款与可执行规则的版本对应关系')
        return ok({'policy_evidence':result,'rule_checks':checks},limitations=limitations)
