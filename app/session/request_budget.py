"""Bound provider requests without changing the persisted conversation/evidence.

Fixed instructions, the latest user request and current task state are pinned.
Only complete old turns and explicitly named derived memory can be removed.
Tool evidence can be projected, but its call/result protocol is never split.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from app.session.context_budget import estimate_request_tokens, estimate_text_tokens


class ContextBudgetExceeded(RuntimeError):
    """Required input cannot fit, or its tool protocol is already invalid."""

    def __init__(self, estimated_tokens: int = 0, limit_tokens: int = 0, *, reason: str = "required_context"):
        self.estimated_tokens = estimated_tokens
        self.limit_tokens = limit_tokens
        self.reason = reason
        super().__init__("当前请求所需上下文超过容量，请缩短本次输入或联系人工客服处理。"
                         if reason != "invalid_tool_protocol" else
                         "当前工具上下文不完整，请重试或联系人工客服处理。")


def request_input_limit(params: dict) -> int:
    """Keep the output reservation and configured margin inside a known window."""
    from app.config.settings import settings

    limit = settings.context_request_input_tokens
    window = settings.context_model_window_tokens
    output = int(params.get("max_completion_tokens") or params.get("max_tokens") or settings.model_max_output_tokens)
    if window:
        limit = min(limit, window - output - settings.context_window_margin_tokens)
    return max(0, limit)


def _check_tool_protocol(messages: list[dict]) -> None:
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ContextBudgetExceeded(reason="invalid_tool_protocol")
            pending.remove(call_id)
            continue
        if pending:
            raise ContextBudgetExceeded(reason="invalid_tool_protocol")
        calls = message.get("tool_calls")
        if calls:
            if role != "assistant" or not isinstance(calls, list):
                raise ContextBudgetExceeded(reason="invalid_tool_protocol")
            ids = [call.get("id") if isinstance(call, dict) else None for call in calls]
            if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(set(ids)) != len(ids):
                raise ContextBudgetExceeded(reason="invalid_tool_protocol")
            pending.update(ids)
    if pending:
        raise ContextBudgetExceeded(reason="invalid_tool_protocol")


_CRITICAL_FIELDS = frozenset({
    "success", "ok", "status", "state", "error", "error_code", "code", "reason",
    "id", "sku", "number", "name", "blocked", "replayed", "confirmation_token",
    "needs_confirmation", "requires_confirmation", "needs_human", "human_handoff_created",
    "amount", "total", "price", "refund_amount", "quantity", "count", "row_count",
    "currency", "columns", "arguments", "validation", "verified", "source",
})


def _critical(key: str) -> bool:
    return key in _CRITICAL_FIELDS or key.endswith(("_id", "_ids", "_no", "_number", "_status"))


def _project(value: Any, *, items: int, chars: int, key: str = "", keep_list: bool = False) -> Any:
    if key in {"arguments", "validation"}:
        return value
    if isinstance(value, dict):
        # Preserve field names, especially status and business identifiers even
        # when nested in a provider-specific result envelope.
        return {name: _project(child, items=items, chars=chars, key=str(name)) for name, child in value.items()}
    if isinstance(value, list):
        selected = value if keep_list or key in {"columns", "arguments", "__row__"} else value[:items]
        result = [_project(child, items=items, chars=chars, key="__row__" if key == "rows" else "") for child in selected]
        # The marker stays outside SQL row arrays, preserving column alignment.
        if len(selected) < len(value):
            result.append({"_context_omitted_items": len(value) - len(selected)})
        return result
    if isinstance(value, str):
        if _critical(key):
            return value
        # Some tools wrap JSON results as strings; don't cut away nested status.
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return _project(parsed, items=items, chars=chars)
        if len(value) > chars:
            return value[:chars] + "… [context omitted]"
    return value


def _project_content(content: Any, *, items: int, chars: int, workflow: bool) -> str:
    if isinstance(content, str):
        try:
            data = json.loads(content)
        except (ValueError, RecursionError):
            data = content
    else:
        data = content
    projected = _project(data, items=items, chars=chars, keep_list=workflow)
    return json.dumps({
        "_context_projection": {
            "incomplete": True,
            "note": "部分工具内容已省略；仅依据保留字段作答，不能把省略视为没有结果。",
        },
        "result": projected,
    }, ensure_ascii=False, separators=(",", ":"))


def apply_request_budget(params: dict, scene: str = "", model: str = "") -> dict:
    """Return an independent, bounded request; never mutate caller messages.

    ``scene``/``model`` are accepted for callers' per-scene diagnostics. The
    client records before/after estimates together with actual provider usage.
    Disable with CONTEXT_REQUEST_BUDGET_ENABLED for a deliberate rollback.
    """
    from app.config.settings import settings

    request = deepcopy(params)
    if not settings.context_request_budget_enabled:
        return request
    messages = request.get("messages", [])
    _check_tool_protocol(messages)
    limit = request_input_limit(request)

    def fits() -> bool:
        return estimate_request_tokens(request) <= limit

    if fits():
        return request

    for name in ("short_term_recall", "long_term_memory", "short_term_summary"):
        messages[:] = [message for message in messages if not (
            message.get("role") == "system" and message.get("name") == name)]
        if fits():
            return request

    # Last user and everything after it belong to the current turn. Every fixed
    # system/developer instruction stays pinned even if interleaved in history.
    user_indices = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    remove: set[int] = set()
    for start, end in zip(user_indices, user_indices[1:]):
        # Consecutive historical user messages are also removable. Human
        # support can receive several messages before replying; requiring an
        # assistant in every group would keep that history pinned forever.
        remove.update(i for i in range(start, end) if messages[i].get("role") not in {"system", "developer"})
        request["messages"] = [message for i, message in enumerate(messages) if i not in remove]
        if fits():
            _check_tool_protocol(request["messages"])
            return request
    messages = request.get("messages", [])

    # Largest evidence first; each level derives from the original, avoiding
    # repeated summary drift. Tool call ids and arguments are never changed.
    evidence = [(i, message.get("content")) for i, message in enumerate(messages) if
                message.get("role") == "tool" or
                (message.get("role") == "system" and message.get("name") == "workflow_evidence")]
    evidence.sort(key=lambda item: estimate_text_tokens(item[1]), reverse=True)
    for items, chars in ((8, 2048), (4, 512), (1, 128)):
        for index, original in evidence:
            message = messages[index]
            projected = _project_content(original, items=items, chars=chars,
                                         workflow=message.get("name") == "workflow_evidence")
            if estimate_text_tokens(projected) < estimate_text_tokens(message.get("content")):
                message["content"] = projected
            if fits():
                _check_tool_protocol(messages)
                return request
    raise ContextBudgetExceeded(estimate_request_tokens(request), limit)
