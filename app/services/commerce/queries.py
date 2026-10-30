"""Resource queries for the tool contract; projections contain recorded facts only."""
from decimal import Decimal
import json
from app.services.commerce.common import BusinessError, at, decoded, money, ok, ref, unref
from app.services.commerce.reads import ReadService, masked, state, SALE_STATES
from app.services.commerce.rules import RuleService
from app.services.commerce.collection_queries import CollectionQueryService
from app.services.commerce.item_queries import ItemQueryService, items_query


def filter_specs(rows, specs):
    known = {k for r in rows for k in r.get('spec_values', {})}
    applied = [s for s in specs if s['name'] in known]
    unknown = [s for s in specs if s['name'] not in known]
    return ([r for r in rows if all(str(r['spec_values'].get(s['name'])) == s['value'] for s in applied)], unknown)


class QueryService(ItemQueryService, CollectionQueryService, ReadService, RuleService):
    def get_my_account(self, tx, **args):
        collection=args.get('collection')
        if collection:
            if collection=='contacts':
                query='SELECT contact_id,contact_type,verified_at FROM user_contacts WHERE user_id=%s'
                keys=('contact_id',)
            elif collection=='platform_accounts':
                query='SELECT id,platform_code,status,verified_at FROM user_platform_accounts WHERE user_id=%s'
                keys=('platform_code','id')
            else:
                raise BusinessError('invalid_arguments','未知账号记录集合')
            result=self.sql_page(tx,query,[self.user],'get_my_account',args,keys=keys,
                data={'collection':collection},limitations=['联系方式和外部平台账号内容已隐藏'])
            if collection=='contacts':
                result['data']['items']=[dict(row,value='***') for row in result['data']['items']]
            return result
        if args.get('cursor'):
            raise BusinessError('invalid_arguments','账号翻页需要指定 collection')
        result = self.get_my_profile(tx)
        platforms = self.list_platform_accounts(tx)
        result['data']['platform_accounts'] = platforms['data']
        result['data']['contacts_truncated']=result['data']['contacts_total']>len(result['data']['contacts'])
        result['data']['contacts_query']=items_query('get_my_account',collection='contacts')
        platforms['data']['truncated']=platforms['data']['total']>len(platforms['data']['items'])
        platforms['data']['items_query']=items_query('get_my_account',collection='platform_accounts')
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
        terms=args['query'].casefold().split(); clauses=[]; values=[]
        for term in terms:
            pattern='%'+term.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
            clauses.append("(p.name LIKE %s ESCAPE '!' OR p.category LIKE %s ESCAPE '!' OR p.description LIKE %s ESCAPE '!' OR m.name LIKE %s ESCAPE '!')")
            values.extend([pattern]*4)
        if getattr(self.scope,'store_id',None): clauses.append('p.merchant_id=%s'); values.append(self.scope.store_id)
        sku_clauses=["s.status='active'"]; sku_values=[]; known_select=[]; known_values=[]
        cap=args.get('max_price')
        if cap:
            sku_clauses += ['s.currency=%s','s.list_price<=%s']; sku_values += [cap['currency'],money(cap['amount'])]
        for index,spec in enumerate(args.get('specs',[])):
            path='$.'+json.dumps(spec['name'],ensure_ascii=False)
            known_select.append("MAX(JSON_CONTAINS_PATH(spec_values,'one',%s)) AS known_"+str(index))
            known_values.append(path)
            sku_clauses.append("(JSON_UNQUOTE(JSON_EXTRACT(s.spec_values,%s))=%s OR flags.known_"+str(index)+'=0)')
            sku_values += [path,spec['value']]
        def sku_source(ids=()):
            if not known_select: return 'product_skus s',[]
            # Aggregate field presence once per product, across all its SKUs.
            # Nested correlated EXISTS predicates can become quadratic in MySQL.
            where=' WHERE product_id IN ('+','.join(['%s']*len(ids))+')' if ids else ''
            source='product_skus s JOIN (SELECT product_id,'+','.join(known_select)+ \
                ' FROM product_skus'+where+' GROUP BY product_id) flags ON flags.product_id=s.product_id'
            return source,known_values+list(ids)
        sku_where=' AND '.join(sku_clauses)
        source,source_values=sku_source()
        fields=['known_'+str(i) for i in range(len(known_select))]
        selected=''.join(',MAX(flags.'+field+') AS '+field for field in fields)
        matched='SELECT s.product_id'+selected+' FROM '+source+' WHERE '+sku_where+' GROUP BY s.product_id'
        query='SELECT p.product_id,p.merchant_id,p.name,p.category,p.description,m.name merchant_name'+ \
            ''.join(',matched.'+field for field in fields)+ \
            ' FROM products p JOIN merchants m ON m.merchant_id=p.merchant_id JOIN ('+matched+ \
            ') matched ON matched.product_id=p.product_id WHERE '+(' AND '.join(clauses) or '1=1')
        result=self.sql_page(tx,query,source_values+sku_values+values,'search_products',args,keys=('product_id',),size=10)
        rows=result['data']['items']
        if not rows: return result
        ids=[r['product_id'] for r in rows]
        source,source_values=sku_source(ids)
        skus=tx.all('''SELECT * FROM (SELECT s.*,p.merchant_id,p.name product_name,
            i.available_qty inventory_available_qty,i.updated_at inventory_updated_at,
            ROW_NUMBER() OVER (PARTITION BY s.product_id ORDER BY s.sku_id) rn,
            COUNT(*) OVER (PARTITION BY s.product_id) sku_count
            FROM '''+source+''' JOIN products p ON p.product_id=s.product_id
            LEFT JOIN sku_inventory i ON i.sku_id=s.sku_id WHERE s.product_id IN ('''+','.join(['%s']*len(ids))+') AND '+sku_where+') ranked WHERE rn<=5 ORDER BY product_id,sku_id',tuple(source_values+ids+sku_values))
        for row in rows:
            matches=[s for s in skus if s['product_id']==row['product_id']]
            count=matches[0]['sku_count']
            for sku in matches:
                sku.pop('rn'); sku.pop('sku_count')
                available=sku.pop('inventory_available_qty'); updated=sku.pop('inventory_updated_at')
                sku['inventory']=None if available is None else dict(available_qty=available,updated_at=updated)
                sku['spec_values']=decoded(sku['spec_values'],{}); sku['subject_ref']=ref('sku',sku['sku_id'])
            unknown=[s for i,s in enumerate(args.get('specs',[])) if not row.pop('known_'+str(i))]
            row.update(product_ref=ref('product',row['product_id']),subject_ref=ref('product',row['product_id']),
                matched_fields=[k for k in ('name','category','description','merchant_name') if any(t in str(row.get(k) or '').casefold() for t in terms)],
                sku_summary=matches,sku_count=count,skus_truncated=count>5,unapplied_conditions=unknown)
        return result

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

    def get_order(self, tx, **args):
        result=super().get_order(tx,**args)
        order=self.owned_order(tx,args['order_ref'])
        progress=self.get_order_items(tx,order_ref=args['order_ref'])['data']
        result['data'].update(items=progress['items'][:5],item_count=progress['total'],items_truncated=progress['total']>5,
            items_query=items_query('get_order_items',order_ref=args['order_ref']))
        saved=tx.one('SELECT * FROM order_edit_details WHERE platform_code=%s AND order_id=%s',(order['platform_code'],order['order_id']))
        if saved:
            purpose='order-edit:'+self.user+':'+order['platform_code']+':'+order['order_id']
            details=decoded(self.cipher.open(saved['details_ciphertext'],purpose,saved['key_version']))
            result['data']['note']=details.get('note')
            result['data']['invoice_info']={'invoice_type':details['invoice']['invoice_type'],'title':'***','tax_number':'***' if details['invoice'].get('tax_number') else None} if details.get('invoice') else None
        return result

    def get_dispatch_status(self, tx, **args):
        order = self.owned_order(tx, args['order_ref'])
        result = self.sql_page(
            tx,
            '''SELECT request_id,status,provider_reference,created_at,updated_at
               FROM commerce_provider_requests
               WHERE user_id=%s AND platform_code=%s AND order_id=%s AND kind='dispatch' ''',
            [self.user, order['platform_code'], order['order_id']],
            'get_dispatch_status', args, keys=('created_at', 'request_id'),
            data={key: order[key] for key in ('platform_code', 'order_id')},
            limitations=['催单受理或处理完成不等于已经发货；实际发货状态需查询物流'],
        )
        valid = {'queued', 'accepted', 'processing', 'completed', 'rejected', 'cancelled', 'unknown'}
        result['data']['items'] = [state(row, valid) for row in result['data']['items']]
        return result

    def parcel_summary(self,tx,ship,reverse=False):
        if reverse:
            auth=tx.one('SELECT after_sale_id FROM return_authorizations WHERE authorization_id=%s',(ship['authorization_id'],))
            sale,order=self.sale(tx,auth['after_sale_id'])
            ship=dict(ship,after_sale_id=sale['after_sale_id'],platform_code=order['platform_code'],order_id=order['order_id'])
            if sale.get('is_demo'): ship.update(is_demo=True,source='simulated_provider')
        else:
            order=self.owned_order(tx,ref('order',ship['platform_code'],ship['order_id']))
        key='return_shipment_id' if reverse else 'shipment_id'
        table='return_shipment' if reverse else 'shipment'
        source=' FROM '+table+'_items s LEFT JOIN order_items i ON i.order_item_id=s.order_item_id AND i.platform_code=%s AND i.order_id=%s WHERE s.'+key+'=%s'
        params=(order['platform_code'],order['order_id'],ship[key])
        if tx.one('SELECT 1 invalid'+source+' AND i.order_item_id IS NULL LIMIT 1',params):
            raise BusinessError('insufficient_data','包裹明细归属不一致')
        items=tx.all('SELECT s.*,i.sku_id,i.product_id'+source+' ORDER BY s.order_item_id LIMIT 20',params)
        for item in items:
            if 'actual_spec_snapshot' in item: item['actual_spec_snapshot']=decoded(item['actual_spec_snapshot'])
        events=tx.all('SELECT * FROM '+table+'_events WHERE '+key+'=%s ORDER BY event_time DESC,event_id DESC LIMIT 3',(ship[key],))
        count=tx.one('SELECT COUNT(*) n FROM '+table+'_events WHERE '+key+'=%s',(ship[key],))['n']
        item_count=tx.one('SELECT COUNT(*) n FROM '+table+'_items WHERE '+key+'=%s',(ship[key],))['n']
        return dict(ship,target_ref=ref(table,ship[key]),items=items,item_count=item_count,items_truncated=item_count>20,
                    items_query=items_query('get_shipment_items',target_ref=ref(table,ship[key])),
                    events=events,events_total=count,events_truncated=count>3)

    def get_logistics(self,tx,**args):
        parsed=unref(args['target_ref'],{'order','shipment','return_shipment','after_sale'})
        if parsed[0]=='shipment':
            result=self.get_shipment_tracking(tx,shipment_id=parsed[1],cursor=args.get('cursor'))
            result['data']['shipment']=self.parcel_summary(tx,result['data']['shipment'])
            return result
        if parsed[0]=='return_shipment':
            ship=tx.one('''SELECT s.*,a.after_sale_id FROM return_shipments s JOIN return_authorizations a
                ON a.authorization_id=s.authorization_id WHERE s.return_shipment_id=%s''',(parsed[1],))
            if not ship: raise BusinessError('not_found','退货包裹不存在')
            self.sale(tx,ship['after_sale_id'])
            return self.sql_page(tx,'SELECT * FROM return_shipment_events WHERE return_shipment_id=%s',[parsed[1]],
                'get_return_tracking',args,keys=('event_time','event_id'),data={'shipment':self.parcel_summary(tx,ship,True)})
        if parsed[0]=='after_sale':
            self.sale(tx,parsed[1])
            query="""SELECT CONCAT('r:',s.return_shipment_id) package_key,'return_shipment' kind,s.return_shipment_id ident
                FROM return_shipments s JOIN return_authorizations a ON a.authorization_id=s.authorization_id WHERE a.after_sale_id=%s
                UNION ALL SELECT CONCAT('s:',shipment_id) package_key,'shipment' kind,shipment_id ident
                FROM shipments WHERE after_sale_id=%s AND shipment_kind='exchange' """
            result=self.sql_page(tx,query,[parsed[1],parsed[1]],'get_logistics',args,keys=('package_key',),size=5)
            for i,row in enumerate(result['data']['items']):
                reverse=row['kind']=='return_shipment'; table='return_shipments' if reverse else 'shipments'
                key='return_shipment_id' if reverse else 'shipment_id'
                ship=tx.one('SELECT * FROM '+table+' WHERE '+key+'=%s',(row['ident'],))
                result['data']['items'][i]=self.parcel_summary(tx,ship,reverse)
            return result
        return self.order_logistics(tx,args)

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
                estimate=self.estimate_purchase(tx,items=target['items'],coupon_refs=[])['data']
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

    def get_after_sale_status(self,tx,**args):
        sale,order=self.sale(tx,args['after_sale_id'])
        source=''' FROM after_sale_items s LEFT JOIN order_items i ON i.order_item_id=s.order_item_id
            AND i.platform_code=%s AND i.order_id=%s WHERE s.after_sale_id=%s'''
        params=(order['platform_code'],order['order_id'],sale['after_sale_id'])
        if tx.one('SELECT 1 invalid'+source+' AND i.order_item_id IS NULL LIMIT 1',params):
            raise BusinessError('insufficient_data','售后明细归属不一致')
        items=tx.all('SELECT s.*,i.sku_id,i.product_id'+source+' ORDER BY s.order_item_id LIMIT 20',params)
        item_count=tx.one('SELECT COUNT(*) n FROM after_sale_items WHERE after_sale_id=%s',(sale['after_sale_id'],))['n']
        _,_,_,refunds=self.refund_query(tx,ref('after_sale',sale['after_sale_id']))
        work=tx.one('SELECT * FROM service_work_orders WHERE after_sale_id=%s',(sale['after_sale_id'],))
        if work: work=dict(work,details=decoded(work['details']))
        auth=tx.one('SELECT authorization_id,status,deadline,source,updated_at FROM return_authorizations WHERE after_sale_id=%s',(sale['after_sale_id'],))
        ships=tx.all('SELECT * FROM return_shipments WHERE authorization_id=%s ORDER BY created_at,return_shipment_id LIMIT 5',(auth['authorization_id'],)) if auth else []
        ship_count=tx.one('SELECT COUNT(*) n FROM return_shipments WHERE authorization_id=%s',(auth['authorization_id'],))['n'] if auth else 0
        ships=[self.parcel_summary(tx,s,True) for s in ships]
        reverse_query=''' FROM return_shipment_events e JOIN return_shipments s ON s.return_shipment_id=e.return_shipment_id
            JOIN return_authorizations a ON a.authorization_id=s.authorization_id WHERE a.after_sale_id=%s'''
        reverse_events=tx.all('SELECT e.*'+reverse_query+' ORDER BY e.event_time DESC,e.event_id DESC LIMIT 3',(sale['after_sale_id'],))
        reverse_count=tx.one('SELECT COUNT(*) n'+reverse_query,(sale['after_sale_id'],))['n']
        replacements=tx.all("SELECT * FROM shipments WHERE after_sale_id=%s AND shipment_kind='exchange' ORDER BY created_at,shipment_id LIMIT 5",(sale['after_sale_id'],))
        replacement_count=tx.one("SELECT COUNT(*) n FROM shipments WHERE after_sale_id=%s AND shipment_kind='exchange'",(sale['after_sale_id'],))['n']
        replacements=[self.parcel_summary(tx,s) for s in replacements]
        return self.sql_page(tx,'SELECT * FROM after_sale_events WHERE after_sale_id=%s',[sale['after_sale_id']],
            'get_after_sale_status',args,keys=('created_at','event_id'),data={'after_sale':state(masked(sale),SALE_STATES),'order_ref':ref('order',order['platform_code'],order['order_id']),
            'purchase_items':items,'item_count':item_count,'items_truncated':item_count>20,'refund_summary':refunds,'work_order':work,'return_authorization':auth,
            'purchase_items_query':items_query('get_after_sale_items',after_sale_id=sale['after_sale_id']),
            'return_shipment':ships[0] if ship_count==1 else None,'return_shipments':ships,'replacement_shipments':replacements,
            'return_shipments_total':ship_count,'return_shipments_truncated':ship_count>5,
            'replacement_shipments_total':replacement_count,'replacement_shipments_truncated':replacement_count>5,
            'logistics_ref':ref('after_sale',sale['after_sale_id']),
            'return_events':reverse_events,'return_events_total':reverse_count,'processes_connected':{'return':bool(auth),'service':bool(work)}})
