"""Trusted request context for the existing single-Agent knowledge tool."""
from contextlib import contextmanager
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone

from app.rag.policy import PolicyContext, PolicyError

_context = ContextVar('policy_context', default=None)


@contextmanager
def policy_scope(context):
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def current_policy_context():
    return _context.get()


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def source_request_time(runs, source, *, tx=None):
    """Use message receipt time, not the time a queued worker starts."""
    with (nullcontext(tx) if tx is not None else runs.store.transaction(read_only=True)) as current:
        row = current.one("""SELECT e.created_at FROM support_events e JOIN support_conversations c
            ON c.session_id=e.session_id WHERE c.user_id=? AND e.session_id=? AND e.sequence=?
            AND e.kind='message' AND e.actor=?""",
            (runs.user, source['session_id'], source['sequence'], runs.user))
    moment = _time(row['created_at']) if row else None
    if moment is None:
        raise PolicyError('policy_request_time_unverified')
    return moment


def context_for_task(task, *, request_at=None):
    details = task.record.details
    scope = vars(task.runs.candidates.scope)
    if getattr(task, 'request_time', None) is not None:
        request_at = task.request_time
    elif getattr(task, 'request', None) is not None:
        request_at = source_request_time(task.runs, task.request.source.model_dump())
    values = dict(request_at=request_at or datetime.fromtimestamp(task.evidence_time, timezone.utc))
    order_id = details.get('order_id')
    if not order_id:
        # General public consultation may specify a shop/category. It still
        # cannot request staff access or choose the rule's time/version basis.
        values.update({key: scope.get(key) or details.get(key) for key in ('platform_code', 'store_id', 'channel', 'category')})
        return PolicyContext.model_validate(values)
    evidence = next((e for e in reversed(task.historical + task.receipts)
        if e['tool'] == 'query_order' and e['result'].get('success')
        and e['observed_epoch'] >= task.evidence_time - 60
        and e['result'].get('order', {}).get('order_id') == order_id), None)
    if evidence is None:
        raise PolicyError('policy_order_evidence_required', ['query_order'])
    order = evidence['result']['order']
    values.update(order_id=order_id, order_evidence_id=evidence['event_id'],
                  order_created_at=_time(order.get('created_at')), delivered_at=_time(order.get('delivered_at')))
    for key in ('platform_code', 'store_id', 'channel', 'category'):
        actual = order.get(key, evidence['result'].get(key))
        if scope.get(key) is not None and actual != scope[key]:
            raise PolicyError('policy_order_scope_unverified', [key])
        if key in ('platform_code', 'store_id') and details.get(key) and actual != details[key]:
            raise PolicyError('policy_order_scope_unverified', [key])
        values[key] = actual
    if not values.get('category'):
        items = order.get('items')
        if isinstance(items, list):
            selected = [item for item in items if isinstance(item, dict) and all(
                not details.get(key) or item.get(key) == details[key] for key in ('item_id', 'sku_id', 'product_id'))]
            categories = {item.get('category') for item in selected}
            if len(categories) == 1 and None not in categories:
                values['category'] = categories.pop()
    return PolicyContext.model_validate(values)


def context_current(task, result):
    """A valid clause cannot outlive the business evidence selecting it."""
    try:
        expected = PolicyContext.model_validate(result['context'])
        return context_for_task(task, request_at=expected.request_at) == expected
    except (PolicyError, ValueError, TypeError, KeyError, AttributeError):
        return False
