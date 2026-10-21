"""电商客服 Agent 的 FastAPI 服务入口。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import random
from hashlib import sha256
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime
from dataclasses import replace
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

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
from app.session.lifecycle import compact_messages, context_messages
from app.runtime.chat_queue import ChatJob, ChatQueue, ChatWorker, SessionBusy, RequestConflict
from app.runtime.concurrency import DistributedLease, SessionLock, CoordinationUnavailable, execution_lease_guard
from app.support.runtime import get_store, bot_scope, current_scope, explicit_handoff, check_bot
from app.support.store import SupportError, BotInterrupted
from app.runtime.budgets import RateExceeded, admit_request, agent_partition
from app.runtime.streaming import BoundedStreamingResponse, open_subscription
from app.api.support import router as support_router, ensure_session

auth_scheme = HTTPBearer(auto_error=False)

# 模块级共享 OpenAI 客户端与 LLM 并发信号量：
# - 客户端复用避免每个请求重建 httpx 连接（TCP 握手 / TLS 是大头开销）；
# - 信号量限制同时在飞的 LLM 慢调用数量，防止打爆上游、防止 FastAPI 线程池
#   被长时间占满（否则连 /health 等轻请求都会被拖入排队）。
_openai_client: Any | None = None
_openai_client_lock = threading.Lock()


_redis_url = settings.api_concurrency_redis_url
_llm_lease = DistributedLease(
    _redis_url, f"{settings.api_chat_queue_prefix}:agent-slots",
    settings.api_max_concurrent_llm,
    lease_seconds=settings.api_distributed_lease_seconds,
)
_chat_queue = ChatQueue(
    _redis_url,
    prefix=settings.api_chat_queue_prefix,
    ttl=settings.api_chat_job_ttl_seconds,
    max_depth=settings.api_chat_queue_max_depth,
    queue_timeout=settings.api_chat_queue_timeout_seconds,
    run_timeout=settings.api_chat_run_timeout_seconds,
) if _redis_url else None
_chat_worker: ChatWorker | None = None
_chat_worker_lock = threading.Lock()


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
    """启动时预热共享组件（RAG embedding 模型、PostgreSQL 连接池），退出时清理。

    预热在后台进行，不阻塞服务启动；失败静默（例如本地没有索引或模型），
    首次真实检索时仍会按原逻辑懒加载。
    """
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

    async def _warmup_rag() -> None:
        try:
            from app.tools.knowledge import _get_retriever

            await asyncio.to_thread(_get_retriever)
        except Exception:
            # 预热失败不阻塞服务；search_knowledge 会按原有路径降级提示。
            pass

    warmup_task = None
    if settings.api_chat_execution_mode == "sync" or settings.api_chat_worker_enabled:
        warmup_task = asyncio.create_task(_warmup_rag())
    if settings.api_chat_execution_mode == "async":
        await asyncio.to_thread(get_store().initialize)
    if settings.api_chat_execution_mode == "async" and settings.api_chat_worker_enabled:
        _ensure_chat_worker()
    try:
        yield
    finally:
        if warmup_task:
            warmup_task.cancel()
        await asyncio.to_thread(_stop_chat_worker)
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


def _build_engine(
    user_id: str, session_id: str | None, *, query: str = '', task_id: str | None = None,
) -> tuple[str, list[dict]]:
    """新链路执行引擎入口（意图识别 → DataPlan → dispatch）。

    返回 (规范化 session_id, 会话历史 messages)。历史供微调意图分类器
    构造多轮上下文（末轮由调用方追加当前 user 消息）；引擎本身不持有会话状态。
    """
    normalized_session_id = _normalise_session_id(session_id)
    if current_scope():
        if task_id:
            history = get_store().tasks.context(normalized_session_id, user_id, task_id,
                                                query=query, exclude_latest_user=True)
        else:
            history = get_store().context(normalized_session_id, user_id,
                                          query=query, exclude_latest_user=True)
        return normalized_session_id, history

    if task_id:
        return normalized_session_id, get_store().tasks.context(
            normalized_session_id, user_id, task_id, query=query,
        )

    if session_id is not None:
        # 显式 session_id 仅表示客户端继续当前聊天线程；不再有空闲恢复窗口。
        loaded = load_session(
            normalized_session_id,
            settings.api_session_storage_backend,
            settings.api_session_postgres_dsn,
            user_id,
        )
        if loaded and loaded["metadata"].get("owner_user_id") not in {None, user_id}:
            raise HTTPException(status_code=403, detail="无权访问该会话")
        return normalized_session_id, context_messages(loaded)
    return normalized_session_id, []


def _persist_session_turn(
    session_id: str,
    user_id: str,
    user_message: str,
    assistant_message: str,
) -> None:
    """保存每一轮会话，并在达到上下文预算时压缩旧消息。

    PostgreSQL 使用 state_version 乐观锁；JSON 后端用跨进程文件锁包住完整的
    读改写事务。即使多个实例同时收到同一个 session 的消息，也不会互相覆盖。
    """
    if current_scope():
        # The outer job transaction publishes the reply only if ownership is current.
        check_bot()
        return
    from app.session.repository import default_session_metadata, save_session
    from app.session.summarizer import summarize

    def save_once() -> None:
        existing = load_session(
            session_id,
            settings.api_session_storage_backend,
            settings.api_session_postgres_dsn,
            user_id,
        ) or {
            "messages": [], "summary": None,
            "metadata": default_session_metadata(user_id), "state_version": 0,
        }
        messages = list(existing.get("messages", [])) + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]

        def summarize_old(old_messages: list[dict], previous_summary: str | None) -> str:
            return summarize(
                _get_openai_client(), settings.model_name, old_messages, previous_summary,
            )

        messages, summary = compact_messages(messages, existing.get("summary"), summarize_old)
        metadata = dict(existing.get("metadata") or default_session_metadata(user_id))
        metadata["owner_user_id"] = user_id
        metadata["last_active_at"] = datetime.now().isoformat(timespec="seconds")
        save_session(
            session_id, messages, summary, metadata,
            settings.api_session_storage_backend, settings.api_session_postgres_dsn,
            expected_state_version=int(existing.get("state_version", 0) or 0),
        )

    if settings.api_session_storage_backend == "json":
        # JSON 只用于离线兼容，但也必须在多 worker 下保证完整读改写互斥。
        from app.memory.file_lock import ProcessFileLock
        from app.session.repository import JSON_SESSION_DIR

        with ProcessFileLock(JSON_SESSION_DIR / f"{session_id}.lock"):
            save_once()
        return

    from app.session.repository import SessionConflict
    for attempt in range(3):
        try:
            save_once()
            return
        except SessionConflict:
            if attempt == 2:
                raise
            # 其他实例刚写入，重新读取并在下一轮合并最新消息。
            continue


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
    """Admission depends on the durable store; Redis outages leave jobs pending."""
    if settings.api_chat_execution_mode == "async":
        if _chat_queue is None:
            return Response(content='{"status":"not_ready","reason":"queue_not_configured"}',
                            status_code=503, media_type="application/json")
        try:
            with get_store().transaction(read_only=True) as tx:
                tx.one('SELECT 1 AS ready')
        except Exception:
            return Response(content='{"status":"not_ready","reason":"database_unavailable"}',
                            status_code=503, media_type="application/json")
    return Response(content='{"status":"ready"}', media_type="application/json")


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
    if loaded["metadata"].get("owner_user_id") not in {None, user_id}:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return SessionHistoryResponse(
        session_id=normalized_session_id,
        status=loaded["metadata"].get("status", "active"),
        messages=_display_messages(loaded["messages"]),
    )


def _run_chat_job(job: ChatJob) -> dict:
    from app.runtime.metrics import EXECUTION
    import time
    started_at = time.monotonic()
    try:
        result = _execute_chat_job(job)
    except Exception:
        EXECUTION.labels(result='error').observe(time.monotonic() - started_at)
        raise
    EXECUTION.labels(result='suppressed' if result.get('suppressed') else 'completed').observe(time.monotonic() - started_at)
    get_store().jobs.finish(job, response=result)
    return result


def _execute_chat_job(job: ChatJob) -> dict:
    """Worker 入口：全局 LLM 租约 + 本地槽位 + session 锁三层保护。"""
    request = ChatRequest(
        message=job.message,
        session_id=job.session_id,
        confirmation_token=job.confirmation_token,
    )
    task_route = get_store().tasks.turn(job.session_id, job.user_id, job.job_id) if job.control_version is not None else {}
    # Jobs queued before this rollout belong only to the original bot epoch.
    # Never grant an old job the current epoch after a human takeover.
    if job.control_version is None:
        ensure_session(job.session_id, job.user_id)
        job = replace(job, control_version=0)
    scope = bot_scope(get_store(), job.session_id, job.user_id, job.control_version)
    started = False
    try:
        with scope:
            if job.control_version is not None:
                cached = get_store().bot_result(job.session_id, job.user_id, job.job_id)
                if cached:
                    return cached
            get_store().start_bot(job.session_id, job.user_id, job.control_version, job.job_id)
            started = True
            if task_route.get('reply'):
                result = ChatResponse(session_id=job.session_id, reply=task_route['reply'], intent='answer_question',
                                      confidence=1, requires_human=False, follow_up_question=None, control_version=job.control_version)
                get_store().publish_bot(job.session_id, job.user_id, job.control_version, job.job_id, result.reply, result.model_dump())
                return result.model_dump()
            if task_route.get('task_id'):
                request.task_id = UUID(task_route['task_id'])
                request.task_version = task_route['task_version']
                if not settings.assistant_just_react and not task_route.get('confirmation'):
                    request.message = task_route['message']
            lost_lease = threading.Event()
            with execution_lease_guard(lost_lease), agent_partition(job.user_id, lost_lease), _llm_lease.slot(settings.api_queue_wait_seconds, lost_event=lost_lease) as acquired:
                if not acquired:
                    raise TimeoutError("Agent 全局并发配额暂时耗尽")
                result = _handle_chat(request, job.user_id)
                result.control_version = job.control_version
                if job.control_version is not None:
                    check_bot()
                    if result.requires_human:
                        state = get_store().handoff(job.session_id, job.user_id, "agent_escalation")
                        return _support_response(state).model_dump()
                    get_store().publish_bot(job.session_id, job.user_id, job.control_version, job.job_id, result.reply, result.model_dump())
                return result.model_dump()
    except BotInterrupted:
        result = _support_response(get_store().state(job.session_id, job.user_id))
        result.suppressed = True
        return result.model_dump()
    except CoordinationUnavailable:
        get_store().failed_job(job.session_id, job.user_id, job.job_id, interrupted=True)
        raise
    except Exception:
        if started:
            get_store().jobs.finish(job, error='任务执行失败，请查看会话或转人工核实')
        raise
    finally:
        if started:
            get_store().release_bot(job.session_id, job.user_id, job.job_id)


def _support_response(state: dict) -> ChatResponse:
    return ChatResponse(session_id=state["session_id"], reply="", intent="human_handoff",
                        confidence=1, requires_human=state["mode"] != "bot",
                        follow_up_question=None, support=state, control_version=state['control_version'])


def _ensure_chat_worker() -> None:
    global _chat_worker
    if _chat_worker is not None:
        _chat_worker.start()
        return
    with _chat_worker_lock:
        if _chat_worker is None:
            _chat_worker = ChatWorker(
                _chat_queue,
                _run_chat_job,
                concurrency=settings.api_chat_worker_concurrency,
            )
        _chat_worker.start()


def _stop_chat_worker() -> None:
    global _chat_worker
    with _chat_worker_lock:
        if _chat_worker is not None:
            _chat_worker.stop()
            _chat_worker = None


def _run_chat_sync(request: ChatRequest, user_id: str) -> ChatResponse:
    job = ChatJob(
        job_id=uuid4().hex,
        user_id=user_id,
        session_id=_normalise_session_id(request.session_id),
        message=request.message,
        confirmation_token=request.confirmation_token,
    )
    result = _run_chat_job(job)
    return ChatResponse.model_validate(result)


@app.post("/api/v1/chat", response_model=ChatAcceptedResponse | ChatResponse)
async def chat(
    request: ChatRequest,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
) -> ChatAcceptedResponse | ChatResponse:
    """提交客服任务。

    async 模式只做校验和入队，Agent 执行由 Worker 完成；sync 模式保留给
    本地兼容调试。生产配置应使用 async，并通过状态接口或 SSE 读取结果。
    """
    request_key = str(request.request_id or uuid4())
    job_id = uuid5(NAMESPACE_URL, current_user.user_id + ":" + request_key).hex
    normalized_session_id = _normalise_session_id(
        request.session_id or str(uuid5(NAMESPACE_URL, "session:" + job_id)))
    request.session_id = normalized_session_id
    from app.tasks.routing import parse
    control_request = request.task_action or explicit_handoff(request.message)
    if not settings.assistant_just_react:
        control_request = control_request or parse(request.message)['action']
    try:
        await asyncio.to_thread(admit_request, current_user.user_id, 'control' if control_request else 'chat')
    except RateExceeded as exc:
        raise HTTPException(429, str(exc), headers={'Retry-After': str(exc.retry_after)}) from exc
    # Preserve idempotent replay of requests made before task fields were added.
    has_task_input = any((request.task_action, request.task_id, request.task_version, request.question_id))
    fingerprint_input = json.dumps(request.model_dump(mode='json', exclude={'request_id','session_id','message'}), sort_keys=True) if has_task_input else (request.confirmation_token or '')
    await asyncio.to_thread(ensure_session, normalized_session_id, current_user.user_id)
    routing = await asyncio.to_thread(get_store().receive, normalized_session_id, current_user.user_id,
                                     request.message, job_id, explicit_handoff=explicit_handoff(request.message),
                                     request_fingerprint=sha256(fingerprint_input.encode()).hexdigest(),
                                     task_input=request.model_dump(mode='json'),
                                     delivery=({'max_pending': settings.api_chat_queue_max_depth,
                                                'queue_timeout': settings.api_chat_queue_timeout_seconds,
                                                'run_timeout': settings.api_chat_run_timeout_seconds}
                                               if settings.api_chat_execution_mode == 'async' else None))
    if routing["mode"] != "bot" or routing["route_version"] != routing["control_version"]:
        return _support_response(routing)
    cached = await asyncio.to_thread(get_store().bot_result, normalized_session_id, current_user.user_id, job_id)
    if cached:
        await asyncio.to_thread(get_store().release_bot, normalized_session_id, current_user.user_id, job_id)
        return ChatResponse.model_validate(cached)

    job = ChatJob(
        job_id=job_id,
        user_id=current_user.user_id,
        session_id=normalized_session_id,
        message=request.message,
        confirmation_token=request.confirmation_token,
        control_version=routing["route_version"],
    )
    task_route = await asyncio.to_thread(get_store().tasks.turn, normalized_session_id, current_user.user_id, job_id)
    if settings.api_chat_execution_mode != "async" or task_route.get('reply'):
        return ChatResponse.model_validate(await asyncio.to_thread(_run_chat_job, job))
    # The receive transaction already committed the durable outbox record.
    # Dispatch is independent of this request and survives API/Redis outages.
    response.status_code = 202
    return ChatAcceptedResponse(
        job_id=job.job_id,
        session_id=job.session_id,
        status="queued",
        status_url=f"/api/v1/chat/{job.job_id}",
        events_url=f"/api/v1/chat/{job.job_id}/events",
        control_version=routing['control_version'],
    )


def _job_status_for_user(job_id: str, user_id: str) -> dict:
    durable = get_store().jobs.get(job_id, user_id)
    if durable is not None:
        return durable
    status_data = _chat_queue.get(job_id) if _chat_queue else None
    if not status_data or status_data.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if status_data.get("status") in {"expired", "failed", "interrupted"}:
        sid = status_data["session_id"]
        get_store().failed_job(sid, user_id, job_id, status_data["status"] == "interrupted")
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


def _order_locate_execute(user_id: str):
    """查询改写层「商品定位」用的工具执行器。

    在函数内延迟导入本地工具，避免服务启动路径（import 本模块时）拉入
    chromadb / sentence-transformers 等重依赖；仅在真正发生商品指代定位时才加载。
    调用前设置请求级用户上下文，保证 ``list_user_orders`` 按用户隔离订单。
    """
    from app.tools.product import query_product
    from app.tools.request_context import reset_current_user_id, set_current_user_id
    from app.tools.user_orders import list_user_orders

    def execute(name: str, args: dict) -> dict:
        token = set_current_user_id(user_id)
        try:
            if name == "query_product":
                return query_product(**args)
            if name == "list_user_orders":
                return list_user_orders()
            return {"success": False, "error": f"不支持的工具: {name}"}
        finally:
            reset_current_user_id(token)

    return execute


def _handle_chat(request: ChatRequest, user_id: str) -> ChatResponse:
    """同步处理单条 chat 消息（意图识别 → 查询改写 → Agent 执行）。

    业务链日志监控：开启 BusinessTracer，各阶段直埋 record，异常记录 error
    后原样抛出（FastAPI 全局处理），finally 统一 finish 写 JSONL。
    """
    tracer = None
    if settings.observability_enabled:
        from app.observability.tracer import BusinessTracer

        tracer = BusinessTracer.start(request.session_id, user_id, request.message)
    try:
        return _handle_chat_inner(request, user_id, tracer)
    except Exception as e:  # noqa: BLE001 —— 观测不应改变业务异常行为
        if tracer is not None:
            tracer.set_error(f"{type(e).__name__}: {e}")
        raise
    finally:
        if tracer is not None:
            tracer.finish()


def _handle_chat_inner(
    request: ChatRequest, user_id: str, tracer,
) -> ChatResponse:
    """原 _handle_chat 主体；tracer 为 None 时埋点全部跳过，行为与旧版一致。

    第12期：StateObserver 把执行链建模为状态流转（INTENT→REWRITE→PLAN→
    EXECUTE），产出与 @fsm_state_observation 装饰器同构的状态事件；
    异常路径统一 close 标记残留状态（观测层有状态视图，业务层无状态机）。
    """
    from app.observability.fsm import StateObserver
    from app.tools.request_context import reset_current_user_id, set_current_user_id
    from app.session.request_budget import ContextBudgetExceeded
    from app.memory.runtime import build_memory_manager
    from app.tools.memory_tool import set_memory_manager, reset_memory_manager

    # 整个 chat 处理期间注入请求级用户身份：订单查询/写操作按
    # get_current_user_id() 归属（order_service 只信 ContextVar，不信任
    # 模型参数）。此前仅查询改写层注入了上下文，Workflow/ReAct 的工具
    # 调用拿不到身份，返回"缺少用户身份上下文"，订单查询恒为空。
    token = set_current_user_id(user_id)
    observer = StateObserver()
    memory_token = set_memory_manager(None)
    try:
        if settings.memory_enabled:
            try:
                from app.support.runtime import get_store
                set_memory_manager(build_memory_manager(user_id, store=get_store()))
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Long-term memory load unavailable: %s", type(exc).__name__)
        return _handle_chat_inner_impl(request, user_id, tracer, observer)
    except ContextBudgetExceeded:
        observer.close(error="context_budget_exceeded")
        reply = '本次需要处理的信息过多，已暂停当前任务。请简要重述要处理的订单和问题，或转人工继续。'
        sid = _normalise_session_id(request.session_id)
        _persist_session_turn(sid, user_id, request.message, reply)
        return ChatResponse(session_id=sid, reply=reply, intent='answer_question', confidence=1.0,
                            requires_human=False, follow_up_question=None, task_outcome='paused')
    except Exception:
        observer.close(error="unhandled_exception")
        raise
    finally:
        reset_memory_manager(memory_token)
        reset_current_user_id(token)


def _handle_chat_inner_impl(
    request: ChatRequest, user_id: str, tracer, observer,
) -> ChatResponse:
    """Unified conversation by default; the old route remains for rollback only."""
    if settings.assistant_just_react:
        from app.api.conversation import handle_conversation
        return handle_conversation(request, user_id, tracer, observer)
    session_id, history = _build_engine(user_id, request.session_id, query=request.message,
                                        task_id=str(request.task_id) if request.task_id else None)
    request.session_id = session_id

    from app.agents.engine import AgentEngine
    from app.data_plan.enums import Intent
    from app.intent import recognize
    from app.intent.query_rewrite import make_llm_disambiguator, rewrite_query
    from app.reliability import MutationGuard

    from app.memory.case_postgres import create_case_manager
    case_manager = create_case_manager(user_id)

    # 若上一轮在多订单中要求选择，本轮只接受明确序号或候选订单号。
    # 选中后恢复原始请求并注入订单号，不让“第 2 个”交给模型猜测。
    selection = case_manager.consume_order_selection(session_id, request.message) if not request.task_id else None
    if selection and selection.get("invalid"):
        lines = ["请回复候选序号或完整订单号，例如“2”："]
        lines.extend(
            f"{index}. {item['order_id']}：{item.get('items_summary', '')}（{item.get('status', '')}）"
            for index, item in enumerate(selection["candidates"], 1)
        )
        _persist_session_turn(session_id, user_id, request.message, "\n".join(lines))
        return ChatResponse(
            session_id=session_id, reply="\n".join(lines), intent="answer_question",
            confidence=1.0, requires_human=False, follow_up_question=None,
        )
    effective_message = request.message
    selected_intent = None
    task = None
    if request.task_id:
        task = get_store().tasks.get(session_id, user_id, str(request.task_id))
        selected_intent = task['intent']
        if request.confirmation_token:
            from app.tasks.routing import effective_text
            effective_message = effective_text(task)
    if selection and selection.get("order_id"):
        effective_message = f"{selection['message']} {selection['order_id']}"
        selected_intent = selection["intent"]

    # 第一步：意图识别（微调分类器，判断「想干嘛」）。
    context = [*history, {"role": "user", "content": effective_message}]
    observer.enter("INTENT")
    intent = Intent(selected_intent) if selected_intent else recognize(effective_message, messages=context)
    if not request.task_id and current_scope():
        scope = current_scope()
        task = get_store().tasks.adopt(session_id, user_id, scope[3], intent.value, effective_message)
        if task:
            request.task_id = UUID(task['task_id'])
            # Classification can create a new task after session context was
            # read. Reassemble to avoid executing with another task's orders.
            _, history = _build_engine(user_id, session_id, query=effective_message,
                                        task_id=task['task_id'])
            context = [*history, {"role": "user", "content": effective_message}]
    observer.leave("INTENT", to_state="REWRITE", intent=intent.value)
    if tracer is not None:
        tracer.record(
            "intent",
            intent=intent.value,
            source="selection" if selected_intent else "classifier",
        )

    # 第二步：查询改写（意图感知的实体消歧）——只对需要订单定位的意图消歧
    # 「指哪个」；指代消歧用 LLM 判断「查 Case 记忆（业务事项）还是查订单库
    # （商品/订单）」，其他场景直接放行。查询改写的 LLM 调用走统一 LlmClient
    # （scene=rewrite，与主生成分开统计 token）。
    from app.observability.llm_client import SCENE_REWRITE, LlmClient

    llm_rewrite = LlmClient(
        raw_client=_get_openai_client(), model=settings.model_name,
        default_scene=SCENE_REWRITE,
    )
    observer.enter("REWRITE")
    rewritten = rewrite_query(
        effective_message,
        intent=intent,
        case_manager=case_manager,
        # Historical summary/retrieval is reference material for generation,
        # never an implicit selection of an order for an operation.
        session_context=[m for m in history if m.get('role') in {'user', 'assistant'}],
        task_order_id=(task or {}).get('slots', {}).get('order_id'),
        disambiguator=make_llm_disambiguator(llm_rewrite, settings.model_name),
        execute=_order_locate_execute(user_id),
    )
    if tracer is not None:
        tracer.record(
            "rewrite",
            message=rewritten.message,
            source=getattr(rewritten, "source", None),
            clarification=bool(rewritten.clarification),
            order_candidates=[
                item.get("order_id") for item in (rewritten.order_candidates or [])
            ],
            # 第13期：把实体抽取结果打进日志，便于线上确认 order_status 等
            # 是否被正确抽取（此前日志看不到 entities，出问题只能盲查）。
            entities={
                "order_id": rewritten.entities.order_id,
                "tracking_number": rewritten.entities.tracking_number,
                "order_status": rewritten.entities.order_status,
                "product_keywords": list(rewritten.entities.product_keywords),
                "has_cross_session_ref": rewritten.entities.has_cross_session_ref,
                "has_product_ref": rewritten.entities.has_product_ref,
            },
        )
    if rewritten.clarification:
        observer.leave("REWRITE", to_state="RESPONSE", clarification=True)
        if rewritten.order_candidates and not request.task_id:
            case_manager.request_order_selection(
                session_id, effective_message, intent.value, list(rewritten.order_candidates),
            )
        if tracer is not None:
            tracer.set_response(
                reply=rewritten.clarification, requires_human=False,
                follow_up_question=None, intent=intent.value,
                execution="clarification", confidence=1.0,
            )
        _persist_session_turn(session_id, user_id, request.message, rewritten.clarification)
        return ChatResponse(
            session_id=session_id,
            reply=rewritten.clarification,
            intent=intent.value,
            confidence=1.0,
            requires_human=False,
            follow_up_question=None,
            question={'field': 'order_id' if rewritten.order_candidates else 'details', 'prompt': rewritten.clarification,
                      'options': [{'value': item['order_id'], 'label': f"{item['order_id']}：{item.get('items_summary', '')}"} for item in rewritten.order_candidates]},
        )

    observer.leave("REWRITE", to_state="PLAN", source=getattr(rewritten, "source", "none"))

    # 对写操作：确认令牌替代不可信的客户端布尔 confirmed 标记。
    # 令牌具体绑定工具名与参数，错误意图或参数变化都会被 MutationGuard 拒绝。
    guard = MutationGuard(case_manager, session_id)
    pending = guard.peek_confirmation(request.confirmation_token) if request.confirmation_token else None
    if pending and request.message.strip().strip("。！! ") not in {"确认", "确认执行", "是", "是的", "同意"}:
        pending = None
        request.confirmation_token = None
    resolution = case_manager.resolve(rewritten.message)
    turn_id = case_manager.begin_turn(session_id, rewritten.message, resolution)
    if request.confirmation_token and pending is None:
        if tracer is not None:
            tracer.set_response(
                reply="确认令牌无效、已过期或已使用，请重新发起操作。",
                requires_human=False, follow_up_question=None,
                intent=intent.value, execution="rule", confidence=1.0,
            )
        return ChatResponse(
            session_id=session_id, reply="确认令牌无效、已过期或已使用，请重新发起操作。",
            intent=intent.value, confidence=1.0, requires_human=False, follow_up_question=None,
            task_outcome='paused',
        )
    if pending is not None:
        # 确认轮不重新依赖模型规划：直接回到已审计的原动作和原参数。
        # 这避免“确认”被分类为普通对话或模型在第二轮改写参数。
        from app.data_plan.enums import Execution, Intent
        from app.execution.types import ExecutionResult
        # 确认可能发生在用户首次请求之后数分钟；订单状态可能已经变化。
        # 对依赖“未发货”前提的动作重新做一次权威校验，避免过期确认令牌
        # 在订单已发货/取消后仍执行写操作。
        pending_order_id = str(pending.get("arguments_dict", {}).get("order_id") or "")
        pending_action = str(pending.get("action") or "")
        if pending_order_id and pending_action in {"update_address", "cancel_order", "expedite_logistics"}:
            from app.tools.registry import execute_tool
            current_order = execute_tool("query_order", {"order_id": pending_order_id})
            if isinstance(current_order, str):
                try:
                    current_order = json.loads(current_order)
                except json.JSONDecodeError:
                    current_order = {}
            current_status = (current_order.get("order") or {}).get("status") if isinstance(current_order, dict) else None
            if not isinstance(current_order, dict) or not current_order.get("success"):
                return ChatResponse(
                    session_id=session_id,
                    reply=f"订单 {pending_order_id} 当前无法核实，操作未执行，请稍后重试或联系人工客服。",
                    intent=pending.get("intent", intent.value), confidence=1.0,
                    requires_human=True,
                    follow_up_question="当前无法核实，尚未创建人工工单，请稍后重试或联系客服。",
                )
            if current_status != "pending":
                return ChatResponse(
                    session_id=session_id,
                    reply=f"订单 {pending_order_id} 当前状态已变为「{current_status}」，无法执行该操作。",
                    intent=pending.get("intent", intent.value), confidence=1.0,
                    requires_human=False, follow_up_question=None,
                )
        observer.enter("EXECUTE", execution="confirmation")
        outcome = guard.execute(
            pending["intent"], pending["action"], pending["arguments_dict"],
            request.confirmation_token, lambda name, args: __import__(
                "app.tools.registry", fromlist=["execute_tool"]
            ).execute_tool(name, args),
        )
        result = ExecutionResult(
            intent=Intent(pending["intent"]), execution=Execution.WORKFLOW,
            reply=outcome.result.get("message") if outcome.result.get("success") else (
                f"操作未执行：{outcome.result.get('error', '请重新发起操作')}"
            ),
            replayed=outcome.replayed,
        )
        result.add_step(pending["action"], pending["arguments_dict"], outcome.result)
        observer.leave("EXECUTE", to_state="RESPONSE", replayed=outcome.replayed)
    else:
        # Retrieve only after intent/rewrite and task adoption. Historical user
        # preferences cannot choose an order or change the current task route.
        from app.tools.memory_tool import memory_prompt_sections
        context = [*memory_prompt_sections(effective_message, context=context, session_id=session_id), *context]
        historical_evidence = [] if request.task_id else case_manager.recent_tool_evidence(session_id)
        engine = AgentEngine(
            intent,
            # 回复（及 ReAct 规划）统一用 DeepSeek 主模型 deepseek-v4-flash；
            # API Key 由 _get_openai_client 从环境变量 DEEPSEEK_API_KEY 读取。
            client=LlmClient(raw_client=_get_openai_client(), model=settings.deepseek_model_name),
            model=settings.deepseek_model_name,
        )
        if tracer is not None:
            tracer.record(
                "plan",
                execution=engine.plan.execution.value,
                tools=[getattr(t, "name", str(t)) for t in engine.plan.tools],
                retrievable=engine.plan.retrievable,
            )
        observer.enter("PLAN", execution=engine.plan.execution.value)
        observer.leave("PLAN", to_state="EXECUTE", execution=engine.plan.execution.value)
        observer.enter("EXECUTE", execution=engine.plan.execution.value)
        result = engine.execute(
            rewritten.message,
            messages=context,
            confirmed=False,
            mutation_guard=guard,
            entities=rewritten.entities,
            historical_evidence=historical_evidence,
        )
        observer.leave("EXECUTE", to_state="RESPONSE", execution=result.execution.value)
    if result.selection_candidates and not request.task_id:
        case_manager.request_order_selection(
            session_id, request.message, result.intent.value, result.selection_candidates,
        )
        if tracer is not None:
            tracer.record(
                "selection_requested",
                intent=result.intent.value,
                candidate_order_ids=[item.get("order_id") for item in result.selection_candidates],
            )
    for step in result.tool_steps:
        case_manager.record_tool_result(
            session_id, turn_id, step.name,
            json.dumps(step.result, ensure_ascii=False),
        )
    case_manager.record_response(session_id, turn_id, result.reply)
    # 持久化会话历史：前端 GET /api/v1/sessions/{id} 与后端 _build_engine
    # 加载 history 都依赖此数据；缺失则多轮上下文彻底断（参见诊断）。
    _persist_session_turn(session_id, user_id, request.message, result.reply)
    if result.needs_confirmation:
        follow_up = "请确认是否继续该操作？"
    elif result.needs_human:
        follow_up = (
            "已创建人工工单，请稍候。" if result.human_handoff_created
            else "当前需要人工介入，但系统尚未创建人工工单，请联系客服继续处理。"
        )
    else:
        follow_up = None
    if tracer is not None:
        tracer.set_tool_steps(result.tool_steps)
        tracer.set_response(
            reply=result.reply,
            requires_human=result.needs_human,
            follow_up_question=follow_up,
            intent=result.intent.value,
            execution=result.execution.value,
            confidence=0.95 if result.execution.value != "react" else 0.80,
        )
    return ChatResponse(
        session_id=session_id,
        reply=result.reply,
        intent=result.intent.value,
        confidence=0.95 if result.execution.value != "react" else 0.80,
        requires_human=result.needs_human,
        follow_up_question=follow_up,
        confirmation_token=result.confirmation_token,
        question=result.question,
        task_outcome='needs_human' if result.needs_human else ('paused' if result.blocked else None),
    )


@app.post("/api/v1/sessions/{session_id}/reset", response_model=SessionResponse)
async def reset_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    try:
        return await asyncio.to_thread(_handle_reset_session, session_id, current_user.user_id)
    except (SessionBusy, CoordinationUnavailable) as exc:
        raise HTTPException(status_code=409 if isinstance(exc, SessionBusy) else 503, detail=str(exc)) from exc


def _handle_reset_session(session_id: str, user_id: str) -> SessionResponse:
    try:
        get_store().state(_normalise_session_id(session_id), user_id)
    except SupportError as exc:
        if exc.status != 404:
            raise
    else:
        raise SupportError("会话记录用于工单追溯，请使用新建咨询；返回机器人请使用对应按钮")
    from app.session.repository import delete_session

    normalized_session_id, _ = _build_engine(user_id, session_id)
    control_token = None
    session_guard = nullcontext()
    if settings.api_chat_execution_mode == "async":
        if _chat_queue is None:
            raise CoordinationUnavailable("异步任务队列未配置")
        control_token = _chat_queue.reserve_control(normalized_session_id)
    else:
        session_guard = SessionLock(_redis_url, normalized_session_id, timeout=settings.api_queue_wait_seconds)
    try:
        with session_guard:
            delete_session(
                normalized_session_id,
                settings.api_session_storage_backend,
                settings.api_session_postgres_dsn,
                user_id,
            )
    finally:
        if control_token is not None:
            _chat_queue.release_control(normalized_session_id, control_token)
    return SessionResponse(session_id=normalized_session_id, status="reset")


@app.post("/api/v1/sessions/{session_id}/close", response_model=SessionResponse)
async def close_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    try:
        return await asyncio.to_thread(_handle_close_session, session_id, current_user.user_id)
    except (SessionBusy, CoordinationUnavailable) as exc:
        raise HTTPException(status_code=409 if isinstance(exc, SessionBusy) else 503, detail=str(exc)) from exc


def _handle_close_session(session_id: str, user_id: str) -> SessionResponse:
    try:
        state = get_store().state(_normalise_session_id(session_id), user_id)
    except SupportError as exc:
        if exc.status != 404:
            raise
    else:
        if state["mode"] != "bot":
            raise SupportError("人工处理中，请先明确结束人工服务")
    from app.session.repository import update_session_metadata

    normalized_session_id, _ = _build_engine(user_id, session_id)
    control_token = None
    session_guard = nullcontext()
    if settings.api_chat_execution_mode == "async":
        if _chat_queue is None:
            raise CoordinationUnavailable("异步任务队列未配置")
        control_token = _chat_queue.reserve_control(normalized_session_id)
    else:
        session_guard = SessionLock(_redis_url, normalized_session_id, timeout=settings.api_queue_wait_seconds)
    try:
        with session_guard:
            update_session_metadata(
                normalized_session_id,
                settings.api_session_storage_backend,
                settings.api_session_postgres_dsn,
                user_id,
                status="closed",
            )
    finally:
        if control_token is not None:
            _chat_queue.release_control(normalized_session_id, control_token)
    return SessionResponse(session_id=normalized_session_id, status="closed")
