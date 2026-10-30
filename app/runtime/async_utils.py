# 文件说明：在异步取消时安全收尾线程操作，避免提前释放在途资源。
"""Cancellation-safe finalization of bounded threaded resource operations."""
import asyncio


async def settle(task):
    """Finish cleanup even when a surrounding ASGI scope cancels repeatedly."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()
