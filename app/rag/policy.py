"""Operator-supplied applicability; no model may invent these fields."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PolicyError(ValueError):
    def __init__(self, code, fields=()):
        super().__init__(code)
        self.code, self.fields = code, list(fields)


class PolicyMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source_id: str = Field(min_length=1, max_length=160)
    source_uri: str = Field(min_length=1, max_length=1000)
    version: str = Field(min_length=1, max_length=80)
    rule_key: str = Field(min_length=1, max_length=160)
    platform_code: str = Field(min_length=1, max_length=80)
    store_id: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=120)
    audience: Literal['public', 'staff']
    selection_basis: Literal['request_at', 'order_created_at', 'delivered_at']
    selection_reference: str = Field(min_length=1, max_length=1000)
    effective_from: datetime
    effective_until: datetime | None = None
    reviewed: Literal[True]

    @field_validator('source_uri')
    @classmethod
    def safe_uri(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Source must be a public HTTP(S) reference without credentials')
        return value

    @field_validator('effective_from', 'effective_until')
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('Policy times require an explicit timezone')
        return value

    @model_validator(mode='after')
    def interval(self):
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError('Empty policy interval')
        return self


class PolicyContext(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    platform_code: str | None = Field(default=None, max_length=80)
    store_id: str | None = Field(default=None, max_length=120)
    channel: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=120)
    request_at: datetime
    order_created_at: datetime | None = None
    delivered_at: datetime | None = None
    order_id: str | None = Field(default=None, max_length=200)
    order_evidence_id: str | None = Field(default=None, max_length=200)

    @field_validator('request_at', 'order_created_at', 'delivered_at')
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('Business times require an explicit timezone')
        return value


SCOPE_FIELDS = ('platform_code', 'store_id', 'channel', 'category')
_INJECTION = re.compile(
    r'忽略.{0,20}(?:指令|规则|提示)|系统提示词|开发者指令|你现在是.{0,12}(?:管理员|系统)|'
    r'(?:跳过|无需|绕过).{0,12}确认|(?:调用|执行).{0,12}(?:工具|SQL|shell)|'
    r'ignore.{0,20}(?:instructions|prompt)|system\s*prompt|developer\s*message|'
    r'<\|(?:system|im_start)\|>|(?:api[_ -]?key|password)\s*[:=]', re.I)


def safe_clause(text):
    from app.memory.source_policy import memory_safe
    return isinstance(text, str) and memory_safe(text) and not _INJECTION.search(text)


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def applicability(metadata, context):
    """Return eligible/missing/excluded before reading a document body."""
    if metadata.audience != 'public':
        return 'excluded', []
    missing = []
    for key in SCOPE_FIELDS:
        declared, actual = getattr(metadata, key), getattr(context, key)
        if declared == '*':
            continue
        if actual is None:
            missing.append(key)
        elif actual != declared:
            return 'excluded', []
    moment = getattr(context, metadata.selection_basis)
    if moment is None:
        missing.append(metadata.selection_basis)
    elif moment < metadata.effective_from or (metadata.effective_until is not None and moment >= metadata.effective_until):
        return 'excluded', []
    if missing:
        return 'missing', missing
    return 'eligible', []


def failure(code, fields=()):
    return dict(success=False, backend='chroma-policy', results=[], error_code=code,
        missing_fields=sorted(set(fields)),
        error='尚无足够、有效且适用的条款依据。请核实缺少的条件，必要时转人工；不能据此承诺办理结果。')
