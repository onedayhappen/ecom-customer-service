"""API admission, authenticated history and health; execution belongs to Workers."""
from contextlib import asynccontextmanager
import asyncio
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from app.api.auth import router as auth_router, get_auth_store, use_mysql_auth
from app.api.middleware import AuthMiddleware
from app.api.support import router as support_router, identity
from app.config.settings import settings
from app.support.runtime import get_store
from app.support.store import SupportError
from app.runtime.budgets import RateExceeded, admit_request
from app.runtime.concurrency import CoordinationUnavailable

@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化会话库、审计与连接池；模型和工具执行由独立 Worker 承担。"""
    settings.validate_runtime_configuration()
    if use_mysql_auth():
        await asyncio.to_thread(get_auth_store().ensure_schema)
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

# 注册与登录认证路由。
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
    """Prometheus 指标出口：LLM/工具/RT 指标由埋点体系发射，抓取端读取。

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
def current_identity(request: Request):
    return {"user_id": identity(request)}


@app.get("/api/v1/sessions/{session_id}")
async def get_session_history(session_id: UUID, request: Request):
    user, sid = identity(request), str(session_id)
    await asyncio.to_thread(admit_request, user, 'read')
    messages = await asyncio.to_thread(get_store().public_history, sid, user)
    return {"session_id": sid, "status": "active", "messages": messages}


# Tombstones only: old clients cannot accidentally invoke a second pipeline.
@app.post("/api/v1/chat", include_in_schema=False)
@app.get("/api/v1/chat/{job_id}", include_in_schema=False)
@app.get("/api/v1/chat/{job_id}/events", include_in_schema=False)
async def retired_chat():
    raise HTTPException(410, '旧单消息入口已移除，请使用 /api/v1/task-messages 和会话事件接口')


@app.post("/api/v1/sessions/{session_id}/reset", include_in_schema=False)
@app.post("/api/v1/sessions/{session_id}/close", include_in_schema=False)
async def retired_session_control(session_id: UUID, request: Request):
    await asyncio.to_thread(get_store().state, str(session_id), identity(request))
    raise SupportError('会话记录用于追溯，请新建咨询；人工接管请使用对应入口')
