# 文件说明：定义 RAG 题集、相关性标注、政策回执及采集轨迹的数据契约。
"""Human-authored relevance labels and captured current-policy receipts."""
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from app.rag.policy import PolicyContext


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceKey(Record):
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    section: str = Field(min_length=1)

    @property
    def key(self):
        return self.source_id, self.version, self.section


class Relevance(EvidenceKey):
    grade: StrictInt = Field(ge=0, le=3)


class PolicyHit(EvidenceKey):
    # Keep current PolicyRetriever metadata (scope, digest, index provenance).
    model_config = ConfigDict(extra="allow")
    chunk_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class PolicyReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: StrictBool
    results: list[PolicyHit] = Field(default_factory=list, max_length=5)
    error_code: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.success and (not self.results or self.error_code):
            raise ValueError("Successful policy receipts need hits and no error code")
        if not self.success and (self.results or not self.error_code):
            raise ValueError("Failed policy receipts need an error code and no hits")
        return self


class ToolObservation(Record):
    name: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    result: dict | str


class EvalCase(Record):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=1000)
    context: PolicyContext
    relevance: list[Relevance] = Field(default_factory=list)
    expected_error: Literal[
        "policy_evidence_missing", "policy_context_missing",
        "policy_versions_conflict", "policy_untrusted_instructions",
    ] | None = None
    reference_answer: str | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_labels(self):
        if len({r.key for r in self.relevance}) != len(self.relevance):
            raise ValueError("Duplicate relevance labels")
        positives = any(r.grade > 0 for r in self.relevance)
        if (self.expected_error is None) != positives:
            raise ValueError("Supply positive relevance labels OR an expected policy rejection")
        return self


class EvalDataset(Record):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    description: str
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case IDs")
        return self


class ReplayTrace(Record):
    case_id: str
    query: str
    context: PolicyContext
    retrieval: PolicyReceipt
    # Number of hits requested, including when fewer were returned.
    requested_k: StrictInt = Field(ge=1, le=5)
    answer: str | None = None
    observations: list[ToolObservation] = Field(default_factory=list)
    total_tokens: StrictInt | None = Field(default=None, ge=0)


class ReplayDataset(Record):
    schema_version: Literal[1] = 1
    description: str
    traces: list[ReplayTrace] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({t.case_id for t in self.traces}) != len(self.traces):
            raise ValueError("Duplicate trace IDs")
        return self


def load_dataset(path: str | Path) -> EvalDataset:
    return EvalDataset.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


def load_traces(path: str | Path) -> ReplayDataset:
    return ReplayDataset.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
