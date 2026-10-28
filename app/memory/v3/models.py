"""Wire contracts contain no caller-controlled identity or timestamps."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Evidence(Strict):
    message_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=4000)


class Scope(Strict):
    subject: Literal['self'] = 'self'
    product_category: str | None = None
    usage: Literal['general', 'daily', 'running'] = 'general'
    size_system: Literal['INT', 'EU', 'US', 'UK', 'CN', 'unknown'] | None = None


class Change(Strict):
    action: Literal['ADD', 'UPDATE', 'MERGE', 'FORGET', 'NOOP', 'NEEDS_CLARIFICATION']
    group_id: str = Field(min_length=1, max_length=80)
    predicate: str
    scope: Scope = Field(default_factory=Scope)
    value: Any = None
    polarity: Literal['positive', 'negative'] = 'positive'
    evidence: list[Evidence] = Field(min_length=1, max_length=8)
    primary_message_id: str
    target_ids: list[str] = Field(default_factory=list, max_length=16)
    restore: bool = False
    valid_time_quote: str = Field(default='', max_length=120)


class Plan(Strict):
    schema_version: Literal[3] = 3
    changes: list[Change] = Field(max_length=64)


class Conflict(RuntimeError):
    """The extraction snapshot changed; retry outside the transaction."""


class EvidenceRejected(ValueError):
    pass
