"""Typed public inputs. Identity, prices, authority and request IDs are never model inputs."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(min_length=1, max_length=512, description='查询工具返回的对象编号或引用')]
Reason = Annotated[str, Field(min_length=1, max_length=255, description='用户明确表达的办理原因')]
Count = Annotated[int, Field(strict=True, ge=1, le=999)]

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)

class Empty(Input):
    pass

class Page(Input):
    cursor: str | None = Field(None, max_length=4096, description='上一页返回的 next_cursor；省略查第一页')

class Money(Input):
    amount: str = Field(pattern=r'^\d{1,10}(\.\d{1,2})?$', description='非负十进制金额字符串')
    currency: str = Field(pattern=r'^[A-Z]{3}$', description='三位币种；不自动换汇')

class DateRange(Input):
    from_: str = Field(alias='from', description='区间开始，含时区的 ISO 8601 时间，包含边界')
    to: str = Field(description='区间结束，含时区的 ISO 8601 时间，不包含边界')

    @model_validator(mode='after')
    def interval(self):
        a, b = (datetime.fromisoformat(v.replace('Z', '+00:00')) for v in (self.from_, self.to))
        if not a.tzinfo or not b.tzinfo or not 0 < (b-a).total_seconds() <= 366*86400:
            raise ValueError('时间区间须包含时区且跨度为 0–366 天')
        return self

class SearchProducts(Page):
    query: str = Field(min_length=1, max_length=200, description='商品名称、品类或需求关键词')
    max_price: Money | None = Field(None, description='具体 SKU 的标价预算上限')
    specs: list['Spec'] = Field(default_factory=list, max_length=5)

class Spec(Input):
    name: str = Field(min_length=1,max_length=64)
    value: str = Field(min_length=1,max_length=128)

class Target(Page):
    target_ref: Id

class ProductDetails(Target):
    specs: list[Spec] = Field(default_factory=list,max_length=5)

class Addresses(Page):
    query: str | None = Field(None,min_length=1,max_length=200)

class Product(Page):
    product_id: Id

class Sku(Input):
    sku_id: Id

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
    merchant_id: Id

OrderStatus = Literal['all','pending_payment','paid','pending_receipt','received','after_sale','cancelled','closed','unknown']
SaleStatus = Literal['all','pending_review','approved','rejected','processing','completed','cancelled','closed','unknown']
class FindOrders(Page):
    query: str | None = Field(None, min_length=1, max_length=200, description='订单号或购买商品名称关键词')
    status: OrderStatus = Field('all', description='订单状态筛选')
    created_range: DateRange | None = Field(None, description='按下单时间筛选')

class Order(Input):
    order_ref: Id

class OrderPage(Order, Page):
    pass

class Shipment(Page):
    shipment_id: Id

class AssessOrder(Order):
    action: Literal['cancel_order','change_address','invoice'] = Field(description='只读评估的操作类型')

class Subject(Page):
    subject_ref: Id | None = Field(None, description='已查询的商家、商品、SKU 或订单引用，限定范围')

class Wallet(Page):
    status: Literal['all','available','reserved','used','expired','invalid','unknown'] = Field('all', description='本人持券状态')

class Coupon(Input):
    coupon_ref: Id

class CouponPage(Coupon, Page):
    pass

class PurchaseItem(Input):
    sku_id: Id
    quantity: Count

class PurchaseTarget(Input):
    order_ref: Id | None = None
    items: list[PurchaseItem] | None = Field(None,min_length=1,max_length=20)

    @model_validator(mode='after')
    def exclusive(self):
        if (self.order_ref is None)==(self.items is None):
            raise ValueError('订单与待购商品须二选一')
        return self

class CheckCoupon(Coupon):
    target: PurchaseTarget

class FindCoupons(Wallet):
    query: str | None = Field(None,min_length=1,max_length=200)
    subject_ref: Id | None = None

class Estimate(Input):
    items: list[PurchaseItem] = Field(min_length=1,max_length=20,description='同商家、同币种的待购 SKU 与数量')
    coupon_refs: list[Id] = Field(default_factory=list,max_length=5,description='一组确切的本人券组合；省略表示不用券')

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.coupon_refs)) != len(self.coupon_refs):
            raise ValueError('券实例不得重复')
        return self

class AfterSales(Page):
    query: str | None = Field(None,min_length=1,max_length=200)
    created_range: DateRange | None = None
    order_ref: Id | None = None
    status: SaleStatus = 'all'

class AfterSale(Input):
    after_sale_id: Id

class AfterSalePage(AfterSale, Page):
    pass

class AfterSaleItem(Input):
    order_item_id: Id
    quantity: Count

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

class CancelOrder(Order):
    reason: Reason | None = None

class CancelSale(AfterSale):
    reason: Reason | None = None

class Choice(Input):
    label: str = Field(min_length=1,max_length=100)
    ref: Id | None = None

class Question(Input):
    question: str = Field(min_length=1,max_length=500,description='当前事项需要补充或澄清的一个问题')
    choices: list[Choice] | None = Field(None,min_length=2,max_length=5,description='可选的 2–5 个不同选项，仍允许自由回答')

    @model_validator(mode='after')
    def unique(self):
        if self.choices and len({c.label for c in self.choices}) != len(self.choices):
            raise ValueError('选项不得重复')
        return self

class Handoff(Input):
    reason: Reason

class Invoices(Page):
    target_ref: Id | None = None

class Delivery(Input):
    items: list[PurchaseItem] = Field(min_length=1,max_length=20)
    address_id: Annotated[str,Field(pattern=r'^[1-9]\d{0,19}$')]

class Checkout(Estimate):
    address_id: Annotated[str,Field(pattern=r'^[1-9]\d{0,19}$')]

class Purchase(Input):
    quote_ref: Id

class OrderItem(Input):
    order_item_id: Id

class PriceProtection(OrderItem):
    reason: Reason | None = None

class Exchange(OrderItem):
    replacement_sku_id: Id
    quantity: Count
    reason: Reason

class ReturnShipment(AfterSale):
    carrier_code: str = Field(min_length=1,max_length=32)
    tracking_number: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9-]+$')
    items: list[AfterSaleItem] | None = Field(None,min_length=1,max_length=20,description='本包裹的原订单明细和数量；省略时登记尚未寄回的全部数量')

class Repair(OrderItem):
    quantity: Count
    problem: str = Field(min_length=1,max_length=500)

class IssuedInvoice(Input):
    issued_invoice_id: Id

class InvoiceReissue(IssuedInvoice):
    invoice_type: Literal['personal','company']
    title: str = Field(min_length=1,max_length=200)
    tax_number: str | None = Field(None,min_length=1,max_length=64)

    @model_validator(mode='after')
    def tax(self):
        if self.invoice_type=='company' and not self.tax_number:
            raise ValueError('企业申请需要税号')
        return self

SearchProducts.model_rebuild()
