"""Human support API. Identity comes from AuthMiddleware; roles from the DB."""
import asyncio
import json
import time
import random
from uuid import UUID

from fastapi import APIRouter, Request, Query, Depends
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from app.support.runtime import get_store
from app.support.store import SupportError
from app.config.settings import settings
from starlette.background import BackgroundTask
from app.runtime.streaming import BoundedStreamingResponse, open_subscription

async def request_budget(request: Request):
    from app.runtime.budgets import admit_request
    await asyncio.to_thread(admit_request, identity(request), 'read' if request.method == 'GET' else 'control')


router = APIRouter(prefix="/api/v1", tags=["support"], dependencies=[Depends(request_budget)])


def identity(request):
    user = getattr(request.state, "user_id", None)
    if not user:
        raise SupportError("请先登录", 401)
    return user


def ensure_session(sid, user):
    store = get_store()
    store.ensure(sid, user)
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


class TaskUpdateBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)
    details: dict[str, str | int | None] | None = None
    goal: str | None = Field(default=None, min_length=1, max_length=2000)


@router.post('/support/tickets/{ticket_id}/tasks/{task_id}')
def update_handoff_task(ticket_id: UUID, task_id: UUID, body: TaskUpdateBody, request: Request):
    from app.tasks.handoffs import TaskHandoffs
    store, actor = get_store(), identity(request)
    # Authorize ticket visibility before resolving the customer identity.
    detail = store.detail(str(ticket_id), actor)
    with store.transaction(read_only=True) as tx:
        control = store._control(tx, detail['ticket']['session_id'])
    record = TaskHandoffs(store, control['user_id']).update(str(ticket_id), actor,
        task_id=str(task_id), **body.model_dump())
    return {'task': record.model_dump()}


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


@router.get('/sessions/{session_id}/tasks/history')
def task_history(session_id: UUID, request: Request, before: int | None = Query(None, ge=1),
                 limit: int = Query(30, ge=1, le=100)):
    return get_store().tasks.history(str(session_id), identity(request), before, limit)


@router.post("/sessions/{session_id}/resume-bot")
def resume(session_id: UUID, request: Request):
    return get_store().resume(str(session_id), identity(request))


@router.get("/sessions/{session_id}/messages")
def messages(session_id: UUID, request: Request, after: int = Query(0, ge=0), staff: bool = False, ticket_id: UUID | None = None):
    return get_store().events(str(session_id), identity(request), after, staff=staff,
                              ticket_id=str(ticket_id) if ticket_id else None)


@router.get('/sessions/{session_id}/transcript')
def transcript(session_id: UUID, request: Request, before: int | None = Query(None, ge=1),
               limit: int = Query(50, ge=1, le=100), staff: bool = False, ticket_id: UUID | None = None):
    return get_store().transcript(str(session_id), identity(request), before, limit, staff=staff,
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
    wake, lost, cleanup = await open_subscription(user, sid)

    async def stream():
        cursor = after
        try:
            yield f'retry: {random.randint(2000, 5000)}\n\n'
            while not await request.is_disconnected():
                if lost.is_set():
                    return
                wake.clear()  # Before reading: events arriving during the query cannot be lost.
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
                try:
                    await asyncio.wait_for(wake.wait(), timeout=min(settings.api_sse_catchup_seconds,
                        max(.01, getattr(request.state, 'token_expires_at', 0) - time.time())))
                except TimeoutError:
                    pass
        finally:
            await cleanup()
    return BoundedStreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}, background=BackgroundTask(cleanup))


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
