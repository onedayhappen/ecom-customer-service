"""Request-scoped bot ownership, also propagated through asyncio.to_thread."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
import re

from app.support.store import SupportStore

_scope = ContextVar("support_bot_scope", default=None)


@lru_cache(maxsize=4)
def _store(dsn):
    return SupportStore(dsn)


def get_store():
    from app.config.settings import settings
    return _store(settings.support_database_url or settings.api_session_postgres_dsn)


def explicit_handoff(text):
    # Conservative control command: ambiguous phrases stay in normal dialogue.
    return bool(re.fullmatch(r"\s*(?:请|请帮我|帮我|我要|我想|我想要|给我)?\s*(?:转|转接|联系|找)?\s*人工(?:客服|服务)?\s*[！!。.?？]*\s*", text))


@contextmanager
def bot_scope(store, sid, user, version):
    token = _scope.set((store, sid, user, version))
    try:
        check_bot()
        yield
    finally:
        _scope.reset(token)


def check_bot():
    scope = _scope.get()
    if scope:
        store, sid, user, version = scope
        store.check_bot(sid, user, version)


def control_binding():
    scope = _scope.get()
    return {"support_session": scope[1], "support_version": scope[3]} if scope else {}


def current_scope():
    return _scope.get()


def valid_binding(pending):
    binding = control_binding()
    return not binding or all(pending.get(k) == v for k, v in binding.items())


def call_tool(name, arguments, function):
    from app.data_plan.capabilities import MUTATING_CAPABILITIES
    scope = _scope.get()
    if not scope:
        return function(**arguments)
    store, sid, user, version = scope
    check_bot()
    if name not in MUTATING_CAPABILITIES:
        result = function(**arguments)
        if name != "escalate_to_human":
            store.evidence(sid, user, name, arguments, result)
        return result
    oid = store.begin_operation(sid, user, version, name)
    try:
        result = function(**arguments)
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        store.finish_operation(oid, {"success": False, "error_code": "transient_failure"})
        raise
    store.finish_operation(oid, parsed if isinstance(parsed, dict) else {})
    store.evidence(sid, user, name, arguments, result)
    return result


def escalate_to_human():
    scope = _scope.get()
    if not scope:
        return {"success": False, "error": "转人工需要已验证的会话上下文"}
    store, sid, user, version = scope
    store.check_bot(sid, user, version)
    state = store.handoff(sid, user, "agent_escalation")
    return {"success": True, "ticket_id": state["ticket"]["ticket_id"], "message": "已进入人工队列"}
