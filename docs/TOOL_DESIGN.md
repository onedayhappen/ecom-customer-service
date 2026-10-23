# 客服工具设计

更新日期：2026-10-23。**本文件为待落地的调整方案，不是当前已注册工具清单。**

目标目录共 **48 个工具：33 个基础工具、15 个扩展工具**。现有 38 个核心工具的实现需要按本方案调整；扩展能力仍需补齐数据和处理流程。ReAct 调用链评审见 [评审说明](TOOL_REACT_REVIEW.md)，现有实现配置见 [实现说明](TOOL_IMPLEMENTATION.md)。

## 1. 工具职责与参数约定

LLM 负责理解用户表达、选择查询对象与比较维度、基于证据分析和组织回复。工具负责取得模型没有的数据、执行确定的业务规则或计算、产生受控的系统操作。工具描述须明确可观测结果，不承担泛化的推荐、分析、解释或下一步规划。

读取工具可以联查关联数据、做全量统计及返回最新记录；这些是固定的数据处理。商品推荐、优缺点归纳、是否需要继续查询等由 LLM 根据返回事实判断。金额分摊、优惠叠加、余额／数量约束及规则资格由程序精确核验，不能交给 LLM 猜算。

- 模型根据对话和工具结果选择下一步；工具可见范围由登录权限、可信事项上下文和已接通能力决定，不依赖预设分类或关键词路由。
- `?` 表示可选，其余必填。参数包含类型、用途及约束；不以一个自由文本或任意 JSON 代替明确业务参数。
- 用户身份、当前事项、权限、当前时间、幂等编号及确认凭证由服务端提供，模型不能填写。
- `query` 仅作为文本／语义检索线索，返回命中字段、相关片段及实际应用的条件，不执行隐藏的业务规划。“适合通勤”等用户偏好由 LLM 拆为检索线索、明确约束及待比较属性；无法验证的条件必须说明。精确的金额、时间、状态采用结构化参数。
- `*_ref` 是工具返回的带类型引用。不得编造引用，也不隐式使用会话中最后一笔订单。引用不是权限凭证；每次访问均检查归属。
- 返回候选时同时提供可展示标签和下一步可用的引用。多候选不能默认选第一条；读取结果唯一匹配也不代表用户授权办理。
- `cursor?: string` 原样使用同一工具上一页返回的游标，不得更改查询条件。默认 10 条候选／20 条明细；返回总数或明确未知，以及是否还有下一页。
- 查询固定的、有界的结构化事实，不在工具内部生成回答或推理报告。摘要包含关联对象引用；记录超限时分页。累计金额和数量基于完整授权记录统计，不能由模型累加当前页后当作总额。
- 查询结果返回 `ok / no_match / unavailable`、候选列表及数量、覆盖范围和来源时间；缺字段通过 `missing_fields`、缺规则通过 `limitations` 明示。是否需要用户选择由 LLM 结合当前对话判断；服务端阻止未明确绑定对象的提交。规则校验另返回 `eligible / ineligible / unknown` 与规则编号和事实依据。
- `prepare_*` 在用户明确要求办理时调用，内部完成所需预检并生成确认卡片。先前的只读评估不是必经步骤，也不能免除提交时复核。用户只咨询资格或金额时只调用只读评估。
- 普通解释、商品比较和推荐由 LLM 直接回复；没有对应副作用时不设计专用工具。`ask_user` 的保留理由是保存待答状态并暂停事项；`handoff_to_human` 的保留理由是实际建立交接。模型不能调用确认或支付工具。
- 不以减少调用次数为唯一目标。两个独立商品详情可并行查询，不必包装成“比较工具”。相同参数的有效查询结果可复用；需要当前库存／价格或状态发生变化时重新获取。
- 输入默认值必须保守明确：省略优惠券等同不用券，不能隐式替用户选择、领取或替换权益。领域专属确定性优化以后确有需求时单独定义目标和约束，不藏在普通查询／试算里。

### 1.1 复合类型与引用

| 类型／参数 | 字段与含义 | 约束 |
| --- | --- | --- |
| `Money` | `amount: string` 金额；`currency: string` 币种 | 均必填；非负十进制、最多两位小数；币种为三位大写代码，不自动换汇 |
| `DateRange` | `from: string` 开始；`to: string` 结束 | 均必填，带时区 ISO 8601；左闭右开，跨度不超过 366 天。相对日期使用服务端提供的当前时间和时区换算，结果回显实际区间 |
| `PurchaseItem` | `sku_id: string` 规格；`quantity: integer` 数量 | 均必填；1–20 行，数量 1–999；同商家、同币种；重复 SKU 合并校验 |
| `AfterSaleItem` | `order_item_id: string` 购买行；`quantity: integer` 数量 | 均必填；同一订单的 1–20 行，数量 1–999，禁止重复行，不超过可办理余量 |
| `PurchaseTarget` | `order_ref: string` 或 `items: array<PurchaseItem>` | 严格二选一；用于检查指定券适用于已有未支付订单还是待购商品，不能同时传两者 |
| `Spec / specs` | 单个 `Spec` 为 `{name: string, value: string}`；`specs` 为 `array<Spec>` | 最多 5 个属性条件，如颜色、尺码；只接受商品数据可验证的属性，不执行表达式；未知属性回显为未应用 |
| `Choice / choices` | `array<{label: string, ref?: string}>` | 2–5 项，label 1–100 字且不重复；对象选项的 ref 必须来自当前授权查询，自由回答仍可用；不把选项序号当业务主键 |
| `order_ref` | 平台与订单号组成的服务端引用 | `find_orders` 返回；已有用户订单号也须在本人订单范围内定位 |
| `product_ref / sku_ref` | 商品或确定规格引用 | 搜索／商品详情返回；商品尚未选定规格时不得假定默认 SKU |
| `sku_id / merchant_id / order_item_id` | SKU／商家／购买行编号 | 对应搜索、详情或订单结果提供；需同时返回后续工具使用的带类型引用，不能由模型拼接 |
| `subject_ref` | 对应工具允许的商家／商品／SKU／订单引用 | 由授权查询返回；每个工具允许的范围以其参数描述为准 |
| `coupon_ref` | 券模板或本人持券引用 | 返回中明确 `template / instance`；查询均可接受，领券仅模板、用券仅实例 |
| `target_ref` | 各工具描述中列明的订单、商品、SKU、包裹、售后申请或发票申请引用 | 不允许任意对象；类型与归属由服务端验证 |
| `after_sale_id / invoice_id / issued_invoice_id` | 售后申请／开票申请／正式发票编号 | 查询结果返回，并分别绑定本人订单 |
| `address_id` | 本人保存的地址编号 | 十进制字符串；不能用自由文本地址冒充已保存地址 |
| `quote_ref` | 结算工具生成的报价引用 | 绑定本人、商品、地址、券及有效期；下单时重新检查竞争与状态变化 |

普通取消／撤回不为填写可选原因额外追问；特定业务规则确实要求原因时返回缺项，才向用户补充询问。

### 1.2 通用值域

- `query`：1–200 字；订单／售后／券检索线索可省略，商品搜索和政策搜索必填。
- `reason`：1–255 字，用户给出的原因，模型不得编造。
- `days`：1–365，默认 30。
- 订单 `status`：`all / pending_payment / paid / fulfilling / shipped / completed / cancelled / closed / unknown`。
- 售后 `status`：`all / pending_review / approved / rejected / processing / completed / cancelled / closed / unknown`。
- 持券 `status`：`all / available / reserved / used / expired / invalid / unknown`。
- 上述状态筛选默认 `all`。数据库状态未映射时返回 `unknown` 和来源值，不强行认定成功。
- `invoice_type`：`personal / company`，仅表示个人／企业抬头；`title` 为 1–200 字，`tax_number` 为 1–64 字且企业必填，不代表专用票／普通票分类。

## 2. 基础工具：33 个（目标接口）

### 2.1 账号与地址

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `get_my_account` | 读取本人展示资料、脱敏联系方式、验证时间及平台绑定状态。仅返回账号事实，不推断会员等级、推荐处理方式或修改账号。 | 无 |
| `list_addresses` | 按地区或已有标签查找本人保存地址，返回编号、地区、标签、默认标记和脱敏摘要。未匹配时返回空结果；不猜测“家里／公司”的对应地址。 | `query?: string`，地区或已有地址标签；`cursor?: string` |

### 2.2 商品数据

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `search_products` | 按检索词及显式预算／属性条件返回商品候选与匹配 SKU 摘要，包括名称、规格、标价、币种、库存来源时间和引用。返回命中字段和未应用条件，不给出推荐分数、优缺点或“最适合”结论。 | `query: string`，名称／品类／描述检索词；`max_price?: Money`，SKU 标价上限；`specs?: array<Spec>`，精确属性条件；`cursor?: string` |
| `get_product_details` | 读取商品说明、结构化属性、规格、价格、库存及其来源时间。商品引用返回规格列表，SKU 引用返回指定规格和商品属性；缺失字段明确为空。LLM 可分别查询多个对象后自行对比。 | `target_ref: string`，商品或 SKU 引用；`specs?: array<Spec>`，商品内的规格筛选；`cursor?: string`，规格下一页 |
| `get_price_history` | 读取确定 SKU 的已记录价格区间、来源及覆盖缺口，可按币种／价格类型统计记录中的最低值。调价原因仅返回有来源的记录，不生成价格趋势预测或价保结论。 | `sku_id: string`，规格编号；`days?: integer`，最近天数；`cursor?: string` |
| `get_merchant` | 读取商家公开名称、经营状态和已有服务资料；未记录的评分、热线或营业时间返回缺项。 | `merchant_id: string`，商家编号 |

### 2.3 订单、支付与配送

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `find_orders` | 按文本、下单时间和状态筛选本人订单。候选包含平台、日期、商品／规格摘要、金额、状态及引用；不选择目标订单，也不将唯一命中视为办理授权。 | `query?: string`，订单号或购买商品名称；`created_range?: DateRange`，下单区间；`status?: enum`，订单状态；`cursor?: string` |
| `get_order` | 读取订单状态、金额组成、收货摘要、前 5 个购买行及支付／包裹／售后／发票的数量、记录状态与引用。返回完整计数与截断标记，不生成订单诊断或处理建议。 | `order_ref: string`，已定位的订单引用 |
| `get_order_items` | 分页读取购买明细、成交规格、数量及可核实的发货／售后占用数量。未知占用明确标注，不据此推断错发、漏发或责任。 | `order_ref: string`；`cursor?: string`，商品明细下一页 |
| `get_payments` | 读取支付尝试和渠道流水，按币种统计已报成功金额，返回订单已付金额及可计算的差额。保留每笔渠道状态与时间，不生成“为什么失败”或“确定重复扣款”的推断。 | `order_ref: string`；`cursor?: string`，流水下一页 |
| `get_logistics` | 订单引用返回包裹、商品数量和每包最近 3 条轨迹；包裹引用返回轨迹分页。只返回有来源的 ETA 及全订单发出数量统计，不预测到达、判断丢件或猜测延迟原因。订单包裹列表超过 5 个时分页。 | `target_ref: string`，订单或包裹引用；`cursor?: string`，对应包裹列表／轨迹下一页 |
| `assess_order_action` | 按已配置的确定性规则校验订单取消、改址或开票条件，返回三态结果、命中规则、事实值及缺项；不根据自然语言自由判断，也不执行操作。 | `order_ref: string`；`action: enum`，`cancel_order / change_address / invoice` |

### 2.4 优惠查询与购买试算

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `list_promotions` | 读取指定商家／商品／SKU 范围内的活动版本、期限、门槛、减免及互斥规则。范围相关不代表指定数量达到优惠条件，工具不推荐购买或组合方案。 | `subject_ref?: string`，商家／商品／SKU 引用；`cursor?: string` |
| `list_claimable_coupons` | 按明确的基础领取规则读取可领券模板，返回期限、门槛、限领与校验时刻；不领取、不选券、不把可领取视为可用于当前商品。 | `subject_ref?: string`，商家／商品／SKU 引用；`cursor?: string` |
| `find_my_coupons` | 按券名、商家、相关商品范围及状态读取本人持券，返回引用、生命周期、到期时间和规则摘要。范围相关不等于最终可用；不自动挑选最佳券。 | `query?: string`，券名或相关商家名称；`subject_ref?: string`，商家／商品／SKU 引用；`status?: enum`，持券状态；`cursor?: string` |
| `get_coupon` | 读取模板规则，或本人持券领取时规则及占用、核销、释放记录。返回门槛、范围和期限原值，不生成用券建议；实例状态与是否到期分别返回。 | `coupon_ref: string`，券模板或本人券实例引用；`cursor?: string`，范围或历史下一页 |
| `check_coupon` | 按程序规则核验指定本人券与订单／待购商品，返回每项检查的通过／失败／未知、实际门槛差额和规则来源。既不选择其他券，也不推断未记录的失败原因；最终整组叠加由金额试算校验。 | `coupon_ref: string`，本人券实例；`target: PurchaseTarget`，订单或待购商品二选一 |
| `estimate_purchase` | 精确计算指定商品、数量与指定持券组合的商品金额、规则允许的活动减免及逐行分摊。活动按已配置固定优先级／互斥规则执行，冲突返回未知；不搜索最优组合、替换券、凑单或推荐商品。运费未知时不输出完整实付金额。 | `items: array<PurchaseItem>`；`coupon_refs?: array<string>`，0–5 张本人券实例，禁止重复；省略或空数组均表示不用券，非空表示严格核验该组券 |

### 2.5 售后与退款

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `find_after_sales` | 按购买商品、订单／申请编号、申请时间和状态读取本人售后候选，返回商品、申请类型、金额、日期、状态和编号；不判断用户想处理哪一笔。 | `query?: string`，商品名、订单号或申请号；`order_ref?: string`，限定订单；`created_range?: DateRange`，申请时间区间；`status?: enum`，售后状态；`cursor?: string` |
| `get_after_sale_status` | 读取申请、商品行、审核记录和资金汇总；扩展接通后附退货／换货／维修的实际状态与子流程引用。状态字段分开，明确各子流程是否已接通。分页返回过程事件；详细退款流水用 get_refunds。只返回已记录待办，不推导退款到账或下一步方案。 | `after_sale_id: string`；`cursor?: string`，申请处理事件下一页 |
| `get_refunds` | 按订单或售后申请读取退款渠道流水，并按币种汇总成功、处理中和失败金额。返回原支付关联及来源时间，不将渠道成功说成已核实银行卡到账，也不执行退款。 | `target_ref: string`，订单或售后申请引用；`cursor?: string`，流水下一页 |
| `assess_after_sale` | 按确定性规则核验指定购买行、数量和售后方式，计算可申请金额／余量，返回规则及缺项。原因作为用户陈述；质检、真伪或原因认定需人工时返回未知，不能以模型判断替代审核。 | `kind: enum`，`refund_only / return_refund`；`items: array<AfterSaleItem>`；`reason?: string`，用户已说明的原因，不能编造 |

### 2.6 发票与政策

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `get_invoices` | 读取本人开票申请及状态。订单引用返回对应申请，申请引用返回详情，无引用分页列本人申请；扩展接通后附正式票引用与文件可用状态，不推断已经开票。 | `target_ref?: string`，订单或开票申请引用；`cursor?: string`，申请列表下一页 |
| `search_policies` | 检索经审核且适用范围可核实的完整条款片段，返回原文、来源、版本、生效区间和缺少的适用条件。工具不生成政策总结、推荐处理方式或审批结论，由 LLM 结合业务事实解释。 | `query: string`，需要核实的政策问题；`subject_ref?: string`，订单／商品／SKU 引用 |

### 2.7 业务准备与领取

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `claim_coupon` | 在明确领取请求下核验模板、期限及限领，事务性创建本人持券并返回幂等回执。仅免费且无额外交换条件的领券；不能因为发现可领券自动执行。 | `coupon_ref: string`，仅接受模板引用 |
| `prepare_after_sale` | 校验明确的商品行、数量、方式及用户原因，按程序规则计算金额并保存待确认申请。缺项返回字段和规则要求，不自选整单、扩大数量、转换售后方式或替用户作质量认定。确认后登记申请，资金处理另行执行。 | `kind: enum`，`refund_only / return_refund`；`items: array<AfterSaleItem>`；`reason: string`，办理原因 |
| `prepare_invoice` | 校验指定订单和抬头资料，保存待确认的开票申请。企业税号必填；完整敏感信息只在可信确认界面核对。确认后登记申请，不生成正式票或擅自更正用户税号。 | `order_ref: string`；`invoice_type: enum`，`personal / company`；`title: string`，抬头；`tax_number?: string`，企业必填 |
| `prepare_order_address_change` | 校验指定订单与本人保存地址、发货状态和履约控制权，保存待确认的新旧地址内容。确认后更新订单快照并追加历史；不自动选默认地址或修改地址簿。 | `order_ref: string`；`address_id: string`，目标地址 |
| `prepare_order_cancel` | 校验指定订单的支付、履约、库存与券占用并保存取消方案。确认后取消及释放占用；不支持时返回规则阻断，不能转成退款申请。 | `order_ref: string`；`reason?: string`，用户已说明的取消原因；未提供时不编造 |
| `prepare_after_sale_cancel` | 校验指定申请的受理、退款及履约状态，保存待确认的撤回内容；确认后才变更申请及事件。不能把取消聊天事项当成撤销已有业务申请。 | `after_sale_id: string`；`reason?: string`，用户已说明的撤回原因；未提供时不编造 |

### 2.8 事项协作

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `read_task_context` | 读取当前授权事项中的原话、已绑定对象、已明确选择、待补字段和历史回执／交接记录，保留来源与时间。通常首轮由执行器注入；工具不生成处理计划，历史回执不充当当前状态。 | 无 |
| `ask_user` | 保存 LLM 已拟定的澄清问题和候选，更新事项为待答并结束本轮。工具不自行决定问什么；用户回答不等于业务确认。 | `question: string`，1–500 字；`choices?: array<Choice>`，2–5 个候选；省略时自由回答 |
| `handoff_to_human` | 创建或复用当前事项的真实人工交接并返回工单编号／状态，服务端附带已有事实和原话。转交决定由明确用户请求或既定升级规则支持；不能自动承诺处理结果。 | `reason: string`，转交原因 |

## 3. 扩展工具：15 个（待补齐配套能力）

| 工具 | 工具描述 | 参数 |
| --- | --- | --- |
| `quote_delivery` | 按确定商品和保存地址执行配送区域、运费模板及已配置时效规则，返回逐项费用、来源和缺项。缺重量、地区或仓库数据时不猜运费或到达日期。 | `items: array<PurchaseItem>`；`address_id: string` |
| `get_restock_plan` | 读取指定 SKU 的补货计划、预计入库／可售日期、来源和更新时间。计划时间不等于承诺，不评估没有记录的“可信度”或预测到货。 | `sku_id: string` |
| `assess_price_protection` | 按价保期限、可比价格、成交分摊与历史补差规则校验指定订单行并计算额度；条件无法核实时返回未知，不按两次标价直接补差。 | `order_item_id: string` |
| `prepare_price_protection` | 执行同一价保校验并保存待确认申请；确认后登记，由已接通流程处理资金，返回可查询的售后编号。不得输入或生成任意赔偿金额。 | `order_item_id: string`；`reason?: string` |
| `prepare_exchange` | 校验用户明确指定的原购买行、替换 SKU、数量、价差及库存规则，保存待确认换货申请。不得自行选尺码／颜色；确认后登记关联退件和补发流程，返回售后编号。 | `order_item_id: string`；`replacement_sku_id: string`；`quantity: integer`，1–999；`reason: string` |
| `get_return_instructions` | 读取已生效退货授权的仓库地址、期限、材料要求及可登记承运商名称／代码。未授权或信息缺失返回缺项，不由 LLM 或工具推测退货地址。 | `after_sale_id: string` |
| `prepare_return_shipment` | 校验退货授权、已支持承运商及用户提供的实际运单，保存待确认的逆向包裹登记。登记成功不等于承运商已收件；后续按独立物流、收货和验货记录查询。 | `after_sale_id: string`；`carrier_code: string`，已支持的承运商；`tracking_number: string`，实际寄回运单 |
| `prepare_repair` | 按保修与数量规则保存待确认维修申请，故障描述按用户陈述记录。不能推断检测结论、费用或维修完成；确认后登记工单并返回售后编号。 | `order_item_id: string`；`quantity: integer`，1–999；`problem: string`，1–500 字故障描述 |
| `request_dispatch` | 在明确催单请求下向已接通的接收方提交幂等催单，返回实际受理编号和状态。没有真实渠道则不可用；受理不等于已经发货。 | `order_ref: string`；`reason?: string` |
| `prepare_invoice_reissue` | 校验本人正式原票与新抬头信息，保存待确认更正／重开申请。需实际原票关系及作废／红冲处理流程；不能生成虚构票据或直接覆盖原票。 | `issued_invoice_id: string`；`invoice_type: enum`，`personal / company`；`title: string`；`tax_number?: string`，企业必填 |
| `get_invoice_document` | 读取本人正式发票号码、文件元数据和经服务端授权的下载入口；无真实文件返回未就绪，不接收任意下载地址。 | `issued_invoice_id: string` |
| `get_membership` | 读取本人的会员等级、积分余额、权益记录及有效期；无会员模型或数据时返回未接通／无记录，不根据消费金额或系统角色自行推断。 | 无 |
| `list_points` | 分页读取本人积分增减、过期、来源与撤销关联；余额与汇总由服务端计算，不由模型累加一页数据推算。 | `cursor?: string` |
| `quote_checkout` | 按明确商品、地址和券组合计算完整结算金额，保存有效期内的不可变报价快照并返回 quote_ref。任何费用缺失或组合不合法时不生成可提交报价；不自动选券、不预留库存或券。 | `items: array<PurchaseItem>`；`address_id: string`；`coupon_refs?: array<string>`，0–5 张本人券实例、禁止重复；省略／空数组均不用券，非空为指定组合 |
| `prepare_purchase` | 核验用户明确选定的报价及当前价格、库存和券状态，保存待确认下单内容。确认后创建订单及占用；变化则返回失效，不擅自改价／换券／换商品。支付另行确认处理。 | `quote_ref: string`，有效报价引用 |
