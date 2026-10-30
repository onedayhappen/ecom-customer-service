"""Configured provider gateway. Never accept endpoints or credentials from model arguments."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse
import hashlib
import json
import urllib.request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from datetime import datetime
from decimal import Decimal
from app.services.commerce.common import BusinessError

_override=ContextVar('commerce_provider_override',default=None)

@contextmanager
def provider_factory(factory):
    token=_override.set(factory)
    try: yield
    finally: _override.reset(token)

class ProviderModel(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Carrier(ProviderModel):
    code: str=Field(min_length=1,max_length=32)
    name: str=Field(min_length=1,max_length=100)

class ReturnAuthorization(ProviderModel):
    address: dict[str,str]=Field(min_length=1,max_length=12)
    carriers: list[Carrier]=Field(min_length=1,max_length=50)
    requirements: list[str]=Field(max_length=30)
    deadline: datetime

class ReturnEvent(ProviderModel):
    return_shipment_id: str | None=Field(None,max_length=64)
    status: Literal['registered','collected','in_transit','delivered','received','inspecting','accepted','rejected']
    event_time: datetime
    description: str=Field(max_length=512)

class Refund(ProviderModel):
    payment_id: str=Field(min_length=1,max_length=64)
    provider_refund_id: str=Field(min_length=1,max_length=128)
    amount: Decimal=Field(gt=0,max_digits=12,decimal_places=2)
    currency: str=Field(pattern=r'^[A-Z]{3}$')
    completed_at: datetime

class IssuedInvoice(ProviderModel):
    invoice_number: str=Field(min_length=1,max_length=128)
    document_key: str=Field(min_length=1,max_length=255)
    mime_type: Literal['application/pdf','application/xml','application/ofd']
    sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    issued_at: datetime
    amount: Decimal | None=None
    currency: str | None=None
    title_digest: str | None=None

class ReplacementShipment(ProviderModel):
    tracking_number: str=Field(min_length=1,max_length=64)
    carrier: str=Field(min_length=1,max_length=64)
    shipped_at: datetime
    sku_id: str=Field(min_length=1,max_length=64)
    quantity: int=Field(strict=True,ge=1,le=999)
    delivered_at: datetime | None=None
    receipt_confirmed_at: datetime | None=None

class RefusedReturn(ProviderModel):
    shipment_id: str=Field(min_length=1,max_length=64)
    refused_at: datetime
    returned_at: datetime
    inspected_at: datetime

class CancellationEvidence(ProviderModel):
    approved: bool=False
    fulfillment_stopped: bool=False
    returns: list[RefusedReturn]=Field(default_factory=list,max_length=100)

class ProviderResponse(ProviderModel):
    status: Literal['accepted','processing','completed','rejected','not_found']
    reference: str | None=Field(None,max_length=128)
    message: str | None=Field(None,max_length=512)
    return_authorization: ReturnAuthorization | None=None
    return_events: list[ReturnEvent]=Field(default_factory=list,max_length=20)
    refund: Refund | None=None
    refunds: list[Refund]=Field(default_factory=list,max_length=100)
    cancellation: CancellationEvidence | None=None
    invoice: IssuedInvoice | None=None
    original_voided: bool=False
    replacement_shipment: ReplacementShipment | None=None
    request_id: str | None=None
    user_id: str | None=None
    platform_code: str | None=None
    order_id: str | None=None
    target_id: str | None=None
    items: list[dict]=Field(default_factory=list,max_length=100)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise BusinessError('temporarily_unavailable','处理渠道返回了重定向')

class HttpGateway:
    def __init__(self,config):
        self.endpoint=config['url'].rstrip('/'); self.token=config['token']
        parsed=urlparse(self.endpoint)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BusinessError('temporarily_unavailable','处理渠道必须配置固定 HTTPS 服务地址')
    def call(self,operation,request_id,kind,payload=None):
        body=json.dumps({'request_id':request_id,'kind':kind,'payload':payload},ensure_ascii=False).encode()
        req=urllib.request.Request(self.endpoint+'/'+operation,data=body,method='POST',
            headers={'Content-Type':'application/json','Authorization':'Bearer '+self.token,'Idempotency-Key':request_id})
        try:
            with urllib.request.build_opener(NoRedirect).open(req,timeout=8) as response:
                raw=response.read(262145)
            if len(raw)>262144: raise ValueError('oversized provider response')
            result=ProviderResponse.model_validate_json(raw)
            if result.status!='not_found' and not result.reference: raise ValueError('missing receipt')
            return result.model_dump(mode='json')
        except Exception:
            raise BusinessError('result_unknown','处理渠道结果暂未核实，将按原请求编号查询') from None

def provider_mode():
    from app.config.settings import settings
    mode=settings.commerce_provider_mode or ('http' if settings.commerce_provider_config.strip() not in {'', '{}'} else 'disabled')
    if mode not in {'disabled','simulator','http'}:
        raise BusinessError('temporarily_unavailable','COMMERCE_PROVIDER_MODE 配置无效')
    return mode


def gateway(kind, *, mode=None, store=None, user=None):
    if _override.get(): return _override.get()(kind)
    from app.config.settings import settings
    selected=provider_mode()
    if mode is not None and mode!=selected:
        raise BusinessError('provider_mode_mismatch','任务绑定渠道与当前模式不同，已阻止切换处理来源')
    if selected=='disabled': return None
    if selected=='simulator':
        if kind not in {'refund_only','return_refund','invoice'}: return None
        from app.services.commerce.simulator import Simulator
        return Simulator(store,user)
    try:
        config=json.loads(settings.commerce_provider_config or '{}').get(kind)
        return HttpGateway(config) if config else None
    except (ValueError,TypeError,AttributeError,KeyError):
        raise BusinessError('temporarily_unavailable','处理渠道配置格式无效') from None

def invoice_file(invoice):
    from app.config.settings import settings
    if not settings.commerce_invoice_root: raise BusinessError('unsupported_capability','发票文件存储尚未配置')
    root=Path(settings.commerce_invoice_root).resolve()
    path=(root/invoice['document_key']).resolve()
    if not path.is_relative_to(root) or path==root or not path.is_file(): raise BusinessError('not_found','发票文件不存在')
    if invoice['mime_type'] not in {'application/pdf','application/xml','application/ofd'}: raise BusinessError('insufficient_data','发票文件格式未核实')
    with path.open('rb') as handle:
        checksum=hashlib.file_digest(handle,'sha256').hexdigest()
    if not invoice['sha256'] or checksum!=invoice['sha256']:
        raise BusinessError('insufficient_data','发票文件校验失败')
    return path
