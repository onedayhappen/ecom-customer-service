"""电商客服 Agent 的 FastAPI 服务入口。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import random
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.background import BackgroundTask
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.api.auth import router as auth_router
from app.api.middleware import AuthMiddleware
from app.config.settings import settings
from app.session.repository import load_session
from app.runtime.chat_queue import ChatQueue
from app.runtime.concurrency import CoordinationUnavailable
from app.support.runtime import get_store
from app.support.store import SupportError
from app.runtime.budgets import RateExceeded, admit_request
from app.runtime.streaming import BoundedStreamingResponse, open_subscription
from app.api.support import router as support_router

auth_scheme = HTTPBearer(auto_error=False)

# 共享客户端供工具使用；模型额度由统一 LlmClient 管理。
_openai_client: Any | None = None
_openai_client_lock = threading.Lock()


_redis_url = settings.api_concurrency_redis_url
_chat_queue = ChatQueue(
    _redis_url,
    prefix=settings.api_chat_queue_prefix,
    ttl=settings.api_chat_job_ttl_seconds,
    max_depth=settings.api_chat_queue_max_depth,
    queue_timeout=settings.api_chat_queue_timeout_seconds,
    run_timeout=settings.api_chat_run_timeout_seconds,
) if _redis_url else None


def _get_openai_client() -> Any:
    """懒加载进程级 OpenAI 客户端单例（线程安全）。"""
    global _openai_client
    if _openai_client is None:
        with _openai_client_lock:
            if _openai_client is None:
                from openai import OpenAI

                _openai_client = OpenAI(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                    timeout=settings.llm_timeout_seconds,
                    max_retries=0,
                )
                # 业务链日志监控：单例创建时对 completions.create 打一次幂等补丁，
                # 后续所有请求零 patch 开销，仅当线程有活跃 tracer 时采集 LLM 调用。
                if settings.observability_enabled:
                    from app.observability.tracer import patch_client

                    patch_client(_openai_client)
    return _openai_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化会话库、审计与连接池；模型和工具执行由独立 Worker 承担。"""
    settings.validate_runtime_configuration()
    from app.runtime.metrics import install
    install()
    from app.db import get_connection_pool

    # 第12期：装配 Trace/Span 核心与审计器（异常 100% 写 agent_audit_log）
    from app.observability.audit import build_audit_logger, set_audit_logger
    from app.observability.trace import TraceManager

    TraceManager.enabled = settings.observability_trace_enabled
    if settings.audit_enabled:
        set_audit_logger(build_audit_logger(
            sample_rate=settings.audit_sample_rate,
            postgres_dsn=settings.audit_postgres_dsn or settings.api_session_postgres_dsn,
            log_dir=settings.audit_log_dir,
        ))

    # 预热 PG 连接池：连接按需懒建，这里先触发单例注册。
    for dsn in {
        settings.api_session_postgres_dsn,
        settings.memory_postgres_dsn,
    }:
        if dsn:
            get_connection_pool(dsn)

    await asyncio.to_thread(get_store().initialize)
    try:
        yield
    finally:
        from app.runtime.notifications import get_hub
        await asyncio.to_thread(get_hub().close)
        from app.rag.remote import close_clients
        await asyncio.to_thread(close_clients)
        from app.db import close_pools
        close_pools()


class CurrentUser(BaseModel):
    user_id: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    request_id: UUID | None = None
    # 高风险写操作第二步必须回传上一轮返回的确认令牌。
    confirmation_token: str | None = Field(default=None, min_length=20, max_length=256)
    task_action: Literal['answer', 'resume', 'pause', 'cancel', 'amend'] | None = None
    task_id: UUID | None = None
    task_version: int | None = Field(default=None, ge=1)
    question_id: UUID | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    intent: str
    confidence: float
    requires_human: bool
    follow_up_question: str | None
    confirmation_token: str | None = None
    support: dict | None = None
    suppressed: bool = False
    question: dict | None = None
    task_outcome: Literal['completed', 'paused', 'needs_human'] | None = None
    control_version: int | None = None


class ChatAcceptedResponse(BaseModel):
    job_id: str
    session_id: str
    status: str = "queued"
    status_url: str
    events_url: str
    control_version: int | None = None


class ChatJobStatusResponse(BaseModel):
    job_id: str
    session_id: str
    status: str
    result: ChatResponse | None = None
    error: str | None = None

# 用于恢复、重置、关闭等动作的简短结果
class SessionResponse(BaseModel):
    session_id: str
    status: str

# 用于读取用户可见的聊天历史
class SessionHistoryMessage(BaseModel):
    role: str
    content: str


class SessionHistoryResponse(BaseModel):
    session_id: str
    status: str
    messages: list[SessionHistoryMessage]

# 后端用密钥生成 JWT；浏览器通过 HttpOnly Cookie 携带，Bearer 仍保留给 CLI/服务间调用。
def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
) -> CurrentUser:
    """验证 Bearer JWT 或 HttpOnly 登录 Cookie。"""

    # 服务端未配置密钥时，返回 503 Service Unavailable，避免服务在“不安全但貌似可用”的状态下接受业务请求
    if not settings.api_jwt_secret:
        raise HTTPException(status_code=503, detail="服务端未配置 API_JWT_SECRET")

    token = credentials.credentials if credentials is not None else request.cookies.get(settings.auth_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="缺少 Bearer Token 或登录 Cookie")
    try:
        claims = jwt.decode(
            token,
            settings.api_jwt_secret,
            algorithms=[settings.api_jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token 无效或已过期") from None

    # 代表当前登录主体的唯一标识，一般放用户 ID
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=401, detail="Token 缺少有效用户身份")
    return CurrentUser(user_id=user_id)


def _normalise_session_id(session_id: str | None) -> str:
    """服务端生成并校验 UUID；会话归属由 PostgreSQL 的 user_id 校验。"""
    if session_id is None:
        normalized_session_id = str(uuid4())
    else:
        try:
            normalized_session_id = str(UUID(session_id))   # UUID(session_id)：校验传入的 session_id 字符串是不是合法 UUID 格式
        except ValueError:
            raise HTTPException(status_code=422, detail="session_id 必须是 UUID") from None

    # PostgreSQL 会话不构造本地文件路径，UUID 是会话主键，user_id 是归属校验条件。
    return normalized_session_id


def _display_messages(messages: list[dict]) -> list[SessionHistoryMessage]:
    """仅返回用户可见消息，过滤工具 Observation 与内部 ReAct 记录。"""
    result = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str):
            result.append(SessionHistoryMessage(role="user", content=content))
        elif message.get("role") == "assistant" and isinstance(content, str):
            reply = _structured_reply(content)
            if reply:
                result.append(SessionHistoryMessage(role="assistant", content=reply))
            elif content and not _next_message_has_same_structured_reply(
                messages, index, content,
            ):
                result.append(SessionHistoryMessage(role="assistant", content=content))
    return result


def _structured_reply(content: str) -> str | None:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    reply = value.get("reply") if isinstance(value, dict) else None
    return reply if isinstance(reply, str) and reply else None


def _next_message_has_same_structured_reply(
    messages: list[dict], index: int, content: str,
) -> bool:
    """兼容旧会话：跳过紧邻结构化回复前的重复纯文本回复。"""
    if index + 1 >= len(messages):
        return False
    next_message = messages[index + 1]
    if next_message.get("role") != "assistant":
        return False
    next_content = next_message.get("content")
    if not isinstance(next_content, str):
        return False
    return _structured_reply(next_content) == content


app = FastAPI(title="Ecom Customer Service API", version="1.0.0", lifespan=lifespan)
# CORS must wrap auth so rejected/expired requests are readable by the UI.
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,     # CORSMiddleware = 跨域中间件，解决前端网页和后端跨域请求浏览器拦截问题。
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "Last-Event-ID"],
)

# 登录认证路由（POST /api/v1/auth/login：用户名密码 → JWT）。
app.include_router(auth_router)
app.include_router(support_router)
from app.api.task_messages import router as task_messages_router
app.include_router(task_messages_router)
from app.api.task_operations import router as task_operations_router
app.include_router(task_operations_router)


@app.exception_handler(SupportError)
async def support_error_handler(request: Request, exc: SupportError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.exception_handler(RateExceeded)
async def rate_error_handler(request: Request, exc: RateExceeded):
    return JSONResponse(status_code=429, content={'detail': str(exc)}, headers={'Retry-After': str(exc.retry_after)})


@app.exception_handler(CoordinationUnavailable)
async def coordination_error_handler(request: Request, exc: CoordinationUnavailable):
    return JSONResponse(status_code=503, content={'detail': str(exc)}, headers={'Retry-After': '2'})

# 注册一个 GET 请求接口，访问路径：http://你的服务地址/health
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> Response:
    """Paused automation still serves history, intake and human support."""
    from app.tasks.rollout import RELEASE
    try:
        with get_store().transaction(read_only=True) as tx:
            state = tx.one('SELECT release,mode FROM task_pipeline_state WHERE singleton=1')
            if not state or state['release'] != RELEASE:
                return JSONResponse(status_code=503, content=dict(status='not_ready', reason='release_mismatch'))
    except Exception:
        return JSONResponse(status_code=503, content=dict(status='not_ready', reason='database_unavailable'))
    return JSONResponse(dict(status='ready', release=RELEASE, automation=state['mode']))


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    """Prometheus 指标出口：LLM/工具/FSM/RT 指标由埋点体系发射，抓取端读取。

    ``prometheus_client`` 未安装时返回 503（观测降级，不影响业务接口）。
    """
    from app.observability.metrics import PROMETHEUS_AVAILABLE

    if not PROMETHEUS_AVAILABLE:
        return Response(
            content="prometheus_client 未安装，指标未启用\n",
            status_code=503, media_type="text/plain",
        )
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/v1/me")
def current_identity(current_user: CurrentUser = Depends(get_current_user)) -> dict:
    """前端仅用此接口校验访问令牌；不暴露会话恢复能力。"""
    return {"user_id": current_user.user_id}


@app.get("/api/v1/sessions/{session_id}", response_model=SessionHistoryResponse)
async def get_session_history(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionHistoryResponse:
    await asyncio.to_thread(admit_request, current_user.user_id, 'read')
    return await asyncio.to_thread(_load_session_history, session_id, current_user.user_id)


def _load_session_history(session_id: str, user_id: str) -> SessionHistoryResponse:
    """同步读取会话历史（DB IO 放线程池，避免阻塞事件循环）。"""
    normalized_session_id = _normalise_session_id(session_id)
    try:
        get_store().state(normalized_session_id, user_id)
    except SupportError as exc:
        if exc.status != 404:
            raise
    else:
        return SessionHistoryResponse(session_id=normalized_session_id, status="active",
            messages=[SessionHistoryMessage(**m) for m in get_store().public_history(normalized_session_id, user_id)])
    loaded = load_session(
        normalized_session_id,
        settings.api_session_storage_backend,
        settings.api_session_postgres_dsn,
        user_id,
    )
    if loaded is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if loaded["metadata"].get("owner_user_id") != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return SessionHistoryResponse(
        session_id=normalized_session_id,
        status=loaded["metadata"].get("status", "active"),
        messages=_display_messages(loaded["messages"]),
    )


@app.post("/api/v1/chat", response_model=ChatAcceptedResponse | ChatResponse)
async def chat(
    request: ChatRequest,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
) -> ChatAcceptedResponse | ChatResponse:
    raise HTTPException(410, '旧单消息执行入口已退役，请更新客户端并使用 /api/v1/task-messages')


def _job_status_for_user(job_id: str, user_id: str) -> dict:
    durable = get_store().jobs.get(job_id, user_id)
    if durable is not None:
        return durable
    status_data = _chat_queue.get(job_id) if _chat_queue else None
    if not status_data or status_data.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return status_data


@app.get("/api/v1/chat/{job_id}", response_model=ChatJobStatusResponse)
async def chat_status(
    job_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> ChatJobStatusResponse:
    await asyncio.to_thread(admit_request, current_user.user_id, 'read')
    data = await asyncio.to_thread(_job_status_for_user, job_id, current_user.user_id)
    result = data.get("result")
    return ChatJobStatusResponse(
        job_id=job_id,
        session_id=str(data.get("session_id", "")),
        status=str(data.get("status", "unknown")),
        result=ChatResponse.model_validate(result) if isinstance(result, dict) else None,
        error=data.get("error"),
    )


@app.get("/api/v1/chat/{job_id}/events")
async def chat_events(
    job_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    await asyncio.to_thread(admit_request, current_user.user_id, 'read')
    data = await asyncio.to_thread(_job_status_for_user, job_id, current_user.user_id)
    wake, lost, cleanup = await open_subscription(current_user.user_id, data['session_id'])

    async def event_stream():
        last_status = None
        deadline = asyncio.get_running_loop().time() + settings.api_chat_job_ttl_seconds
        try:
            yield f'retry: {random.randint(2000, 5000)}\n\n'
            while asyncio.get_running_loop().time() < deadline and not await request.is_disconnected():
                if lost.is_set():
                    return
                wake.clear()
                remaining = getattr(request.state, 'token_expires_at', 0) - time.time()
                if remaining <= 0:
                    yield 'event: revoked\ndata: {}\n\n'
                    return
                data = await asyncio.to_thread(_job_status_for_user, job_id, current_user.user_id)
                status_value = str(data.get('status', 'unknown'))
                if status_value != last_status:
                    payload = {key: data[key] for key in ('job_id', 'status', 'result', 'error') if data.get(key) is not None}
                    yield f'event: status\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                    last_status = status_value
                else:
                    yield ': heartbeat\n\n'
                if status_value in {'completed', 'failed', 'expired', 'interrupted', 'superseded'}:
                    return
                try:
                    await asyncio.wait_for(wake.wait(), min(settings.api_sse_catchup_seconds, remaining))
                except TimeoutError:
                    pass
        finally:
            await cleanup()

    return BoundedStreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(cleanup),
    )


@app.post("/api/v1/sessions/{session_id}/reset", response_model=SessionResponse)
async def reset_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    await asyncio.to_thread(get_store().state, _normalise_session_id(session_id), current_user.user_id)
    raise SupportError('会话记录用于追溯，请新建咨询；人工接管请使用对应入口')


@app.post("/api/v1/sessions/{session_id}/close", response_model=SessionResponse)
async def close_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    await asyncio.to_thread(get_store().state, _normalise_session_id(session_id), current_user.user_id)
    raise SupportError('会话记录用于追溯，请新建咨询；人工接管请使用对应入口')
