"""Global tool allowlist intersected with task profiles, never business authority."""
import re

from app.tools.catalog import BY_NAME

def names(value):
    return frozenset(value.split())


COMMON = names('read_task_context ask_user handoff_to_human search_policies inspect_image recall_user_memory')
ORDER = names('find_orders get_order get_order_items')
PRODUCT = names('search_products get_product_details get_merchant')
SALE = ORDER | names('find_after_sales get_after_sale_status get_refunds get_logistics')
COUPON = PRODUCT | ORDER | names('list_promotions list_claimable_coupons find_my_coupons get_coupon check_coupon estimate_purchase')

TASK_TOOLS = {
    'product': PRODUCT,
    'inventory': PRODUCT | names('get_restock_plan'),
    'price': PRODUCT | names('get_price_history estimate_purchase list_promotions'),
    'delivery': PRODUCT | names('list_addresses quote_delivery'),
    'coupon': COUPON | names('claim_coupon'),
    'purchase': COUPON | names('list_addresses quote_delivery quote_checkout prepare_purchase'),
    'order': ORDER | names('get_payments get_logistics find_after_sales get_after_sale_status get_invoices'),
    'payment': ORDER | names('get_payments get_refunds'),
    'logistics': ORDER | names('get_logistics find_after_sales get_after_sale_status'),
    'address': ORDER | names('list_addresses assess_order_action prepare_order_address_change prepare_order_update'),
    'order_update': ORDER | names('assess_order_action prepare_order_update'),
    'cancel': SALE | names('assess_order_action prepare_order_cancel assess_after_sale prepare_after_sale'),
    'dispatch': ORDER | names('get_logistics get_dispatch_status request_dispatch'),
    'after_sale': SALE | names('assess_after_sale prepare_after_sale prepare_after_sale_cancel get_return_instructions prepare_return_shipment'),
    'refund_status': SALE,
    'exchange': SALE | PRODUCT | names('assess_after_sale prepare_exchange get_return_instructions prepare_return_shipment'),
    'repair': SALE | names('assess_after_sale prepare_repair get_return_instructions prepare_return_shipment'),
    'price_protection': ORDER | PRODUCT | names('get_price_history find_after_sales get_after_sale_status assess_price_protection prepare_price_protection'),
    'invoice': ORDER | names('assess_order_action get_invoices prepare_invoice prepare_invoice_reissue get_invoice_document'),
    'account': names('get_my_account list_addresses get_membership list_points'),
    'policy': frozenset(),
    'conversation': frozenset(),
    'discovery': PRODUCT | ORDER | names('get_my_account get_membership list_points find_after_sales get_after_sale_status get_invoices'),
}

# Keep summary-to-detail navigation usable in every relevant task profile.
# The global allowlist remains authoritative and is applied by for_task below.
for _profile, _tools in TASK_TOOLS.items():
    if _tools & {'get_logistics', 'get_after_sale_status'}:
        _tools = _tools | {'get_shipment_items'}
    if _tools & {'find_after_sales', 'get_after_sale_status'}:
        _tools = _tools | {'get_after_sale_items'}
    TASK_TOOLS[_profile] = frozenset(_tools)

# Specialized engines can supply these labels. The live worker uses "others",
# for which the current task goal, not the latest short answer, is used.
INTENT_PROFILES = {
    'product_comparison': ('product',), 'product_inquiry': ('product',),
    'product_recommendation': ('product',), 'inventory_inquiry': ('inventory',),
    'delivery_inquiry': ('delivery',), 'service_policy_inquiry': ('policy',),
    'promotion_inquiry': ('coupon',), 'promotion_calculation': ('coupon',),
    'coupon_issue': ('coupon',), 'price_comparison': ('price',),
    'cart_optimization': ('purchase',), 'price_history': ('price',),
    'membership_inquiry': ('account',), 'payment_issue': ('payment',),
    'refund_service': ('after_sale',), 'exchange_service': ('exchange',),
    'price_protection_service': ('price_protection',), 'after_sale_status': ('refund_status',),
    'invoice_inquiry': ('invoice',), 'invoice_request': ('invoice',),
    'human_handoff': ('conversation',), 'order_inquiry': ('order',),
    'logistics_inquiry': ('logistics',), 'address_change': ('address',),
    'order_cancel': ('cancel',), 'expedite': ('dispatch',),
    'after_sale_request': ('after_sale', 'exchange', 'repair', 'price_protection'),
    'refund_status': ('refund_status',), 'issue_report': ('after_sale', 'repair'),
    'invoice': ('invoice',), 'complaint': ('order', 'refund_status'),
    'greetings': ('conversation',), 'closure': ('conversation',),
}

# Match multiple domains for compound goals. These are exposure hints, including
# negated requests; existing user-authorization/confirmation rules still apply.
GOAL_RULES = {
    'inventory': r'库存|补货|现货|有货|缺货|stock|inventory',
    'price': r'价格|多少钱|比价|历史价|降价|price|cost',
    'delivery': r'运费|配送范围|配送费|送到.{0,8}(吗|不)|shipping fee|delivery fee',
    'coupon': r'优惠|促销|活动|券|满减|折扣|coupon|promotion|discount',
    'purchase': r'购买|下单|选购|购物车|结算|买|purchase|checkout|buy',
    'payment': r'支付|付款|扣款|payment|charged',
    'logistics': r'物流|快递|包裹|运单|发货|签收|什么时候到|到哪|何时到|tracking|logistics|shipment|delivery',
    'address': r'地址|改址|收货人|address',
    'order_update': r'备注|电话|联系方式|order update',
    'cancel': r'取消订单|取消.{0,8}(订单|这单)|撤单|cancel.{0,12}order',
    'dispatch': r'催|加急|尽快发|expedite|dispatch',
    'after_sale': r'退货|退款|售后|撤销申请|撤回申请|refund|return|after.sale',
    'exchange': r'换货|更换|换.{0,6}(码|色)|exchange|replace',
    'repair': r'维修|保修|修理|故障|repair|warranty',
    'price_protection': r'价保|价格保护|补差|price protection',
    'invoice': r'发票|开票|重开|红冲|invoice',
    'account': r'账号|账户|会员|积分|地址簿|account|membership|points',
}


def task_profiles(intent=None, goal=''):
    """Prefer the current goal so a changed task cannot retain a stale intent."""
    text = goal.casefold()
    profiles = {key for key, pattern in GOAL_RULES.items() if re.search(pattern, text)}
    # Status-only requests need no refund-preparation schemas.
    if 'after_sale' in profiles and re.search(r'进度|状态|到账|status', text) and not re.search(r'申请|办理|撤销|撤回|退货', text):
        profiles.remove('after_sale')
        profiles.add('refund_status')
    # Generic subjects can be independent parts of a compound goal. Preserve
    # them even when another domain matched, without broadening profiles that
    # already contain the corresponding discovery dependencies.
    selected = frozenset().union(*(TASK_TOOLS[key] for key in profiles))
    if re.search(r'订单|这单|\border\b', text) and not ORDER <= selected:
        profiles.add('order')
    if re.search(r'商品|推荐|对比|材质|尺码|颜色|\bproduct\b|\brecommend\b|\bcompare\b', text) and not PRODUCT <= selected:
        profiles.add('product')
    if profiles:
        return frozenset(profiles)
    if re.search(r'订单|这单|order', text):
        return frozenset({'order'})
    if re.search(r'商品|推荐|对比|材质|尺码|颜色|product|recommend|compare', text):
        return frozenset({'product'})
    if re.search(r'政策|规则|条款|policy', text):
        return frozenset({'policy'})
    label = getattr(intent, 'value', intent)
    return frozenset(INTENT_PROFILES.get(label, ('discovery',)))


def parse_allowlist(value):
    """'*' enables registered tools; an empty value deliberately enables none."""
    if value.strip() == '*':
        return frozenset(BY_NAME)
    selected = frozenset(part.strip() for part in value.split(',') if part.strip())
    unknown = selected - BY_NAME.keys()
    if unknown:
        raise ValueError('TOOL_ALLOWLIST contains unknown tools: ' + ', '.join(sorted(unknown)))
    return selected


def global_tools():
    from app.config.settings import settings
    return parse_allowlist(settings.tool_allowlist)


def for_task(intent=None, goal=''):
    selected = COMMON.union(*(TASK_TOOLS[key] for key in task_profiles(intent, goal)))
    return selected & global_tools()
