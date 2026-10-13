"""Human support API. Identity comes from AuthMiddleware; roles from the DB."""
import asyncio
import json
import time
from uuid import UUID

from fastapi import APIRouter, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from app.support.runtime import get_store
from app.support.store import SupportError

router = APIRouter(prefix="/api/v1", tags=["support"])


def identity(request):
    user = getattr(request.state, "user_id", None)
    if not user:
        raise SupportError("请先登录", 401)
    return user


def ensure_session(sid, user):
    from app.config.settings import settings
    from app.session.repository import load_session
    store = get_store()
    try:
        return store.state(sid, user)
    except SupportError as exc:
        if exc.status != 404:
            raise
    # Check old ownership without a user filter before importing old conversations.
    old = load_session(sid, settings.api_session_storage_backend, settings.api_session_postgres_dsn)
    if old and old["metadata"].get("owner_user_id") != user:
        raise SupportError("会话不存在", 404)
    from app.api.main import _display_messages
    history = [m.model_dump() for m in _display_messages(old.get("messages", []))] if old else []
    store.ensure(sid, user, history)
    return store.state(sid, user)


class StaffBody(BaseModel):
    role: Literal["agent", "admin"]
    enabled: bool = True


class ChangeBody(BaseModel):
    version: int = Field(ge=1)
    action: Literal["claim", "assign", "waiting_customer", "resolved", "requeue"]
    assignee: str | None = Field(default=None, max_length=128)


class ReplyBody(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    client_message_id: UUID
    internal: bool = False


class ReconcileBody(BaseModel):
    status: Literal["succeeded", "failed"]
    evidence: str = Field(min_length=10, max_length=2000)


class StoppedBody(BaseModel):
    execution_stopped: Literal[True]
    evidence: str = Field(min_length=10, max_length=2000)


@router.get("/support/me")
def support_me(request: Request):
    user = identity(request)
    return {"user_id": user, "role": get_store().role(user)}


@router.get("/support/staff")
def staff(request: Request):
    return get_store().staff(identity(request))


@router.post("/support/staff/{user_id}")
def update_staff(user_id: str, body: StaffBody, request: Request):
    get_store().update_staff(identity(request), user_id, body.role, body.enabled)
    return {"success": True}


@router.post("/sessions/{session_id}/handoff")
def handoff(session_id: UUID, request: Request):
    sid, user = str(session_id), identity(request)
    ensure_session(sid, user)
    return get_store().handoff(sid, user)


@router.get("/sessions/{session_id}/support-state")
def state(session_id: UUID, request: Request):
    return ensure_session(str(session_id), identity(request))


@router.get("/sessions/{session_id}/tasks")
def tasks(session_id: UUID, request: Request):
    return get_store().tasks.list(str(session_id), identity(request))


@router.post("/sessions/{session_id}/resume-bot")
def resume(session_id: UUID, request: Request):
    return get_store().resume(str(session_id), identity(request))


@router.get("/sessions/{session_id}/messages")
def messages(session_id: UUID, request: Request, after: int = Query(0, ge=0), staff: bool = False, ticket_id: UUID | None = None):
    return get_store().events(str(session_id), identity(request), after, staff=staff,
                              ticket_id=str(ticket_id) if ticket_id else None)


@router.get("/sessions/{session_id}/support-events")
async def events(session_id: UUID, request: Request, after: int = Query(0, ge=0), staff: bool = False, ticket_id: UUID | None = None):
    sid, user = str(session_id), identity(request)
    tid = str(ticket_id) if ticket_id else None
    try:
        after = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise SupportError("事件游标不合法", 422)
    await asyncio.to_thread(get_store().events, sid, user, after, staff=staff, ticket_id=tid)

    async def stream():
        cursor = after
        # Durable catch-up also works during Redis outages. Bounded 2s polling,
        # no queue reaper; only one connection per open conversation in the UI.
        while not await request.is_disconnected():
            if time.time() >= getattr(request.state, "token_expires_at", 0):
                yield 'event: revoked\ndata: {}\n\n'
                return
            try:
                data = await asyncio.to_thread(get_store().events, sid, user, cursor, staff=staff, ticket_id=tid)
            except SupportError:
                yield 'event: revoked\ndata: {}\n\n'
                return
            if data["cursor"] != cursor:
                cursor = data["cursor"]
                yield f"id: {cursor}\nevent: update\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if len(data["events"]) == 100:
                    continue
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/support/tickets")
def tickets(request: Request, view: Literal["queue", "mine", "all"] = "queue", offset: int = Query(0, ge=0)):
    return get_store().list_tickets(identity(request), view, offset)


@router.get("/support/tickets/{ticket_id}")
def detail(ticket_id: UUID, request: Request):
    return get_store().detail(str(ticket_id), identity(request))


@router.post("/support/tickets/{ticket_id}/transition")
def transition(ticket_id: UUID, body: ChangeBody, request: Request):
    return get_store().change(str(ticket_id), identity(request), body.version, body.action, body.assignee)


@router.post("/support/tickets/{ticket_id}/messages")
def reply(ticket_id: UUID, body: ReplyBody, request: Request):
    if not body.content.strip():
        raise SupportError("消息不能为空", 422)
    return get_store().reply(str(ticket_id), identity(request), body.content, str(body.client_message_id), body.internal)


@router.post("/support/operations/{operation_id}/reconcile")
def reconcile(operation_id: UUID, body: ReconcileBody, request: Request):
    get_store().reconcile(str(operation_id), identity(request), body.status, body.evidence)
    return {"success": True}


@router.post("/support/operations/{operation_id}/mark-unknown")
def mark_unknown(operation_id: UUID, body: StoppedBody, request: Request):
    get_store().mark_unknown(str(operation_id), identity(request), body.evidence)
    return {"success": True}
