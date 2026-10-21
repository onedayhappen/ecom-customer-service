"""Server-only confirmation binding for newly implemented write tools."""
from contextlib import contextmanager
from contextvars import ContextVar
import json

_authorized = ContextVar('authorized_commerce_mutation', default=None)
_preflight = ContextVar('authorized_mutation_preflight', default=None)


def canonical(args):
    return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


@contextmanager
def authorized_mutation(user_id, action, arguments, request_id):
    token = _authorized.set((user_id, action, canonical(arguments), request_id))
    try:
        yield
    finally:
        _authorized.reset(token)


def request_id_for(user_id, action, arguments):
    binding = _authorized.get()
    if binding and binding[:3] == (user_id, action, canonical(arguments)):
        return binding[3]
    return None


@contextmanager
def checked_preflight(user_id, action, arguments, version):
    """Server-only business snapshot expected by a newly confirmed operation."""
    token = _preflight.set((user_id, action, canonical(arguments), version))
    try:
        yield
    finally:
        _preflight.reset(token)


def preflight_for(user_id, action, arguments):
    binding = _preflight.get()
    if binding and binding[:3] == (user_id, action, canonical(arguments)):
        return binding[3]
    return None
