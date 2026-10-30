# 文件说明：提供固定消息截止点的记忆归档申请与归档状态查询接口。
"""Authenticated archive/checkpoint API; no model call on the request thread."""
import asyncio
from uuid import UUID

from fastapi import APIRouter, Request, Depends

from app.api.support import identity, request_budget
from app.support.runtime import get_store
from app.support.store import SupportError
from app.memory.v3.jobs import archive, archive_status

router = APIRouter(dependencies=[Depends(request_budget)])


@router.post('/api/v1/sessions/{session_id}/memory-archive', status_code=202)
async def archive_memory(session_id: UUID, request: Request):
    from app.memory.v3.routing import enabled
    if not await asyncio.to_thread(enabled, identity(request), store=get_store()):
        raise SupportError('此功能需要长期记忆 v3', 409)
    return await asyncio.to_thread(archive, get_store(), identity(request), str(session_id))


@router.get('/api/v1/memory-archives/{archive_id}')
async def get_archive(archive_id: UUID, request: Request):
    return await asyncio.to_thread(archive_status, get_store(), identity(request), str(archive_id))
