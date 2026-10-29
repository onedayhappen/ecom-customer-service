"""Authenticated image uploads. Files are never exposed as a static directory."""
import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.support import ensure_session, identity
from app.config.settings import settings
from app.media.service import MediaService
from app.runtime.budgets import admit_request
from app.support.runtime import get_store
from app.support.store import SupportError
from app.tasks.intake import MessageIntake


async def budget(request: Request):
    await asyncio.to_thread(admit_request, identity(request), 'read' if request.method == 'GET' else 'control')


router = APIRouter(prefix='/api/v1/attachments', tags=['attachments'], dependencies=[Depends(budget)])


@router.post('', status_code=201)
async def upload(request: Request, session_id: UUID = Form(...), upload_request_id: UUID = Form(...),
                 file: UploadFile = File(...)):
    user, sid = identity(request), str(session_id)
    try:
        data = await file.read(settings.media_max_bytes + 1)
    finally:
        await file.close()
    await asyncio.to_thread(ensure_session, sid, user)
    return await asyncio.to_thread(MediaService(get_store()).upload, user, sid, str(upload_request_id), data)


def reader(aid, request, ticket_id):
    user = identity(request)
    if ticket_id:
        # The existing staff ticket policy remains the source of authority.
        store = get_store()
        with store.transaction(read_only=True) as tx:
            ticket = tx.one('SELECT session_id FROM support_tickets WHERE ticket_id=?', (str(ticket_id),))
            if not ticket:
                raise SupportError('工单不存在', 404)
            control = store._control(tx, ticket['session_id'])
            _, ceiling, _, _ = store._ticket_view(tx, control, user, str(ticket_id))
            binding = tx.one('''SELECT attachment_id FROM message_attachments
                WHERE session_id=? AND attachment_id=? AND sequence<=? LIMIT 1''',
                (ticket['session_id'], aid, ceiling))
            if not binding:
                raise SupportError('图片不属于此工单', 404)
            user = control['user_id']
    return user


@router.get('/{attachment_id}')
def metadata(attachment_id: UUID, request: Request, ticket_id: UUID | None = None):
    aid = str(attachment_id)
    return MediaService(get_store()).metadata(reader(aid, request, ticket_id), aid)


@router.get('/{attachment_id}/content')
def content(attachment_id: UUID, request: Request, variant: Literal['preview', 'original'] = Query('preview'),
            ticket_id: UUID | None = None):
    aid = str(attachment_id)
    data, mime = MediaService(get_store()).content(reader(aid, request, ticket_id), aid, variant)
    return Response(data, media_type=mime, headers={'Cache-Control': 'private, no-store',
                    'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline'})


@router.delete('/{attachment_id}', status_code=204)
def revoke(attachment_id: UUID, request: Request):
    MediaService(get_store()).revoke(identity(request), str(attachment_id))
    return Response(status_code=204)


class RetryBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    session_id: UUID
    source_sequence: int = Field(ge=1)


@router.post('/{attachment_id}/analysis-retries', status_code=202)
def retry(attachment_id: UUID, body: RetryBody, request: Request):
    return MessageIntake(get_store()).receive(str(body.session_id), identity(request),
        request_id=str(body.request_id), content='', attachment_ids=[str(attachment_id)],
        retry_source_sequence=body.source_sequence)
