# 文件说明：为资源摘要提供完整商品行查询，逐页校验归属和查询范围。
"""Complete, owner-checked item collections behind compact resource summaries."""
from app.services.commerce.common import BusinessError, decoded, ref, unref


def items_query(tool, **arguments):
    """Executable first-page entry point, separate from a parent event cursor."""
    return {'tool': tool, 'arguments': arguments}


class ItemQueryService:
    def item_collection(self, tx, source, values, order, tool, args, *, data, invalid=None):
        # Broken links must not silently hide rows or disclose another order's items.
        scope = 'i.platform_code=%s AND i.order_id=%s'
        if tx.one('SELECT 1 invalid' + source + ' AND (i.order_item_id IS NULL OR NOT (' + scope + ')) LIMIT 1',
                  tuple(values) + (order['platform_code'], order['order_id'])):
            raise BusinessError('insufficient_data', '商品明细归属不一致')
        if invalid and tx.one('SELECT 1 invalid' + source + ' AND ' + invalid[0] + ' LIMIT 1',
                              tuple(values) + tuple(invalid[1])):
            raise BusinessError('insufficient_data', '包裹与售后明细归属不一致')
        result = self.sql_page(tx, 'SELECT s.*,i.sku_id,i.product_id,i.title_snapshot,i.spec_snapshot' + source,
            values, tool, args, keys=('order_item_id',), data={**data,
                'order_ref': ref('order', order['platform_code'], order['order_id']),
                'platform_code': order['platform_code'], 'order_id': order['order_id']})
        for item in result['data']['items']:
            item['spec_snapshot'] = decoded(item['spec_snapshot'])
            if 'actual_spec_snapshot' in item:
                item['actual_spec_snapshot'] = decoded(item['actual_spec_snapshot'])
        return result

    def get_after_sale_items(self, tx, **args):
        sale, order = self.sale(tx, args['after_sale_id'])
        return self.item_collection(tx,
            ' FROM after_sale_items s LEFT JOIN order_items i ON i.order_item_id=s.order_item_id WHERE s.after_sale_id=%s',
            [sale['after_sale_id']], order, 'get_after_sale_items', args,
            data={'after_sale_id': sale['after_sale_id']})

    def get_shipment_items(self, tx, **args):
        kind, ident = unref(args['target_ref'], {'shipment', 'return_shipment'})
        reverse = kind == 'return_shipment'
        key = kind + '_id'
        ship = tx.one('SELECT * FROM ' + kind + 's WHERE ' + key + '=%s', (ident,))
        if not ship:
            raise BusinessError('not_found', '包裹不存在')
        if reverse:
            auth = tx.one('SELECT after_sale_id FROM return_authorizations WHERE authorization_id=%s', (ship['authorization_id'],))
            if not auth:
                raise BusinessError('insufficient_data', '退货包裹缺少售后归属')
            sale, order = self.sale(tx, auth['after_sale_id'])
            ship = dict(ship, after_sale_id=sale['after_sale_id'])
        else:
            order = self.owned_order(tx, ref('order', ship['platform_code'], ship['order_id']))
        sale_id = ship.get('after_sale_id')
        invalid = None
        if reverse:
            invalid = ('s.after_sale_id<>%s', [sale_id])
        elif ship.get('shipment_kind') == 'exchange':
            sale, sale_order = self.sale(tx, sale_id)
            if (sale_order['platform_code'], sale_order['order_id']) != (order['platform_code'], order['order_id']):
                raise BusinessError('insufficient_data', '换货包裹订单归属不一致')
            invalid = ('NOT EXISTS (SELECT 1 FROM after_sale_items a WHERE a.after_sale_id=%s AND a.order_item_id=s.order_item_id)', [sale_id])
        return self.item_collection(tx,
            ' FROM ' + kind + '_items s LEFT JOIN order_items i ON i.order_item_id=s.order_item_id WHERE s.' + key + '=%s',
            [ident], order, 'get_shipment_items', args,
            data={'target_ref': args['target_ref'], key: ident, 'after_sale_id': sale_id}, invalid=invalid)
