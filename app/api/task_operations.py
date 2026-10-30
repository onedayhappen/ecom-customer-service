# 文件说明：提供固定业务操作的卡片查询、确认、取消和结果核实接口。
"""Fixed operation confirmation, never a client-selected business tool call."""
import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.support import identity, request_budget
from app.api.task_messages import enabled as intake_enabled
from app.config.settings import settings
from app.support.runtime import get_store
from app.support.store import SupportError
from app.tasks.operations import TaskOperations


def enabled(response: Response):
    intake_enabled()
    if not settings.task_operations_enabled:
        raise SupportError("事项业务提交入口未启用", 404)
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/v1/task-operations", tags=["task-operations"],
                   dependencies=[Depends(enabled), Depends(request_budget)])


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID


class ConfirmBody(SessionBody):
    confirmation_token: str = Field(min_length=32, max_length=128)


@router.get("/{session_id}")
async def operations(session_id: UUID, request: Request):
    return await asyncio.to_thread(TaskOperations(get_store(), identity(request)).list, str(session_id))


@router.post("/{operation_id}/confirm")
async def confirm(operation_id: UUID, body: ConfirmBody, request: Request):
    return await asyncio.to_thread(TaskOperations(get_store(), identity(request)).confirm,
                                  str(body.session_id), str(operation_id), body.confirmation_token)


@router.post("/{operation_id}/cancel")
async def cancel(operation_id: UUID, body: SessionBody, request: Request):
    return await asyncio.to_thread(TaskOperations(get_store(), identity(request)).cancel,
                                  str(body.session_id), str(operation_id))


@router.post("/{operation_id}/refresh")
async def refresh(operation_id: UUID, body: SessionBody, request: Request):
    return await asyncio.to_thread(TaskOperations(get_store(), identity(request)).reconcile,
                                  str(operation_id), sid=str(body.session_id))
