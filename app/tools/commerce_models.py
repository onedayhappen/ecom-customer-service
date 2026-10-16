"""Validated v1 commerce contracts. Money is integer minor units, never floats."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=200)]
Money = Annotated[int, Field(strict=True, ge=0, le=10**12)]
Count = Annotated[int, Field(strict=True, ge=1, le=10000)]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Evidence(Contract):
    source: Text
    observed_at: datetime

    @model_validator(mode='after')
    def timezone_required(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and value.utcoffset() is None:
                raise ValueError(f'{name} 必须包含时区')
        return self


class Line(Contract):
    sku_id: Text
    quantity: Count


class Basket(Contract):
    items: Annotated[list[Line], Field(min_length=1, max_length=100)]
    channel: Text
    region: Text

    @model_validator(mode='after')
    def unique_items(self):
        if len({i.sku_id for i in self.items}) != len(self.items):
            raise ValueError('同 SKU 请合并数量')
        return self


class OfferQuery(Contract):
    sku_id: Text
    channels: Annotated[list[Text], Field(min_length=1, max_length=10)]
    quantity: Count = 1
    region: Text


class PromotionQuery(Contract):
    sku_ids: Annotated[list[Text], Field(min_length=1, max_length=100)]
    channel: Text


class CouponQuery(Contract):
    coupon_id: Text | None = None
    channel: Text | None = None


class HistoryQuery(Contract):
    sku_id: Text
    channel: Text
    start: datetime
    end: datetime
    price_type: Literal['list', 'payable'] = 'payable'

    @model_validator(mode='after')
    def interval(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None or self.start >= self.end:
            raise ValueError('时间范围必须含时区，且 start < end')
        return self


class AfterSaleQuery(Contract):
    order_id: Text
    after_sale_id: Text | None = None
    kind: Literal['refund', 'return', 'exchange', 'price_protection'] | None = None


class InvoiceFields(Contract):
    title: Text
    invoice_type: Literal['personal', 'company', 'vat_special']
    tax_id: Text | None = None

    @model_validator(mode='after')
    def company_tax_id(self):
        if self.invoice_type != 'personal' and not self.tax_id:
            raise ValueError('企业发票需要税号')
        return self


class InvoiceQuery(Contract):
    order_id: Text
    mode: Literal['lookup', 'preview'] = 'lookup'
    invoice_id: Text | None = None
    fields: InvoiceFields | None = None


class InvoiceWrite(Contract):
    order_id: Text
    fields: InvoiceFields
    preview_version: Text


class InvoiceReissue(InvoiceWrite):
    invoice_id: Text


class OrderActionQuery(Contract):
    order_id: Text
    action: Literal['address_change', 'cancel', 'expedite']
    proposal: dict[str, str] = Field(default_factory=dict, max_length=10)


class PaymentQuery(Contract):
    order_id: Text


class Empty(Contract):
    pass


class MutationQuery(Contract):
    operation_id: Text | None = None
    request_id: Text | None = None

    @model_validator(mode='after')
    def one_reference(self):
        if bool(self.operation_id) == bool(self.request_id):
            raise ValueError('请提供 operation_id 或 request_id，不能同时提供')
        return self


class Offer(Evidence):
    offer_id: Text
    sku_id: Text
    specification: Text
    condition: Text
    included_items: Text
    channel: Text
    region: Text
    quantity: Count
    currency: Currency
    goods_minor: Money
    discount_minor: Money
    shipping_minor: Money
    fees_minor: Money
    total_minor: Money
    eligibility: Literal['eligible', 'ineligible', 'unknown']
    eligibility_basis: Text
    valid_until: datetime

    @model_validator(mode='after')
    def totals(self):
        if self.discount_minor > self.goods_minor:
            raise ValueError('优惠不能超过商品金额')
        if self.total_minor != self.goods_minor - self.discount_minor + self.shipping_minor + self.fees_minor:
            raise ValueError('报价费用明细与总额不一致')
        if self.valid_until <= self.observed_at:
            raise ValueError('报价有效期必须晚于观测时间')
        return self


class OfferComparison(Contract):
    offers: Annotated[list[Offer], Field(min_length=2, max_length=20)]


class PricePoint(Evidence):
    amount_minor: Money


class History(Evidence):
    sku_id: Text
    specification: Text
    channel: Text
    currency: Currency
    price_type: Literal['list', 'payable']
    start: datetime
    end: datetime
    coverage_complete: bool
    points: Annotated[list[PricePoint], Field(max_length=10000)]

    @model_validator(mode='after')
    def valid_points(self):
        if self.start >= self.end:
            raise ValueError('历史区间无效')
        if any(not self.start <= p.observed_at <= self.end for p in self.points):
            raise ValueError('价格样本超出查询范围')
        return self


class HistorySummary(Contract):
    history: History


class Policy(Evidence):
    policy_id: Text
    version: Text
    topic: Text
    channel: Text
    category: Text
    effective_from: datetime
    effective_until: datetime
    text: Annotated[str, Field(min_length=1, max_length=10000)]

    @model_validator(mode='after')
    def effective_interval(self):
        if self.effective_from >= self.effective_until:
            raise ValueError('政策有效区间无效')
        return self


class PolicyCheck(Contract):
    policies: Annotated[list[Policy], Field(min_length=1, max_length=50)]
    topic: Text
    channel: Text
    category: Text


class Discount(Evidence):
    rule_id: Text
    version: Text
    channel: Text
    currency: Currency
    starts_at: datetime
    ends_at: datetime
    sku_ids: list[Text] = Field(default_factory=list, max_length=100)
    minimum_minor: Money = 0
    kind: Literal['fixed', 'percentage']
    # Percentage is discount, e.g. 1000 basis points = 10% off.
    amount_minor: Money = 0
    discount_bps: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0
    cap_minor: Money | None = None
    exclusive_group: Text | None = None
    base: Literal['original', 'remaining'] = 'remaining'
    priority: Annotated[int, Field(strict=True, ge=0)] = 0

    @model_validator(mode='after')
    def rule_valid(self):
        if self.starts_at >= self.ends_at:
            raise ValueError('活动时间区间无效')
        if self.kind == 'fixed' and self.discount_bps or self.kind == 'percentage' and self.amount_minor:
            raise ValueError('固定减免与百分比折扣参数不能混用')
        return self


class Coupon(Evidence):
    coupon_id: Text
    status: Literal['available', 'used', 'locked', 'expired', 'returning']
    owned: bool
    rule: Discount


class PricedLine(Line):
    unit_price_minor: Money


class CouponCheck(Contract):
    coupon: Coupon
    items: Annotated[list[PricedLine], Field(min_length=1, max_length=100)]
    channel: Text
    currency: Currency
    used_exclusive_groups: list[Text] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def unique_skus(self):
        if len({i.sku_id for i in self.items}) != len(self.items):
            raise ValueError('同 SKU 请合并数量')
        return self


class Calculation(Contract):
    items: Annotated[list[PricedLine], Field(min_length=1, max_length=100)]
    channel: Text
    currency: Currency
    shipping_minor: Money
    fees_minor: Money = 0
    rules: Annotated[list[Discount], Field(max_length=100)] = Field(default_factory=list)
    # Supplied rules are a selected fixed plan, not an optimization search.
    eligibility: dict[str, Literal['eligible', 'ineligible', 'unknown']] = Field(default_factory=dict)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({i.sku_id for i in self.items}) != len(self.items):
            raise ValueError('同 SKU 请合并数量')
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError('规则 ID 不能重复')
        return self


class OrderPolicy(Evidence):
    version: Text
    effective_from: datetime
    effective_until: datetime
    allowed_statuses: Annotated[list[Text], Field(min_length=1, max_length=20)]


class DeliveryPolicy(Evidence):
    version: Text
    effective_from: datetime
    effective_until: datetime
    sku_ids: Annotated[list[Text], Field(min_length=1, max_length=100)]
    deliverable: bool
    currency: Currency
    fee_mode: Literal['fixed_plus_unit']
    base_minor: Money
    per_unit_minor: Money
    min_days: Annotated[int, Field(strict=True, ge=0, le=365)]
    max_days: Annotated[int, Field(strict=True, ge=0, le=365)]
    reason: Text | None = None


class AfterSaleRecord(Evidence):
    after_sale_id: Text
    order_id: Text
    kind: Literal['refund', 'return', 'exchange', 'price_protection']
    status: Text
    is_demo: bool
    note: Text | None = None


class PaymentRecord(Evidence):
    transaction_id: Text
    order_id: Text
    status: Literal['pending', 'paid', 'failed', 'closed', 'reversed', 'unknown']
    amount_minor: Money
    currency: Currency
    channel: Text
    is_demo: bool
