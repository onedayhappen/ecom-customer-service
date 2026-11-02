# 文件说明：定义公开工具的严格输入，限制额外字段、对象引用和参数组合。
"""Typed public inputs. Identity, prices, authority and request IDs are never model inputs."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(min_length=1, max_length=512, description='原样复制查询结果中的编号或引用；不猜测，不要求用户记忆内部编号')]
Reason = Annotated[str, Field(min_length=1, max_length=255, description='用户明确表达的办理原因')]
Count = Annotated[int, Field(strict=True, ge=1, le=999)]

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)

class Empty(Input):
    pass

class RecallMemory(Input):
    query: str = Field(min_length=1, max_length=1000, description='当前事项需要核实的本人历史偏好、品类和用途；不查询订单实时状态或办理授权')

class InspectImage(Input):
    attachment_id: str = Field(min_length=1, max_length=200, description='当前事项已收到的图片 attachment_id；不是文件路径或网址')
    question: str = Field(min_length=1, max_length=1000, description='本次需要从图片核实的具体问题')
    detail: Literal['brief', 'full'] = Field('brief', description='brief 返回相关观察摘要；确需完整观察时用 full')

class Page(Input):
    cursor: str | None = Field(None, min_length=1, max_length=4096, description='同一工具、同一筛选条件上一页的 next_cursor；翻页保持其他参数不变，改变条件时省略')

class Account(Page):
    collection: Literal['contacts','platform_accounts'] | None = Field(None,description='省略返回账号摘要；指定集合后分页读取全部脱敏记录')

    @model_validator(mode='after')
    def collection_cursor(self):
        if self.cursor and not self.collection:
            raise ValueError('账号摘要不能翻页；cursor 必须同时指定 collection')
        return self

class Money(Input):
    amount: str = Field(pattern=r'^\d{1,10}(\.\d{1,2})?$', description='非负十进制金额字符串')
    currency: str = Field(pattern=r'^[A-Z]{3}$', description='三位币种；不自动换汇')

class MoneyRange(Input):
    minimum: str | None = Field(None, pattern=r'^\d{1,10}(\.\d{1,2})?$', description='金额下限，包含边界；十进制字符串')
    maximum: str | None = Field(None, pattern=r'^\d{1,10}(\.\d{1,2})?$', description='金额上限，包含边界；十进制字符串')
    currency: str = Field(pattern=r'^[A-Z]{3}$', description='三位币种，例如 CNY；不自动换汇')

    @model_validator(mode='after')
    def bounds(self):
        if self.minimum is None and self.maximum is None:
            raise ValueError('至少指定一个金额边界')
        if self.minimum is not None and self.maximum is not None and Decimal(self.minimum)>Decimal(self.maximum):
            raise ValueError('金额下限不能大于上限')
        return self

class DateRange(Input):
    from_: str = Field(alias='from', description='区间开始，含时区的 ISO 8601 时间，包含边界')
    to: str = Field(description='区间结束，含时区的 ISO 8601 时间，不包含边界')

    @model_validator(mode='after')
    def interval(self):
        a, b = (datetime.fromisoformat(v.replace('Z', '+00:00')) for v in (self.from_, self.to))
        if not a.tzinfo or not b.tzinfo or not 0 < (b-a).total_seconds() <= 366*86400:
            raise ValueError('时间区间须包含时区且跨度为 0–366 天')
        return self

class Spec(Input):
    name: str = Field(min_length=1,max_length=64,description='结构化规格名称；当前数据常用颜色、尺寸（鞋码）、容量、版本、规格。使用实际字段名，例如鞋码查尺寸，不能写成未存储的尺码字段；未知名称先查询候选')
    value: str = Field(min_length=1,max_length=128,description='规格的确切值，例如红色、38；完整匹配，38 不匹配 38.5')

class SpecFilters(Input):
    specs: list[Spec] = Field(default_factory=list, max_length=5, description='同一商品行或 SKU 必须同时满足的规格；未明确时省略')

    @model_validator(mode='after')
    def unique_specs(self):
        if len({s.name for s in self.specs}) != len(self.specs):
            raise ValueError('同一规格名称只能出现一次')
        return self

class SearchProducts(Page, SpecFilters):
    query: str = Field(min_length=1, max_length=200, description='空格分隔的商品、品类、商家关键词，逐词文本匹配；不是自然语言语义搜索，不自动转换品牌别名')
    max_price: Money | None = Field(None, description='具体 SKU 的单件标价上限，不是整单预算或优惠后价格')

class Target(Page):
    target_ref: Id

class ProductDetails(Target, SpecFilters):
    target_ref: Id = Field(description='search_products 返回的 product 引用或 sku 引用；不是原始 product_id/sku_id')

class LogisticsTarget(Target):
    target_ref: Id = Field(description='查询结果中的 order、shipment、return_shipment 或 after_sale 引用；不是快递单号')

class ShipmentTarget(Target):
    target_ref: Id = Field(description='物流结果中 shipment 或 return_shipment 的 target_ref；不是订单引用或快递单号')

class RefundTarget(Target):
    target_ref: Id = Field(description='订单 order_ref 或售后查询返回的 after_sale 类型 target_ref')

class Addresses(Page):
    query: str | None = Field(None,min_length=1,max_length=200,description='一个已有地址标签或省、市、区关键词；省略列出本人保存地址，不匹配电话和街道详情')

class Product(Page):
    product_id: Id

class Sku(Input):
    sku_id: Id = Field(description='商品查询返回的原始 sku_id；不是 product 引用或 sku 引用')

class SkuPage(Sku, Page):
    pass

class Compare(Input):
    sku_ids: list[Id] = Field(min_length=2, max_length=5, description='待比较的 2–5 个不同 SKU')

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.sku_ids)) != len(self.sku_ids):
            raise ValueError('SKU 不得重复')
        return self

class PriceHistory(Sku, Page):
    days: int = Field(30, ge=1, le=365, description='最近天数，默认 30')

class Merchant(Input):
    merchant_id: Id = Field(description='商品或订单查询返回的原始 merchant_id')

OrderStatus = Literal['all','pending_payment','paid','pending_receipt','received','after_sale','cancelled','closed','unknown']
SaleStatus = Literal['all','pending_review','approved','rejected','processing','completed','cancelled','closed','unknown']
class OrderClues(SpecFilters):
    query: str | None = Field(None, min_length=1, max_length=200, description='空格分隔的商品名称、型号或品牌关键词，各词匹配同一购买行；颜色、尺码优先放 specs。兼容已有订单号片段搜索；确切编号用 order_id。不做语义搜索或品牌别名转换。')
    item_quantity: Count | None = Field(None, description='同一购买行的原购买数量，不是订单总件数、包裹件数或售后数量')
    paid_amount_range: MoneyRange | None = Field(None, description='整笔订单已支付金额范围，不是单价、商品行金额或退款金额')
    created_range: DateRange | None = Field(None, description='订单创建时间范围；不是付款、发货、收货时间')

class FindOrders(Page, OrderClues):
    order_id: Id | None = Field(None, description='用户已提供或查询已返回的确切原始订单号；不编造，不索要用户记不住的编号')
    status: OrderStatus = Field('all', description='订单状态筛选')
    hints: OrderClues | None = Field(None, description='用户明确表示不确定、可能记错的线索，仅用于候选排序，不排除未匹配订单。顶层条件均为严格筛选，不要将同一线索同时填到两处。排序不代表已确认订单。')

class Order(Input):
    order_ref: Id = Field(description='find_orders 或订单查询返回的完整 order_ref，原样复制；不是原始订单号')

class OrderPage(Order, Page):
    pass

class Shipment(Page):
    shipment_id: Id

class AssessOrder(Order):
    action: Literal['cancel_order','change_address','invoice'] = Field(description='只读评估的操作类型')

class Subject(Page):
    subject_ref: Id | None = Field(None, description='已查询的商家、商品或 SKU 引用，限定范围')

class Wallet(Page):
    status: Literal['all','available','reserved','used','expired','invalid','unknown'] = Field('all', description='本人持券状态')

class Coupon(Input):
    coupon_ref: Id = Field(description='查询返回的 coupon_template 或 coupon_instance 引用；模板规则和本人持券实例不同')

class ClaimCoupon(Coupon):
    coupon_ref: Id = Field(description='list_claimable_coupons 返回的 coupon_template 引用；不能填本人持券实例')

class CouponPage(Coupon, Page):
    pass

class PurchaseItem(Input):
    sku_id: Id = Field(description='商品查询返回的原始 sku_id，不是 sku 引用')
    quantity: Count = Field(description='用户明确选定的该 SKU 购买件数；同一 SKU 合并为一行')

def unique_rows(items, key):
    ids = [getattr(item, key) for item in items or []]
    if len(set(ids)) != len(ids):
        raise ValueError(f'{key} 不得重复；同一商品须合并数量')

class PurchaseTarget(Input):
    order_ref: Id | None = Field(None,description='已查询的待付款订单 order_ref；与 items 二选一')
    items: list[PurchaseItem] | None = Field(None,min_length=1,max_length=20,description='同商家、同币种的待购商品；与 order_ref 二选一')

    @model_validator(mode='after')
    def exclusive(self):
        if (self.order_ref is None)==(self.items is None):
            raise ValueError('订单与待购商品须二选一')
        unique_rows(self.items, 'sku_id')
        return self

class CheckCoupon(Coupon):
    coupon_ref: Id = Field(description='find_my_coupons 返回的本人 coupon_instance 引用；不能使用券模板')
    target: PurchaseTarget = Field(description='待付款订单或待购商品二选一')

class FindCoupons(Wallet):
    query: str | None = Field(None,min_length=1,max_length=200,description='券名或商家名的连续文本片段，不是空格分词或语义搜索')
    subject_ref: Id | None = Field(None,description='已查询的商家、商品或 SKU 引用；限定相关范围，不代表最终可用')

class Estimate(Input):
    items: list[PurchaseItem] = Field(min_length=1,max_length=20,description='同商家、同币种的待购 SKU 与数量')
    coupon_refs: list[Id] = Field(default_factory=list,max_length=5,description='find_my_coupons 返回的一组确切 coupon_instance 引用；不能填券模板，省略表示不用券')

    @model_validator(mode='after')
    def unique(self):
        unique_rows(self.items, 'sku_id')
        if len(set(self.coupon_refs)) != len(self.coupon_refs):
            raise ValueError('券实例不得重复')
        return self

class AfterSales(Page, SpecFilters):
    query: str | None = Field(None,min_length=1,max_length=200,description='空格分隔的原购买商品关键词，匹配同一商品行；兼容订单/售后编号片段，确切售后编号用 after_sale_id')
    after_sale_id: Id | None = Field(None,description='查询结果中的确切原始售后编号；不是 after_sale 引用')
    created_range: DateRange | None = Field(None,description='售后申请创建时间范围，不是原订单下单时间；不确定时省略')
    order_ref: Id | None = Field(None,description='已定位订单的 order_ref，仅查询该订单售后')
    status: SaleStatus = Field('all',description='售后申请状态；不是退款流水、退货物流或原订单状态')

class AfterSale(Input):
    after_sale_id: Id = Field(description='find_after_sales 等结果中的原始 after_sale_id；不是 after_sale 类型 target_ref')

class AfterSalePage(AfterSale, Page):
    pass

class AfterSaleItem(Input):
    order_item_id: Id = Field(description='订单明细返回的原始 order_item_id；不是 sku_id，同一 SKU 在不同订单中是不同购买行')
    quantity: Count = Field(description='本次处理的该购买行件数，由用户意图确定；不是整单默认数量')

class AssessSale(Input):
    reason: Reason | None = None
    kind: Literal['refund_only','return_refund'] = Field(description='仅退款或退货退款')
    items: list[AfterSaleItem] = Field(min_length=1,max_length=20,description='同一订单的明细行及申请数量')

    @model_validator(mode='after')
    def unique(self):
        ids = [i.order_item_id for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError('订单行不得重复')
        return self

class PrepareSale(AssessSale):
    reason: Reason

class InvoiceList(Page):
    order_ref: Id | None = None

class Invoice(Input):
    invoice_id: Id

class Policies(Input):
    query: str = Field(min_length=1,max_length=200,description='需要检索的服务政策问题')
    subject_ref: Id | None = Field(None,description='订单、商品或 SKU 引用，限定政策适用对象')

class PrepareInvoice(Order):
    invoice_type: Literal['personal','company'] = Field(description='申请主体：个人或企业')
    title: str = Field(min_length=1,max_length=200,description='用户指定的发票抬头')
    tax_number: str | None = Field(None,min_length=1,max_length=64,description='企业税号；企业申请必填')

    @model_validator(mode='after')
    def tax(self):
        if self.invoice_type == 'company' and not self.tax_number:
            raise ValueError('企业申请需要税号')
        return self

class ChangeAddress(Order):
    address_id: Annotated[str,Field(pattern=r'^[1-9]\d{0,19}$',description='本人已保存的收货地址编号')]

class OrderInvoiceInfo(Input):
    invoice_type: Literal['personal','company'] = Field(description='开票主体：个人或企业')
    title: str = Field(min_length=1,max_length=200,description='用户指定的发票抬头')
    tax_number: str | None = Field(None,min_length=1,max_length=64,description='用户提供的企业税号，企业必填')

    @model_validator(mode='after')
    def tax(self):
        if self.invoice_type=='company' and not self.tax_number:
            raise ValueError('企业发票信息需要税号')
        return self

class UpdateOrder(Order):
    phone: str | None = Field(None,pattern=r'^\+?[0-9][0-9 -]{5,23}$',description='用户指定的收货人电话')
    recipient_name: str | None = Field(None,min_length=1,max_length=100,description='用户指定的新收货人姓名；省略表示不修改')
    note: str | None = Field(None,max_length=500,description='订单备注；空字符串表示清空')
    invoice: OrderInvoiceInfo | None = Field(None,description='待付款订单的开票信息，仅保存信息，不开票')

    @model_validator(mode='after')
    def changed(self):
        if all(getattr(self,k) is None for k in ('phone','recipient_name','note','invoice')):
            raise ValueError('至少指定一项要修改的订单信息')
        return self

class CancelOrder(Order):
    reason: Reason | None = None

class CancelSale(AfterSale):
    reason: Reason | None = None

class Handoff(Input):
    reason: str = Field(min_length=1,max_length=255,description='需要人工处理的具体问题与当前阻碍；不编造已处理结果')

class Invoices(Page):
    target_ref: Id | None = Field(None,description='requests 可省略或传 order/invoice 引用；issued 必须传查询返回的 invoice 申请引用，不能传正式发票编号')
    view: Literal['requests','issued'] = Field('requests',description='requests 查开票申请；issued 按申请引用分页读取正式发票记录')

    @model_validator(mode='after')
    def issued_target(self):
        if self.view == 'issued' and not self.target_ref:
            raise ValueError('查询正式发票必须指定 invoice 申请引用 target_ref')
        return self

class Delivery(Input):
    items: list[PurchaseItem] = Field(min_length=1,max_length=20,description='同商家、同币种的待购 SKU 与数量，每个 SKU 只出现一次')
    address_id: Annotated[str,Field(pattern=r'^[1-9]\d{0,19}$',description='list_addresses 返回的本人已保存地址编号，不是自然语言地址')]

    @model_validator(mode='after')
    def unique(self):
        unique_rows(self.items, 'sku_id')
        return self

class Checkout(Estimate):
    address_id: Annotated[str,Field(pattern=r'^[1-9]\d{0,19}$',description='list_addresses 返回的本人已保存地址编号')]

class Purchase(Input):
    quote_ref: Id = Field(description='quote_checkout 返回且用户已选定的有效 quote_ref；不是 estimate_purchase 结果或自行计算的金额')

class OrderItem(Input):
    order_item_id: Id = Field(description='订单购买明细返回的原始 order_item_id；不是 sku_id 或订单引用')

class PriceProtection(OrderItem):
    reason: Reason | None = None

class AssessExchange(OrderItem):
    quantity: Count = Field(description='该购买行本次希望换货的件数，不默认整单')
    replacement_sku_id: Id | None = Field(None,description='商品查询返回、用户希望换入规格的原始 sku_id；未明确时省略，不代选')
    reason: Reason | None = None

class AssessRepair(OrderItem):
    quantity: Count = Field(description='该购买行本次希望维修的件数')
    problem: str | None = Field(None,min_length=1,max_length=500,description='用户描述的故障，不作为检测结论')

class Exchange(OrderItem):
    replacement_sku_id: Id = Field(description='商品查询返回、用户明确选定的换入规格原始 sku_id')
    quantity: Count = Field(description='用户明确希望更换的该购买行件数')
    reason: Reason

class ReturnShipment(AfterSale):
    carrier_code: str = Field(min_length=1,max_length=32,description='退货指引返回的承运商 code；不是猜测的快递公司缩写')
    tracking_number: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9-]+$',description='用户提供的实际寄回运单号；不得编造或用发货包裹编号代替')
    items: list[AfterSaleItem] | None = Field(None,min_length=1,max_length=20,description='本包裹的原订单明细和数量；省略时登记尚未寄回的全部数量')

    @model_validator(mode='after')
    def unique(self):
        unique_rows(self.items, 'order_item_id')
        return self

class Repair(OrderItem):
    quantity: Count = Field(description='用户明确希望维修的该购买行件数')
    problem: str = Field(min_length=1,max_length=500,description='用户描述的具体故障现象，不填写模型推断的检测结论')

class IssuedInvoice(Input):
    issued_invoice_id: Id = Field(description='get_invoices(view=issued) 返回的原始 issued_invoice_id；不是 invoice 申请引用或申请编号')

class InvoiceReissue(IssuedInvoice):
    invoice_type: Literal['personal','company'] = Field(description='重新开票的主体：个人或企业')
    title: str = Field(min_length=1,max_length=200,description='用户指定的重开发票抬头')
    tax_number: str | None = Field(None,min_length=1,max_length=64,description='用户提供的企业税号，企业重开必填')

    @model_validator(mode='after')
    def tax(self):
        if self.invoice_type=='company' and not self.tax_number:
            raise ValueError('企业申请需要税号')
        return self

SearchProducts.model_rebuild()
