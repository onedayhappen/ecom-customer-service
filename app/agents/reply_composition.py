"""One bounded, tool-free model call to express already verified results."""
from copy import deepcopy
import json

from app.config.settings import settings
from app.memory.source_policy import safe_context
from app.observability.interactions import call_model
from app.prompts.reply_composition import REPLY_COMPOSITION_PROMPT
from app.session.context_budget import estimate_request_tokens
from app.session.request_budget import ContextBudgetExceeded, request_input_limit


def validate_reply(value, *, context=None):
    if not isinstance(value, str) or not value.strip() or len(value) > 6000:
        raise ValueError("invalid_composed_reply")
    value = value.strip()
    if context is not None:
        from app.tasks.reply_grounding import choices, validate
        if choices(context):
            return validate(value, context)
    if value.startswith(("```", "{", "[")):
        raise ValueError("structured_composed_reply")
    return value


def compose(client, model, context):
    from app.tasks.reply_grounding import choices
    if hasattr(client, "bind_scene"):
        client = client.bind_scene("reply_composition")
    data = deepcopy(context)
    blocks = choices(context)
    prompt = REPLY_COMPOSITION_PROMPT
    if blocks:
        # Replace the facts, rather than duplicate them in the model request.
        for matter in data["matters"]:
            matter.pop("grounding", None)
            matter.pop("fallback", None)
            matter["verified_facts"] = ""
        data["evidence_blocks"] = blocks
        from app.prompts.reply_composition import GROUNDED_REPLY_PROMPT
        prompt = GROUNDED_REPLY_PROMPT
    if data.get("current_messages_incomplete"):
        raise ContextBudgetExceeded(reason="reply_current_messages")
    params = {**settings.chat_completion_options, "model": model, "temperature": 0,
              "max_tokens": 1500, "timeout": settings.reply_composition_timeout_seconds}
    for key in ("response_format", "tools", "tool_choice"):
        params.pop(key, None)
    while True:
        params["messages"] = [
            {"role": "system", "content": prompt},
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
    return validate_reply(choice.message.content, context=context)
