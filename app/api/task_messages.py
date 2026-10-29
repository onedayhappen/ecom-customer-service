"""Durable message admission; database rollout controls automatic processing."""
import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.support import ensure_session, identity
from app.config.settings import settings
from app.runtime.budgets import admit_request
from app.support.runtime import explicit_handoff, get_store
from app.support.store import SupportError
from app.tasks.intake import MessageIntake


def enabled():
    if not settings.task_intake_enabled:
        raise SupportError("持续消息入口未启用", 404)


router = APIRouter(prefix="/api/v1/task-messages", tags=["task-messages"], dependencies=[Depends(enabled)])


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    request_id: UUID
    message: str = Field(default='', max_length=4000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=4)
    action: Literal["handoff", "cancel"] | None = None
    task_id: UUID | None = None
    task_version: int | None = Field(default=None, ge=1)


@router.post("", status_code=202)
async def receive(body: MessageBody, request: Request):
    user, sid = identity(request), str(body.session_id)
    await asyncio.to_thread(admit_request, user, "control" if body.action or explicit_handoff(body.message) else "chat")
    await asyncio.to_thread(ensure_session, sid, user)
    return await asyncio.to_thread(
        MessageIntake(get_store()).receive, sid, user, request_id=str(body.request_id), content=body.message,
        attachment_ids=[str(aid) for aid in body.attachment_ids],
        action=body.action, task_id=str(body.task_id) if body.task_id else None, task_version=body.task_version)


@router.get("/{session_id}")
async def progress(session_id: UUID, request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
    user = identity(request)
    await asyncio.to_thread(admit_request, user, "read")
    return await asyncio.to_thread(MessageIntake(get_store()).progress, str(session_id), user, after=after, limit=limit)
