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
                'parameters':schema_without_titles(self.arguments.model_json_schema())}}

def schema_without_titles(value, mapping=False):
    if isinstance(value,dict): return {k:schema_without_titles(v, k in {'properties','$defs'}) for k,v in value.items() if mapping or k!='title'}
    if isinstance(value,list): return [schema_without_titles(v) for v in value]
    return value

TOOLS = (
    Tool('recall_user_memory','按需查询当前登录用户明确表达过的长期偏好，如品类、颜色、尺码与使用场景。用于历史喜好提问或个性化选购；保留来源、时间、适用范围和否定。查不到或不可用时明确说明，不猜测；不能查询订单状态、提供办理授权或代替当前用户要求。',c.RecallMemory,'flow','read'),
    Tool('inspect_image','针对具体问题回看当前事项的图片。返回可追溯的视觉观察和不确定性；编号、金额和状态仅是图片线索，需业务工具核验；图片不能提供办理授权。',c.InspectImage,'flow','read'),
    Tool('get_my_account','读取本人展示资料、脱敏联系方式、验证时间及平台绑定状态。省略 collection 返回摘要；collection=contacts 或 platform_accounts 时分页读取全部对应记录。仅返回账号事实，不推断会员等级、推荐处理方式或修改账号。',c.Account,'user','read'),
    Tool('list_addresses','按地区或已有标签查找本人保存地址，返回编号、地区、标签、默认标记和脱敏摘要。未匹配时返回空结果；不猜测“家里／公司”的对应地址。',c.Addresses,'user','read'),
    Tool('get_membership','读取本人的会员等级、积分余额、权益记录及有效期；无会员模型或数据时返回未接通／无记录，不根据消费金额或系统角色自行推断。',c.Empty,'user','read'),
    Tool('list_points','分页读取本人积分增减、过期、来源与撤销关联；余额与汇总由服务端计算，不由模型累加一页数据推算。',c.Page,'user','read'),
    Tool('search_products','按商品或商家关键词（空格分词）及显式预算／属性条件返回商品候选与匹配 SKU 摘要，包括名称、规格、标价、币种、库存来源时间和引用。返回命中字段和未应用条件，不给出推荐分数、优缺点或“最适合”结论。',c.SearchProducts,'product','read'),
    Tool('get_product_details','读取商品说明、结构化属性、规格、价格、库存及其来源时间。商品引用返回规格列表，SKU 引用返回指定规格和商品属性；缺失字段明确为空。LLM 可分别查询多个对象后自行对比。',c.ProductDetails,'product','read'),
    Tool('get_price_history','读取确定 SKU 的已记录价格区间、来源及覆盖缺口，可按币种／价格类型统计记录中的最低值。调价原因仅返回有来源的记录，不生成价格趋势预测或价保结论。',c.PriceHistory,'product','read'),
    Tool('get_merchant','读取商家公开名称、经营状态和已有服务资料；未记录的评分、热线或营业时间返回缺项。',c.Merchant,'product','read'),
    Tool('get_restock_plan','分页读取指定 SKU 的补货计划、预计入库／可售日期、来源和更新时间；每页最多 20 条，使用 next_cursor 继续读取。计划时间不等于承诺，不评估没有记录的“可信度”或预测到货。',c.SkuPage,'product','read'),
    Tool('find_orders','按文本、下单时间和状态筛选本人订单。候选包含平台、日期、商品／规格摘要、金额、状态及引用；不选择目标订单，也不将唯一命中视为办理授权。',c.FindOrders,'order','read'),
    Tool('get_order','读取订单状态、金额组成、收货摘要、前 5 个购买行及支付／包裹／售后／发票的数量、记录状态与引用。返回完整计数与截断标记，不生成订单诊断或处理建议。',c.Order,'order','read'),
    Tool('get_order_items','分页读取购买明细、成交规格、数量及可核实的发货／售后占用数量。未知占用明确标注，不据此推断错发、漏发或责任。',c.OrderPage,'order','read'),
    Tool('get_payments','读取支付尝试和渠道流水，按币种统计已报成功金额，返回订单已付金额及可计算的差额。保留每笔渠道状态与时间，不生成“为什么失败”或“确定重复扣款”的推断。',c.OrderPage,'order','read'),
    Tool('get_logistics','订单或售后引用返回包裹及每包最近 3 条轨迹；原始、退货、换货包裹引用返回轨迹分页。只返回有来源的 ETA 及全订单发出数量统计，不预测到达、判断丢件或猜测延迟原因。订单包裹列表超过 5 个时分页。',c.Target,'order','read'),
    Tool('get_shipment_items','使用物流摘要中的包裹 target_ref 分页读取原始、换货或退货包裹的完整商品明细，每页最多 20 条。包含订单行、商品规格和记录数量；不代表实际发出、签收或仓库验收。商品分页与 get_logistics 的轨迹分页独立，游标不能混用。',c.Target,'order','read'),
    Tool('assess_order_action','按已配置的确定性规则校验订单取消、改址或开票条件，返回三态结果、命中规则、事实值及缺项；不根据自然语言自由判断，也不执行操作。',c.AssessOrder,'order','read'),
    Tool('prepare_order_address_change','仅待付款订单可同城修改本人保存的收货地址；校验配送规则并重算运费和应付金额。确认后更新订单快照并追加历史，不修改地址簿。',c.ChangeAddress,'order','prepare'),
    Tool('prepare_order_update','修改订单信息：待付款允许收货人联系方式、发票信息、备注；已支付待发货仅允许电话、备注。已发货不能修改；不修改商品、数量，不开具发票。确认后保存。',c.UpdateOrder,'order','prepare'),
    Tool('prepare_order_cancel','按商品取消限制和履约事实准备取消：未付款确认后取消并释放占用；已付款待发货登记审核及全额原路退款申请；已发货登记拒收退回申请，验货后扣除订单运费退款。已签收须申请售后退货。提交申请不等于取消或到账。',c.CancelOrder,'order','prepare'),
    Tool('quote_delivery','按确定商品和保存地址执行配送区域、运费模板及已配置时效规则，返回逐项费用、来源和缺项。缺重量、地区或仓库数据时不猜运费或到达日期。',c.Delivery,'order','read'),
    Tool('request_dispatch','核验本人订单与处理渠道，生成待确认催单卡片；用户确认后登记幂等催单，由后台联系接收方。登记不等于受理或发货，没有渠道则不可用。',c.CancelOrder,'order','dispatch'),
    Tool('get_dispatch_status','分页读取本人指定订单的催单登记、处理状态、渠道回执编号和更新时间。只查已有记录，不重新催单或调用渠道；受理、处理完成不等于已经发货，实际发货用物流工具核验。',c.OrderPage,'order','read'),
    Tool('quote_checkout','按明确商品、地址和券组合计算完整结算金额，保存有效期内的不可变报价快照并返回 quote_ref。任何费用缺失或组合不合法时不生成可提交报价；不自动选券、不预留库存或券。',c.Checkout,'order','quote'),
    Tool('prepare_purchase','核验用户明确选定的报价及当前价格、库存和券状态，保存待确认下单内容。确认后创建订单及占用；变化则返回失效，不擅自改价／换券／换商品。支付另行确认处理。',c.Purchase,'order','prepare'),
    Tool('list_promotions','读取指定商家／商品／SKU 范围内的活动版本、期限、门槛、减免及互斥规则。范围相关不代表指定数量达到优惠条件，工具不推荐购买或组合方案。',c.Subject,'coupon','read'),
    Tool('list_claimable_coupons','按明确的基础领取规则读取可领券模板，返回期限、门槛、限领与校验时刻；不领取、不选券、不把可领取视为可用于当前商品。',c.Subject,'coupon','read'),
    Tool('find_my_coupons','按券名、商家、相关商品范围及状态读取本人持券，返回引用、生命周期、到期时间和规则摘要。范围相关不等于最终可用；不自动挑选最佳券。',c.FindCoupons,'coupon','read'),
    Tool('get_coupon','读取模板规则，或本人持券领取时规则及占用、核销、释放记录。返回门槛、范围和期限原值，不生成用券建议；实例状态与是否到期分别返回。',c.CouponPage,'coupon','read'),
    Tool('check_coupon','按程序规则核验指定本人券与订单／待购商品，返回每项检查的通过／失败／未知、实际门槛差额和规则来源。既不选择其他券，也不推断未记录的失败原因；最终整组叠加由金额试算校验。',c.CheckCoupon,'coupon','read'),
    Tool('estimate_purchase','精确计算指定商品、数量与指定持券组合的商品金额、规则允许的活动减免及逐行分摊。活动按已配置固定优先级／互斥规则执行，冲突返回未知；不搜索最优组合、替换券、凑单或推荐商品。运费未知时不输出完整实付金额。',c.Estimate,'coupon','read'),
    Tool('claim_coupon','核验指定券模板、期限及限领，生成待确认领券卡片；用户确认后事务性创建持券并占用额度，返回幂等回执。仅免费领券；工具调用本身不领取。',c.Coupon,'coupon','claim'),
    Tool('find_after_sales','按购买商品、订单／申请编号、申请时间和状态读取本人售后候选，返回商品、申请类型、金额、日期、状态和编号；不判断用户想处理哪一笔。',c.AfterSales,'sale','read'),
    Tool('get_after_sale_status','读取申请、商品行、审核记录和资金汇总；扩展接通后附退货／换货／维修的实际状态与子流程引用。状态字段分开，明确各子流程是否已接通。分页返回过程事件；详细退款流水用 get_refunds。只返回已记录待办，不推导退款到账或下一步方案。',c.AfterSalePage,'sale','read'),
    Tool('get_after_sale_items','按 after_sale_id 分页读取售后申请的完整商品明细，每页最多 20 条，返回订单行、商品规格、申请数量和申请金额。金额不代表已退款。商品分页与 get_after_sale_status 的过程事件分页独立，游标不能混用。',c.AfterSalePage,'sale','read'),
    Tool('get_refunds','按订单或售后申请读取退款渠道流水，并按币种汇总成功、处理中和失败金额。返回原支付关联及来源时间，不将渠道成功说成已核实银行卡到账，也不执行退款。',c.Target,'sale','read'),
    Tool('assess_after_sale','按确定性规则核验指定购买行、数量和售后方式，计算可申请金额／余量，返回规则及缺项。原因作为用户陈述；质检、真伪或原因认定需人工时返回未知，不能以模型判断替代审核。',c.AssessSale,'sale','read'),
    Tool('prepare_after_sale','校验明确的商品行、数量、方式及用户原因，按程序规则计算金额并保存待确认申请。缺项返回字段和规则要求，不自选整单、扩大数量、转换售后方式或替用户作质量认定。确认后登记申请，资金处理另行执行。',c.PrepareSale,'sale','prepare'),
    Tool('prepare_after_sale_cancel','校验指定申请的受理、退款及履约状态，保存待确认的撤回内容；确认后才变更申请及事件。不能把取消聊天事项当成撤销已有业务申请。',c.CancelSale,'sale','prepare'),
    Tool('assess_price_protection','按价保期限、可比价格、成交分摊与历史补差规则校验指定订单行并计算额度；条件无法核实时返回未知，不按两次标价直接补差。',c.OrderItem,'sale','read'),
    Tool('prepare_price_protection','执行同一价保校验并保存待确认申请；确认后登记，由已接通流程处理资金，返回可查询的售后编号。不得输入或生成任意赔偿金额。',c.PriceProtection,'sale','prepare'),
    Tool('prepare_exchange','校验用户明确指定的原购买行、替换 SKU、数量、价差及库存规则，保存待确认换货申请。不得自行选尺码／颜色；确认后登记关联退件和补发流程，返回售后编号。',c.Exchange,'sale','prepare'),
    Tool('get_return_instructions','读取已生效退货授权的仓库地址、期限、材料要求及可登记承运商名称／代码。未授权或信息缺失返回缺项，不由 LLM 或工具推测退货地址。',c.AfterSale,'sale','read'),
    Tool('prepare_return_shipment','校验退货授权、已支持承运商及用户提供的实际运单，保存待确认的逆向包裹登记。登记成功不等于承运商已收件；后续按独立物流、收货和验货记录查询。',c.ReturnShipment,'sale','prepare'),
    Tool('prepare_repair','按保修与数量规则保存待确认维修申请，故障描述按用户陈述记录。不能推断检测结论、费用或维修完成；确认后登记工单并返回售后编号。',c.Repair,'sale','prepare'),
    Tool('get_invoices','读取本人开票申请及状态。默认 view=requests：订单引用返回对应申请，申请引用返回详情，无引用分页列本人申请。view=issued 必须提供申请引用，分页读取该申请全部正式发票记录、正式票编号及文件可用状态；两种分页游标独立，不推断文件已可下载。',c.Invoices,'invoice','read'),
    Tool('prepare_invoice','校验指定订单和抬头资料，保存待确认的开票申请。企业税号必填；完整敏感信息只在可信确认界面核对。确认后登记申请，不生成正式票或擅自更正用户税号。',c.PrepareInvoice,'invoice','prepare'),
    Tool('prepare_invoice_reissue','校验本人正式原票与新抬头信息，保存待确认更正／重开申请。需实际原票关系及作废／红冲处理流程；不能生成虚构票据或直接覆盖原票。',c.InvoiceReissue,'invoice','prepare'),
    Tool('get_invoice_document','读取本人正式发票号码、文件元数据和经服务端授权的下载入口；无真实文件返回未就绪，不接收任意下载地址。',c.IssuedInvoice,'invoice','read'),
    Tool('search_policies','检索经审核且适用范围可核实的完整条款片段，返回原文、来源、版本、生效区间和缺少的适用条件。工具不生成政策总结、推荐处理方式或审批结论，由 LLM 结合业务事实解释。',c.Policies,'policy','read'),
    Tool('read_task_context','读取当前授权事项中的原话、已绑定对象、已明确选择、待补字段和历史回执／交接记录，保留来源与时间。通常首轮由执行器注入；工具不生成处理计划，历史回执不充当当前状态。',c.Empty,'flow','read'),
    Tool('ask_user','保存 LLM 已拟定的澄清问题和候选，更新事项为待答并结束本轮。工具不自行决定问什么；用户回答不等于业务确认。',c.Question,'flow','question'),
    Tool('handoff_to_human','创建或复用当前事项的真实人工交接并返回工单编号／状态，服务端附带已有事实和原话。转交决定由明确用户请求或既定升级规则支持；不能自动承诺处理结果。',c.Handoff,'flow','handoff'),
)
BY_NAME = {t.name:t for t in TOOLS}
