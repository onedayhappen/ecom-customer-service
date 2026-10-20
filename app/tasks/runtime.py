"""Request-local binding to the single task currently being executed."""
from contextlib import contextmanager
from contextvars import ContextVar

_task = ContextVar("current_task_execution", default=None)


def current_task():
    return _task.get()


@contextmanager
def task_scope(execution):
    token = _task.set(execution)
    try:
        yield execution
    finally:
        _task.reset(token)


def check_task():
    value = current_task()
    if value is not None:
        value.check()
