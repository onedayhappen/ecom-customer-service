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
