"""Bounded collection reads; aggregates always cover the full matching collection."""
from decimal import Decimal

from app.services.commerce.common import BusinessError, at, decoded, money, ref, unref, utcnow
from app.services.commerce.reads import ORDER_STATES, SALE_STATES, masked, state


def placeholders(values):
    return ','.join(['%s'] * len(values))


def pattern(value):
    return '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'


def status_filter(clauses, values, column, requested, known):
    if requested == 'unknown':
        clauses.append(f'({column} IS NULL OR {column} NOT IN ({placeholders(known)}))')
        values.extend(sorted(known))
    elif requested and requested != 'all':
        clauses.append(column + '=%s')
        values.append(requested)


class CollectionQueryService:
    @staticmethod
    def replacement_predicate():
        # Older exchange records predate shipment_kind; keep their recorded linkage.
        return """(s.shipment_kind='exchange' OR EXISTS (
            SELECT 1 FROM service_work_orders w JOIN after_sale_requests a ON a.after_sale_id=w.after_sale_id
            WHERE w.kind='exchange' AND a.platform_code=s.platform_code AND a.order_id=s.order_id
            AND JSON_UNQUOTE(JSON_EXTRACT(w.details,'$.replacement_shipment_id'))=s.shipment_id))"""

    def get_order_items(self, tx, **args):
        order = self.owned_order(tx, args['order_ref'])
        result = self.sql_page(tx, 'SELECT * FROM order_items WHERE platform_code=%s AND order_id=%s',
            [order['platform_code'], order['order_id']], 'get_order_items', args, keys=('order_item_id',))
        rows = result['data']['items']
        if not rows: return result
        ids = [row['order_item_id'] for row in rows]
        params = tuple([order['platform_code'], order['order_id']] + ids)
        shipped = {row['order_item_id']: row for row in tx.all('''SELECT order_item_id,
            SUM(CASE WHEN replacement=0 THEN quantity ELSE 0 END) shipped,
            SUM(CASE WHEN replacement=1 THEN quantity ELSE 0 END) replaced,
            SUM(CASE WHEN replacement=0 AND status='delivered' AND delivered_at IS NOT NULL THEN quantity ELSE 0 END) delivered
            FROM (SELECT i.order_item_id,i.quantity,s.status,s.delivered_at,CASE WHEN ''' + self.replacement_predicate() + ''' THEN 1 ELSE 0 END replacement
                FROM shipment_items i JOIN shipments s ON s.shipment_id=i.shipment_id
                WHERE s.platform_code=%s AND s.order_id=%s AND i.order_item_id IN (''' + placeholders(ids) + ''')
                AND s.shipped_at IS NOT NULL AND s.status IN ('shipped','in_transit','delivered')) facts GROUP BY order_item_id''', params)}
        source = ''' FROM after_sale_items i JOIN after_sale_requests a ON a.after_sale_id=i.after_sale_id
            WHERE a.platform_code=%s AND a.order_id=%s AND i.order_item_id IN (''' + placeholders(ids) + ')'
        # Compute occupancy and status over ALL facts, independently of the bounded summary.
        known = "('pending_review','approved','rejected','processing','completed','cancelled','closed')"
        unknown = '(a.status IS NULL OR a.status NOT IN ' + known + ')'
        stats = {row['order_item_id']: row for row in tx.all('''SELECT i.order_item_id,COUNT(*) sale_count,
            SUM(CASE WHEN a.kind<>'price_protection' AND a.status NOT IN ('cancelled','rejected','closed') THEN i.quantity ELSE 0 END) occupied,
            MAX(CASE WHEN a.kind<>'price_protection' AND ''' + unknown + ''' THEN 1 ELSE 0 END) unknown_occupied,
            MAX(CASE WHEN ''' + unknown + ''' THEN 1 ELSE 0 END) unknown_status,
            MAX(CASE WHEN a.status IN ('pending_review','approved','processing') THEN 1 ELSE 0 END) active,
            MAX(CASE WHEN a.status='completed' THEN 1 ELSE 0 END) completed''' + source + ' GROUP BY i.order_item_id', params)}
        summaries = {}
        for row in tx.all('''SELECT * FROM (SELECT i.order_item_id,i.quantity,a.after_sale_id,a.kind,a.status,a.stage,a.completed_at,
            ROW_NUMBER() OVER (PARTITION BY i.order_item_id ORDER BY a.after_sale_id) rn''' + source + ') ranked WHERE rn<=5 ORDER BY order_item_id,rn', params):
            summaries.setdefault(row.pop('order_item_id'), []).append({k: v for k, v in row.items() if k != 'rn'})
        base = self.order_base_status(order)
        for item in rows:
            item['spec_snapshot'] = decoded(item['spec_snapshot'])
            amounts = shipped.get(item['order_item_id'], {})
            sent, replaced, delivered = (int(amounts.get(k, 0)) for k in ('shipped', 'replaced', 'delivered'))
            sales = stats.get(item['order_item_id'], {})
            count = sales.get('sale_count', 0)
            fulfillment = ('received' if delivered >= item['quantity'] else 'partially_received' if delivered else
                'pending_receipt' if sent >= item['quantity'] else 'partially_shipped' if sent else
                base if base in {'pending_payment', 'cancelled', 'closed'} else
                'paid' if order.get('paid_amount') and base in {'paid', 'pending_receipt'} else 'unknown')
            item.update(recorded_shipped=sent, recorded_replacement_shipped=replaced, recorded_delivered=delivered,
                unshipped_quantity=max(0, item['quantity'] - sent), fulfillment_status=fulfillment,
                after_sale_occupied=None if sales.get('unknown_occupied') else int(sales.get('occupied', 0)),
                after_sales=summaries.get(item['order_item_id'], []), after_sales_total=count, after_sales_truncated=count > 5,
                after_sale_status='unknown' if sales.get('unknown_status') else 'processing' if sales.get('active') else
                    'completed' if sales.get('completed') else 'closed' if count else 'none')
        return result

    def owned_collection(self, alias='o'):
        clauses = [alias + '.user_id=%s']
        values = [self.user]
        for attribute, column in [('platform_code', 'platform_code'), ('store_id', 'merchant_id')]:
            value = getattr(self.scope, attribute, None)
            if value:
                clauses.append(alias + '.' + column + '=%s')
                values.append(value)
        return clauses, values

    def order_logistics(self, tx, args):
        order = self.owned_order(tx, args['target_ref'])
        params = (order['platform_code'], order['order_id'])
        source = ''' FROM shipment_items i JOIN shipments s ON s.shipment_id=i.shipment_id
            WHERE s.platform_code=%s AND s.order_id=%s'''
        if tx.one('SELECT 1 invalid' + source + ''' AND NOT EXISTS (SELECT 1 FROM order_items o
                WHERE o.order_item_id=i.order_item_id AND o.platform_code=s.platform_code AND o.order_id=s.order_id) LIMIT 1''', params):
            raise BusinessError('insufficient_data', '包裹明细归属不一致')
        result = self.sql_page(tx, 'SELECT * FROM shipments WHERE platform_code=%s AND order_id=%s', params,
            'get_logistics', args, keys=('shipment_id',), size=5,
            limitations=['未记录预计送达时间；发出数量不代表签收数量'])
        rows = result['data']['items']; replacements = {}
        if rows:
            ids = [row['shipment_id'] for row in rows]
            for work in tx.all('''SELECT details,after_sale_id FROM (SELECT w.details,w.after_sale_id,
                ROW_NUMBER() OVER (PARTITION BY JSON_UNQUOTE(JSON_EXTRACT(w.details,'$.replacement_shipment_id')) ORDER BY w.after_sale_id DESC) rn
                FROM service_work_orders w
                JOIN after_sale_requests a ON a.after_sale_id=w.after_sale_id
                WHERE a.platform_code=%s AND a.order_id=%s AND w.kind='exchange'
                AND JSON_UNQUOTE(JSON_EXTRACT(w.details,'$.replacement_shipment_id')) IN (''' + placeholders(ids) + ')) ranked WHERE rn=1', tuple(list(params) + ids)):
                details = decoded(work['details'])
                replacements[details['replacement_shipment_id']] = dict(details, after_sale_id=work['after_sale_id'])
        for index, ship in enumerate(rows):
            replacement = replacements.get(ship['shipment_id'])
            if ship.get('shipment_kind') == 'exchange':
                replacement = dict(replacement or {}, after_sale_id=ship['after_sale_id'])
            rows[index] = dict(self.parcel_summary(tx, ship), replacement=replacement, estimated_delivery=None)
        progress = self.get_order_items(tx, order_ref=args['target_ref'])['data']
        totals = tx.one('SELECT COALESCE(SUM(quantity),0) purchased FROM order_items WHERE platform_code=%s AND order_id=%s', params)
        totals.update(tx.one('''SELECT COALESCE(SUM(CASE WHEN replacement=0 THEN quantity ELSE 0 END),0) recorded_shipped,
            COALESCE(SUM(CASE WHEN replacement=1 THEN quantity ELSE 0 END),0) recorded_replacement_shipped
            FROM (SELECT i.quantity,CASE WHEN ''' + self.replacement_predicate() + ''' THEN 1 ELSE 0 END replacement''' + source + '''
            AND s.shipped_at IS NOT NULL AND s.status IN ('shipped','in_transit','delivered')) facts''', params))
        result['data'].update(quantities=[dict(order_item_id=i['order_item_id'], purchased=i['quantity'],
            recorded_shipped=i['recorded_shipped'], recorded_replacement_shipped=i['recorded_replacement_shipped']) for i in progress['items']],
            quantities_total=progress['total'], quantities_truncated=progress['has_more'], quantity_totals=totals,
            quantities_order_ref=args['target_ref'],
            quantities_query={'tool': 'get_order_items', 'arguments': {'order_ref': args['target_ref']}})
        return result

    def find_orders(self, tx, **args):
        clauses, values = self.owned_collection()
        query = args.get('query') or args.get('keyword')
        if query:
            clauses.append("""(o.order_id LIKE %s ESCAPE '!' OR EXISTS (
                SELECT 1 FROM order_items i WHERE i.platform_code=o.platform_code
                AND i.order_id=o.order_id AND i.title_snapshot LIKE %s ESCAPE '!'))""")
            values += [pattern(query)] * 2
        if args.get('created_range'):
            clauses += ['o.created_at>=%s', 'o.created_at<%s']
            values += [at(args['created_range']['from']), at(args['created_range']['to'])]
        status_filter(clauses, values, 'o.status', args.get('status'), ORDER_STATES)
        result = self.sql_page(tx, 'SELECT o.* FROM orders o WHERE ' + ' AND '.join(clauses), values,
            'find_orders', args, keys=('created_at', 'platform_code', 'order_id'), descending=('created_at',), size=10)
        rows = result['data']['items']
        summaries = {}
        if rows:
            pairs = ' OR '.join(['(platform_code=%s AND order_id=%s)'] * len(rows))
            params = tuple(value for row in rows for value in (row['platform_code'], row['order_id']))
            items = tx.all('''SELECT * FROM (SELECT platform_code,order_id,title_snapshot,spec_snapshot,quantity,
                ROW_NUMBER() OVER (PARTITION BY platform_code,order_id ORDER BY order_item_id) rn,
                COUNT(*) OVER (PARTITION BY platform_code,order_id) item_count
                FROM order_items WHERE ''' + pairs + ') ranked WHERE rn<=3 ORDER BY platform_code,order_id,rn', params)
            for item in items:
                summaries.setdefault((item['platform_code'], item['order_id']), []).append(item)
        result['data']['items'] = []
        for row in rows:
            items = summaries.get((row['platform_code'], row['order_id']), [])
            result['data']['items'].append(dict(state(masked(row), ORDER_STATES),
                order_ref=ref('order', row['platform_code'], row['order_id']),
                purchase_summary=[dict(name=i['title_snapshot'], specs=decoded(i['spec_snapshot']), quantity=i['quantity']) for i in items],
                items_truncated=bool(items and items[0]['item_count']>3),
                items_query={'tool':'get_order_items','arguments':{'order_ref':ref('order',row['platform_code'],row['order_id'])}},
                item_count=items[0]['item_count'] if items else 0))
        return result

    def find_after_sales(self, tx, **args):
        clauses, values = self.owned_collection()
        clauses.append('a.user_id=%s'); values.append(self.user)
        if args.get('order_ref'):
            order = self.owned_order(tx, args['order_ref'])
            clauses += ['a.platform_code=%s', 'a.order_id=%s']
            values += [order['platform_code'], order['order_id']]
        if args.get('created_range'):
            clauses += ['a.created_at>=%s', 'a.created_at<%s']
            values += [at(args['created_range']['from']), at(args['created_range']['to'])]
        if args.get('query'):
            clauses.append("""(a.after_sale_id LIKE %s ESCAPE '!' OR a.order_id LIKE %s ESCAPE '!' OR EXISTS (
                SELECT 1 FROM after_sale_items s JOIN order_items i ON i.order_item_id=s.order_item_id
                WHERE s.after_sale_id=a.after_sale_id AND i.platform_code=a.platform_code AND i.order_id=a.order_id
                AND i.title_snapshot LIKE %s ESCAPE '!'))""")
            values += [pattern(args['query'])] * 3
        status_filter(clauses, values, 'a.status', args.get('status'), SALE_STATES)
        result = self.sql_page(tx, '''SELECT a.*,o.currency FROM after_sale_requests a
            JOIN orders o ON o.platform_code=a.platform_code AND o.order_id=a.order_id WHERE ''' + ' AND '.join(clauses),
            values, 'find_after_sales', args, keys=('created_at', 'after_sale_id'), descending=('created_at',), size=10)
        rows = result['data']['items']; groups = {}
        if rows:
            ids = [r['after_sale_id'] for r in rows]
            items = tx.all('''SELECT * FROM (SELECT s.after_sale_id,i.title_snapshot,i.spec_snapshot,s.quantity,s.order_item_id,
                ROW_NUMBER() OVER (PARTITION BY s.after_sale_id ORDER BY s.order_item_id) rn,
                COUNT(*) OVER (PARTITION BY s.after_sale_id) item_count
                FROM after_sale_items s JOIN after_sale_requests a ON a.after_sale_id=s.after_sale_id
                JOIN order_items i ON i.order_item_id=s.order_item_id AND i.platform_code=a.platform_code AND i.order_id=a.order_id
                WHERE s.after_sale_id IN (''' + placeholders(ids) + ')) ranked WHERE rn<=5 ORDER BY after_sale_id,rn', tuple(ids))
            for item in items:
                groups.setdefault(item['after_sale_id'], []).append(item)
        result['data']['items'] = []
        for row in rows:
            items = groups.get(row['after_sale_id'], [])
            result['data']['items'].append(dict(state(masked(row), SALE_STATES),
                order_ref=ref('order', row['platform_code'], row['order_id']), target_ref=ref('after_sale', row['after_sale_id']),
                items=[{k: v for k, v in i.items() if k not in {'rn', 'item_count', 'after_sale_id'}} for i in items],
                items_truncated=bool(items and items[0]['item_count']>5),
                items_query={'tool':'get_after_sale_items','arguments':{'after_sale_id':row['after_sale_id']}},
                item_count=items[0]['item_count'] if items else 0))
        return result

    def find_my_coupons(self, tx, **args):
        subject = self.subject(tx, args['subject_ref'], allow_order=False) if args.get('subject_ref') else None
        clauses = ['u.user_id=%s']; values = [self.user]
        if getattr(self.scope, 'platform_code', None):
            clauses.append('t.platform_code=%s'); values.append(self.scope.platform_code)
        if getattr(self.scope, 'store_id', None):
            clauses.append('(t.merchant_id IS NULL OR t.merchant_id=%s)'); values.append(self.scope.store_id)
        source = ''' FROM user_coupons u JOIN coupon_templates t ON t.coupon_id=u.coupon_id
            LEFT JOIN coupon_claim_rules r ON r.user_coupon_id=u.user_coupon_id'''
        # Missing frozen rules must not silently disappear behind a SQL filter.
        if tx.one('SELECT 1 AS invalid' + source + ' WHERE ' + ' AND '.join(clauses) + ' AND r.user_coupon_id IS NULL LIMIT 1', tuple(values)):
            raise BusinessError('insufficient_data', '持券缺少领取时规则快照')
        scopes = "JSON_TABLE(r.rules_snapshot,'$.scopes[*]' COLUMNS(scope_type VARCHAR(64) PATH '$.scope_type',scope_id VARCHAR(128) PATH '$.scope_id')) scopes"
        if subject:
            invalid = "(scopes.scope_type IS NULL OR scopes.scope_type NOT IN ('all','merchant','product','sku'))"
            if tx.one('SELECT 1 AS invalid' + source + ' LEFT JOIN ' + scopes + ' ON 1=1 WHERE ' + ' AND '.join(clauses) + ' AND ' + invalid + ' LIMIT 1', tuple(values)):
                raise BusinessError('insufficient_data', '优惠适用范围缺失或未知')
            matches = ["JSON_CONTAINS(r.rules_snapshot,JSON_OBJECT('scope_type','all','scope_id','*'),'$.scopes')"]
            for kind in ('merchant', 'product', 'sku'):
                if subject.get(kind + '_id') is not None:
                    matches.append("JSON_CONTAINS(r.rules_snapshot,JSON_OBJECT('scope_type',%s,'scope_id',%s),'$.scopes')")
                    values += [kind, str(subject[kind + '_id'])]
            clauses.append('(' + ' OR '.join(matches) + ')')
        if args.get('query'):
            source += " LEFT JOIN merchants m ON m.merchant_id=JSON_UNQUOTE(JSON_EXTRACT(r.rules_snapshot,'$.template.merchant_id'))"
            clauses.append("(JSON_UNQUOTE(JSON_EXTRACT(r.rules_snapshot,'$.template.name')) LIKE %s ESCAPE '!' OR m.name LIKE %s ESCAPE '!')")
            values += [pattern(args['query'])] * 2
        now = utcnow()
        effective = "CASE WHEN u.status='available' AND u.expires_at<=%s THEN 'expired' ELSE u.status END"
        requested = args.get('status')
        if requested == 'unknown':
            # Effective status can only differ from the source for available coupons.
            status_filter(clauses, values, 'u.status', requested, {'available', 'reserved', 'used', 'expired', 'invalid'})
        elif requested and requested != 'all':
            clauses.append(effective + '=%s'); values += [now, requested]
        result = self.sql_page(tx, 'SELECT u.*,r.rules_snapshot' + source + ' WHERE ' + ' AND '.join(clauses), values,
            'find_my_coupons', args, keys=('expires_at', 'user_coupon_id'), size=10)
        items = []
        for row in result['data']['items']:
            snapshot = decoded(row.pop('rules_snapshot'))
            template = dict(snapshot['template'])
            if snapshot.get('membership'): template['membership_requirement'] = snapshot['membership']
            items.append(dict(masked(self.coupon_state(row)), template=template, rules_basis='claim_snapshot'))
        result['data']['items'] = items
        return result

    def get_invoices(self, tx, **args):
        if args.get('view')=='issued':
            if not args.get('target_ref'):
                raise BusinessError('invalid_arguments','正式发票记录分页需要开票申请引用')
            _, ident = unref(args['target_ref'], {'invoice'})
            invoice = tx.one('SELECT * FROM invoice_requests WHERE invoice_id=%s AND user_id=%s', (ident,self.user))
            if not invoice:
                raise BusinessError('not_found','开票申请不存在')
            self.owned_order(tx,ref('order',invoice['platform_code'],invoice['order_id']))
            result = self.sql_page(tx,'SELECT * FROM issued_invoices WHERE invoice_id=%s',[ident],
                'get_invoices',args,keys=('issued_at','issued_invoice_id'),
                data={'collection':'issued','target_ref':args['target_ref'],
                      'order_ref':ref('order',invoice['platform_code'],invoice['order_id'])})
            from app.services.commerce.providers import invoice_file
            for document in result['data']['items']:
                available = False
                if document['status']=='issued' and document['document_key']:
                    try: invoice_file(document); available=True
                    except BusinessError: pass
                document.pop('document_key')
                document['document_available']=available
                document['document_query']={'tool':'get_invoice_document','arguments':{'issued_invoice_id':document['issued_invoice_id']}}
            return result
        target = unref(args['target_ref'], {'order', 'invoice'}) if args.get('target_ref') else None
        clauses, values = self.owned_collection()
        clauses.append('i.user_id=%s'); values.append(self.user)
        if target and target[0] == 'order':
            order = self.owned_order(tx, args['target_ref'])
            clauses += ['i.platform_code=%s', 'i.order_id=%s']; values += [order['platform_code'], order['order_id']]
        elif target:
            clauses.append('i.invoice_id=%s'); values.append(target[1])
        result = self.sql_page(tx, '''SELECT i.invoice_id,i.platform_code,i.order_id,i.invoice_type,i.status,i.created_at,i.issued_at,i.is_demo
            FROM invoice_requests i JOIN orders o ON o.platform_code=i.platform_code AND o.order_id=i.order_id WHERE ''' + ' AND '.join(clauses),
            values, 'get_invoices', args, keys=('created_at', 'invoice_id'), descending=('created_at',))
        if target and target[0] == 'invoice' and not result['data']['total']:
            raise BusinessError('not_found', '开票申请不存在')
        rows = result['data']['items']; groups = {}
        if rows:
            ids = [r['invoice_id'] for r in rows]
            documents = tx.all('''SELECT * FROM (SELECT d.*,
                ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY issued_at,issued_invoice_id) rn,
                COUNT(*) OVER (PARTITION BY invoice_id) document_count FROM issued_invoices d
                WHERE invoice_id IN (''' + placeholders(ids) + ')) ranked WHERE rn<=5 ORDER BY invoice_id,rn', tuple(ids))
            from app.services.commerce.providers import invoice_file
            for document in documents:
                available = False
                if document['status'] == 'issued' and document['document_key']:
                    try: invoice_file(document); available = True
                    except BusinessError: pass
                document.pop('document_key'); document.pop('rn')
                document['document_available'] = available
                groups.setdefault(document['invoice_id'], []).append(document)
        for row in rows:
            documents = groups.get(row['invoice_id'], [])
            count = documents[0]['document_count'] if documents else 0
            for document in documents: document.pop('document_count')
            row.update(target_ref=ref('invoice', row['invoice_id']), title='***',
                order_ref=ref('order', row['platform_code'], row['order_id']), issued_invoices=documents,
                issued_invoices_total=count, issued_invoices_truncated=count > 5,
                issued_invoices_query={'tool':'get_invoices','arguments':{'target_ref':ref('invoice',row['invoice_id']),'view':'issued'}})
        return result

    def get_payments(self, tx, **args):
        order = self.owned_order(tx, args['order_ref'])
        values = [order['platform_code'], order['order_id']]
        source = ' FROM payment_transactions WHERE platform_code=%s AND order_id=%s'
        amounts = {r['currency']: r['amount'] for r in tx.all(
            'SELECT currency,SUM(amount) amount' + source + " AND status='succeeded' GROUP BY currency", tuple(values))}
        paid = order.get('paid_amount'); currency = order.get('currency')
        result = self.sql_page(tx, 'SELECT *' + source, values, 'get_payments', args, keys=('created_at', 'payment_id'),
            data=dict(recorded_success_amounts=amounts, order_paid_amount=paid, order_currency=currency,
                recorded_difference=amounts.get(currency, Decimal(0)) - money(paid) if paid is not None and currency else None),
            limitations=['仅已记录渠道流水，不推断银行卡余额或未同步扣款'])
        result['data']['items'] = [masked(row) for row in result['data']['items']]
        return result

    def refund_query(self, tx, target_ref):
        target = unref(target_ref, {'order', 'after_sale'})
        if target[0] == 'after_sale':
            sale, order = self.sale(tx, target[1])
            where = 'a.after_sale_id=%s'; values = [sale['after_sale_id']]
        else:
            order = self.owned_order(tx, target_ref)
            where = 'a.platform_code=%s AND a.order_id=%s'; values = [order['platform_code'], order['order_id']]
        source = ' FROM refund_transactions r JOIN after_sale_requests a ON a.after_sale_id=r.after_sale_id WHERE ' + where
        if tx.one('SELECT 1 invalid' + source + ''' AND r.payment_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM payment_transactions p WHERE p.payment_id=r.payment_id
                AND p.platform_code=a.platform_code AND p.order_id=a.order_id) LIMIT 1''', tuple(values)):
            raise BusinessError('insufficient_data', '退款原支付关联无法核实')
        amounts = {}
        for row in tx.all('SELECT r.currency,r.status,SUM(r.amount) amount' + source + ' GROUP BY r.currency,r.status', tuple(values)):
            amounts.setdefault(row['currency'], {})[row['status']] = row['amount']
        return 'SELECT r.*' + source, values, order, amounts

    def get_refunds(self, tx, **args):
        target = args.get('target_ref') or ref('after_sale', args['after_sale_id'])
        query, values, order, amounts = self.refund_query(tx, target)
        return self.sql_page(tx, query, values, 'get_refunds', args, keys=('requested_at', 'refund_id'),
            data=dict(order_ref=ref('order', order['platform_code'], order['order_id']), amounts_by_currency_and_status=amounts,
                recorded_success_amount=amounts.get(order['currency'], {}).get('succeeded', Decimal(0))),
            limitations=['渠道成功不代表已核实银行卡入账'])
