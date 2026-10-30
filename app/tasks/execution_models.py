# 文件说明：定义单事项执行输入、调用预算及进度更新的严格契约。
"""The bounded input and progress contracts for single-task execution."""
import re
import json
from typing import Literal, get_args

from pydantic import Field, model_validator

from app.tasks.models import MessageSource, QuestionSpec, StrictModel
from app.agents.association import DetailField
from app.prompts.execution import TASK_PROMPT  # Compatibility re-export.

QUESTION_FIELDS = tuple(sorted({*get_args(DetailField), "details"}))

class ExecutionBudget(StrictModel):
    model_calls: int = Field(default=5, ge=1, le=12)
    tool_calls: int = Field(default=8, ge=1, le=30)
    seconds: float = Field(default=60.0, ge=0.05, le=180)
    call_seconds: float = Field(default=15.0, ge=0.01, le=60)
    input_tokens: int = Field(default=24000, ge=1000, le=30000)
    output_tokens: int = Field(default=1200, ge=64, le=2000)


class TaskExecutionInput(StrictModel):
    job_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)
    source: MessageSource
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)


class ProgressInput(StrictModel):
    expected_version: int = Field(ge=1)
    state: Literal["in_progress", "waiting_user", "waiting_external"]
    question: QuestionSpec | None = None
    wait_reason: str | None = Field(default=None, min_length=1, max_length=500)
    evidence_id: str | None = None

    @model_validator(mode="after")
    def waiting(self):
        if self.state == "waiting_user" and not self.question:
            raise ValueError("A user wait requires a question")
        if self.state == "waiting_external" and not (self.wait_reason and self.evidence_id):
            raise ValueError("An external wait requires evidence")
        if self.state != "waiting_user" and self.question:
            raise ValueError("Unexpected question")
        if self.state != "waiting_external" and (self.wait_reason or self.evidence_id):
            raise ValueError("Unexpected external wait")
        if self.question and self.question.field not in QUESTION_FIELDS:
            raise ValueError("unsupported_question_field")
        if self.question and re.search(r"密码|验证码|银行卡号|密钥|\btoken\b|\bapi.?key\b",
                                       json.dumps(self.question.model_dump(), ensure_ascii=False), re.I):
            raise ValueError("sensitive_question")
        return self


class ReadTaskContext(StrictModel):
    """Internal task read contract, not a model-callable tool."""
    order_id: str | None = Field(default=None, min_length=1, max_length=100)
