# 文件说明：定义历史业务回执及内部确认字段的校验结构。
"""Validation of historical business receipts and internal confirmation fields; no tools."""
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


class InvoiceFields(Contract):
    title: Text
    invoice_type: Literal['personal', 'company', 'vat_special']
    tax_id: Text | None = None

    @model_validator(mode='after')
    def company_tax_id(self):
        if self.invoice_type != 'personal' and not self.tax_id:
            raise ValueError('企业发票需要税号')
        return self


class AfterSaleRecord(Evidence):
    after_sale_id: Text
    order_id: Text
    kind: Literal['refund', 'return', 'exchange', 'price_protection']
    status: Text
    is_demo: bool
    note: Text | None = None
    item_id: Text | None = None
    item_name: Text | None = None
    sku_id: Text | None = None
    quantity: Count | None = None
    tracking_number: Text | None = None
    deadline: datetime | None = None
    required_materials: Annotated[list[Text], Field(max_length=20)] = Field(default_factory=list)
    settled_at: datetime | None = None
    transaction_id: Text | None = None
    request_id: Text | None = None
    platform_code: Text = 'pdd'
    store_id: Text | None = None
    product_id: Text | None = None
