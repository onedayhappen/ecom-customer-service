"""Validated storage contracts. Identity is supplied by the authenticated caller."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MessageSource(StrictModel):
    kind: Literal["message"] = "message"
    session_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)


class ToolSource(StrictModel):
    kind: Literal["tool_result"] = "tool_result"
    session_id: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)


Source = Annotated[MessageSource | ToolSource, Field(discriminator="kind")]
TaskStatus = Literal["in_progress", "waiting_user", "waiting_external", "paused", "completed", "cancelled"]


class TaskContent(StrictModel):
    goal: str = Field(min_length=1, max_length=2000)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    waiting_for: str | dict[str, JsonValue] | None = None
    status: TaskStatus = "in_progress"


class TaskPatch(StrictModel):
    """Supplied fields replace their previous values; omitted fields stay intact."""
    goal: str = Field(default=None, min_length=1, max_length=2000)
    details: dict[str, JsonValue] = Field(default=None)
    waiting_for: str | dict[str, JsonValue] | None = None
    status: TaskStatus = Field(default=None)


class TaskRecord(TaskContent):
    task_id: str
    user_id: str
    version: int
    created_at: str
    updated_at: str
    storage_mode: Literal["legacy", "durable"]


class TaskChange(StrictModel):
    change_id: str = Field(min_length=1, max_length=200)
    source: Source

