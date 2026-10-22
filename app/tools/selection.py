"""Small tool bundles. Routing narrows discovery; service ownership checks still authorize access."""
from app.tools.catalog import TOOLS

def for_task(intent,goal=''):
    value=getattr(intent,'value',str(intent))
    domains={'flow','policy'}
    groups={
        'product':('product','inventory','price_history','price_comparison'),
        'coupon':('coupon','promotion','cart_optimization'),
        'order':('order','logistics','delivery','payment','expedite','address_change'),
        'sale':('refund','after_sale','exchange','price_protection','issue_report','complaint'),
        'invoice':('invoice',),
        'user':('profile','account','address_list'),
    }
    for domain,tokens in groups.items():
        if any(t in value for t in tokens): domains.add(domain)
    if domains=={'flow','policy'}:
        for token,domain in [('订单','order'),('快递','order'),('支付','order'),('退款','sale'),('退货','sale'),('优惠','coupon'),('券','coupon'),('商品','product'),('价格','product'),('多少钱','product'),('推荐','product'),('规格','product'),('库存','product'),('发票','invoice'),('账号','user'),('个人资料','user'),('地址','user')]:
            if token in goal: domains.add(domain)
    if 'sale' in domains or 'invoice' in domains: domains.add('order')
    if 'coupon' in domains: domains.update({'product','order'})
    if 'product' in domains: domains.add('coupon')
    if 'order' in domains: domains.add('user')
    return frozenset(t.name for t in TOOLS if t.domain in domains)
