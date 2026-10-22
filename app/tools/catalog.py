"""Explicit tool metadata, shared by local execution and MCP."""
from dataclasses import dataclass
from app.tools import contracts as c

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: type[c.Input]
    domain: str
    effect: str = 'read'

    @property
    def task_only(self):
        return self.effect != 'read' or self.domain == 'flow'

    def definition(self):
        return {'type':'function','function':{'name':self.name,'description':self.description,
                'parameters':self.arguments.model_json_schema()}}

TOOLS = (
    Tool('get_my_profile','查询本人展示资料、脱敏联系方式与验证状态。',c.Empty,'user'),
    Tool('list_addresses','分页列出本人未删除的收货地址摘要。',c.Page,'user'),
    Tool('list_platform_accounts','查询本人已验证的平台绑定，不能建立绑定或扩大权限。',c.Empty,'user'),
    Tool('search_products','检索商品；预算按实际 SKU 标价筛选，返回已应用和未应用条件。',c.SearchProducts,'product'),
    Tool('get_product','查询商品说明及分页规格目录。',c.Product,'product'),
    Tool('get_sku','查询确定规格的价格、销售状态和库存；查询不锁库存。',c.Sku,'product'),
    Tool('compare_skus','对比 2–5 个已知规格的价格、属性、库存和商家事实。',c.Compare,'product'),
    Tool('get_price_history','查询本地已记录的历史价格及覆盖缺口，不代表全网最低价。',c.PriceHistory,'product'),
    Tool('get_merchant','查询商家公开资料。',c.Merchant,'product'),
    Tool('find_orders','按关键词、状态和下单日期定位本人订单；多候选须澄清。',c.FindOrders,'order'),
    Tool('get_order','查询订单金额和收货摘要及关联业务引用。',c.Order,'order'),
    Tool('get_order_items','分页读取购买明细与成交快照。',c.OrderPage,'order'),
    Tool('get_payments','读取支付尝试及成功金额汇总；多次尝试不等于重复扣款。',c.OrderPage,'order'),
    Tool('get_shipments','核对订单拆包、已发出商品数量；无履约依据时不推断待发货量。',c.OrderPage,'order'),
    Tool('get_shipment_tracking','查询本人包裹的正向物流事件。',c.Shipment,'order'),
    Tool('assess_order_action','只读评估订单取消、改址或开票资格，缺少规则返回未知。',c.AssessOrder,'order'),
    Tool('list_promotions','分页查询活动规则及适用范围完整性。',c.Subject,'coupon'),
    Tool('list_claimable_coupons','按规则查询可领取的券模板；不领取。仅接受商家、商品、SKU 范围。',c.Subject,'coupon'),
    Tool('list_my_coupons','分页查询本人持券，区分生命周期状态与到期状态。',c.Wallet,'coupon'),
    Tool('get_coupon','查询券模板或本人持券规则、范围和核销记录。',c.CouponPage,'coupon'),
    Tool('check_coupon','核验本人一张券与已有订单的适用关系；不改价、不核销。',c.CheckCoupon,'coupon'),
    Tool('estimate_purchase','试算一组明确的待购商品和券组合；不下单、不锁券，运费未知不报完整实付。',c.Estimate,'coupon'),
    Tool('list_after_sales','定位本人的售后申请。',c.AfterSales,'sale'),
    Tool('get_after_sale','查询售后申请及分页商品行，申请状态不等于资金到账。',c.AfterSalePage,'sale'),
    Tool('get_refunds','查询售后退款流水，渠道成功不等于已核实银行卡到账。',c.AfterSalePage,'sale'),
    Tool('assess_after_sale','只读核验指定商品、数量和方式的售后资格与金额；不创建申请。',c.AssessSale,'sale'),
    Tool('list_invoice_requests','分页定位本人开票申请。',c.InvoiceList,'invoice'),
    Tool('get_invoice_request','查询发票申请及脱敏抬头，不能生成正式票据。',c.Invoice,'invoice'),
    Tool('search_policies','检索匹配业务对象的有效政策及版本依据。',c.Policies,'policy'),
    Tool('claim_coupon','用户明确要求领取指定券后实际领券；不得因咨询有什么券而领取。',c.Coupon,'coupon','claim'),
    Tool('prepare_after_sale','用户要求申请后准备仅退款或退货退款，前端确认后才创建申请。',c.PrepareSale,'sale','prepare'),
    Tool('prepare_invoice','准备开票申请，前端确认后提交；企业税号必填。',c.PrepareInvoice,'invoice','prepare'),
    Tool('prepare_order_address_change','准备将订单改用本人已保存的地址，前端确认后生效。',c.ChangeAddress,'order','prepare'),
    Tool('prepare_order_cancel','准备取消本系统管理的未支付未履约订单，前端确认后提交。',c.CancelOrder,'order','prepare'),
    Tool('prepare_after_sale_cancel','准备撤回尚可撤回的售后，前端确认后提交。',c.CancelSale,'sale','prepare'),
    Tool('read_task_context','读取当前授权事项的原话、已核实对象和真实进度。',c.Empty,'flow'),
    Tool('ask_user','发布一个澄清问题后暂停本轮执行；不是业务确认。',c.Question,'flow','question'),
    Tool('handoff_to_human','为当前事项创建或复用真实人工交接。',c.Handoff,'flow','handoff'),
)
BY_NAME = {t.name:t for t in TOOLS}
