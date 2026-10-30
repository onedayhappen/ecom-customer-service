# 文件说明：限制阻塞模型及只读调用的数量和等待时间，保留在途资源占用。
"""Bound blocking read/model calls without releasing occupied resources early."""
from concurrent.futures import Future, TimeoutError
from contextvars import copy_context
import threading

from app.runtime.concurrency import execution_lease_guard


class CallCapacityExceeded(RuntimeError):
    pass


class BoundedCalls:
    """Timed-out calls keep their slot until they really finish.

    Only use for reads or model inference, never a business write or progress
    commit. Python cannot kill a blocked function; late return values are ignored.
    """
    def __init__(self, capacity=16):
        self.slots = threading.BoundedSemaphore(capacity)

    def run(self, function, timeout):
        if timeout <= 0:
            raise TimeoutError("Call deadline exceeded")
        if not self.slots.acquire(blocking=False):
            raise CallCapacityExceeded("Blocking call capacity exhausted")
        result, stopped, context = Future(), threading.Event(), copy_context()
        def invoke():
            try:
                if not result.set_running_or_notify_cancel():
                    return
                with execution_lease_guard(stopped):
                    value = function()
                if not result.cancelled():
                    result.set_result(value)
            except BaseException as exc:
                if not result.cancelled():
                    result.set_exception(exc)
            finally:
                self.slots.release()
        thread = threading.Thread(target=lambda: context.run(invoke), daemon=True, name="agent-read-call")
        try:
            thread.start()
        except BaseException:
            self.slots.release()
            raise
        try:
            return result.result(timeout=timeout)
        except TimeoutError:
            stopped.set()
            result.cancel()
            raise


bounded_calls = BoundedCalls()
