"""One bounded, tool-free call to select code-owned evidence conclusions."""
from copy import deepcopy
import json

from app.config.settings import settings
from app.memory.source_policy import safe_context
from app.observability.interactions import call_model
from app.prompts.reply_composition import REPLY_COMPOSITION_PROMPT
from app.session.context_budget import estimate_request_tokens
from app.session.request_budget import ContextBudgetExceeded, request_input_limit


def selection_context(context):
    """Send selection hints, not a second copy of every renderable sentence.

    Full subject refs, receipt IDs, text and quality remain in the publisher's
    fingerprinted snapshot. Receipt aliases here only reduce input tokens.
    """
    data = deepcopy(context)
    for matter in data["matters"]:
        sources, claims = {}, []
        for claim in matter.get("claims", []):
            eid = claim["evidence_id"]
            if eid not in sources:
                q = claim["quality"]
                sources[eid] = dict(source=len(sources), completeness=q["completeness"],
                                    data_as_of=q["data_as_of"], observed_at=q["observed_at"])
            claims.append(dict(claim_id=claim["claim_id"], source=sources[eid]["source"],
                               kind=claim["kind"], summary=claim["selection_hint"]))
        matter["claims"] = claims
        matter["sources"] = list(sources.values())
        matter.pop("evidence_ids", None)
    return data


def compose(client, model, context):
    if hasattr(client, "bind_scene"):
        client = client.bind_scene("reply_composition")
    data = selection_context(context)
    if data.get("current_messages_incomplete"):
        raise ContextBudgetExceeded(reason="reply_current_messages")
    params = {**settings.chat_completion_options, "model": model, "temperature": 0,
              "max_tokens": 1500, "timeout": settings.reply_composition_timeout_seconds}
    for key in ("response_format", "tools", "tool_choice"):
        params.pop(key, None)
    params["response_format"] = {"type": "json_object"}
    while True:
        params["messages"] = [
            {"role": "system", "content": REPLY_COMPOSITION_PROMPT},
            {"role": "user", "content": json.dumps(safe_context(data), ensure_ascii=False)},
        ]
        estimated, limit = estimate_request_tokens(params), request_input_limit(params)
        if estimated <= limit:
            break
        if not data["recent_messages"]:
            raise ContextBudgetExceeded(estimated, limit, reason="reply_composition")
        data["recent_messages"].pop(0)
        data["history_omitted"] = True
    response = call_model(client, params)
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) not in (None, "stop") or getattr(choice.message, "tool_calls", None):
        raise ValueError("incomplete_composed_reply")
    # Validate against the publisher's immutable snapshot, also for custom
    # composers that do not use this model adapter.
    return choice.message.content
