# 文件说明：限制 SSE 发送时长和用户连接数，管理订阅租约及连接收尾。
"""SSE send deadlines and per-user connection leases without per-client threads."""
import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256

from starlette.responses import StreamingResponse
from app.runtime.budgets import lease, RateExceeded
from app.runtime.concurrency import CoordinationUnavailable
from app.runtime.async_utils import settle


async def acquire_connection(resource):
    pending = asyncio.create_task(asyncio.to_thread(resource.acquire))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop its thread. Wait for the actual
        # acquisition and return its slot before letting the request disappear.
        try:
            token = await settle(pending)
        except Exception:
            pass
        else:
            await settle(asyncio.create_task(asyncio.to_thread(resource.release, token)))
        raise


class BoundedStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        cleanup, self.background = self.background, None
        try:
            await super().__call__(scope, receive, send)
        finally:
            if cleanup:
                await settle(asyncio.create_task(cleanup()))

    async def stream_response(self, send):
        from app.config.settings import settings
        async def bounded_send(message):
            await asyncio.wait_for(send(message), settings.api_sse_send_timeout_seconds)
        try:
            await super().stream_response(bounded_send)
        except TimeoutError:
            # Close slow consumers. Durable cursors let them recover on reconnect.
            pass
        finally:
            close = getattr(self.body_iterator, 'aclose', None)
            if close:
                await settle(asyncio.ensure_future(close()))


@asynccontextmanager
async def connection_budget(user):
    from app.config.settings import settings
    key = settings.api_chat_queue_prefix + ':sse-user:' + sha256(user.encode()).hexdigest()
    resource = lease(settings.api_concurrency_redis_url, key, settings.api_sse_per_user, 60)
    try:
        token = await acquire_connection(resource)
    except CoordinationUnavailable:
        resource = lease('', key, settings.api_sse_per_user, 60)
        token = resource.acquire()
    if not token:
        raise RateExceeded(10)
    lost = asyncio.Event()

    async def renew():
        while True:
            await asyncio.sleep(15)
            if resource._redis:
                try:
                    ok = await asyncio.to_thread(resource._redis.eval, resource._RENEW, 1, resource.key, token, 60000)
                    if not ok:
                        lost.set()
                        return
                except Exception:
                    # Keep the existing connection on its bounded local hub;
                    # after lease loss the client reconnects using emergency caps.
                    lost.set()
                    return
    renewal = asyncio.create_task(renew())
    try:
        yield lost
    finally:
        renewal.cancel()
        async def cleanup():
            await asyncio.gather(renewal, return_exceptions=True)
            await asyncio.to_thread(resource.release, token)
        await settle(asyncio.create_task(cleanup()))


async def open_subscription(user, session_id):
    """Acquire before HTTP headers; return idempotent cleanup for both ASGI paths."""
    from app.runtime.notifications import get_hub
    budget = connection_budget(user)
    lost = await budget.__aenter__()
    subscription = get_hub().subscribe(session_id)
    try:
        wake = await subscription.__aenter__()
    except BaseException:
        await settle(asyncio.create_task(budget.__aexit__(None, None, None)))
        raise
    cleanup_task = None

    async def cleanup():
        nonlocal cleanup_task
        if cleanup_task is None:
            async def close():
                try:
                    await subscription.__aexit__(None, None, None)
                finally:
                    await budget.__aexit__(None, None, None)
            cleanup_task = asyncio.create_task(close())
        await settle(cleanup_task)
    return wake, lost, cleanup
