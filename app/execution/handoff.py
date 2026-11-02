# 文件说明：执行模型只交接处理状态和缺失信息，客户话术由回复组织生成。
"""Structured execution termination, without a customer-facing question."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionHandoff(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['ready', 'needs_input']
    missing_information: str | None = Field(default=None, max_length=1000)
    candidate_refs: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def valid(self):
        if self.status == 'needs_input' and not (self.missing_information or '').strip():
            raise ValueError('Missing information must be explicit')
        if self.status == 'ready' and (self.missing_information or self.candidate_refs):
            raise ValueError('Ready results cannot request input')
        if any(not ref.strip() for ref in self.candidate_refs) or len(set(self.candidate_refs)) != len(self.candidate_refs):
            raise ValueError('Invalid candidate references')
        return self
