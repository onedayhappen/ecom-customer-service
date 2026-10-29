"""Plain-text query rewriting; original messages remain authoritative."""
import json

from app.config.settings import settings
from app.observability.interactions import call_model
from app.prompts.query_rewrite import QUERY_REWRITE_PROMPT, IMAGE_REWRITE_NOTE


def validate_rewrite(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise ValueError("invalid_query_rewrite")
    value = value.strip()
    if value.startswith("```"):
        raise ValueError("structured_query_rewrite")
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if isinstance(parsed, (dict, list)):
        raise ValueError("structured_query_rewrite")
    return value


def rewrite(client, model, context):
    if hasattr(client, "bind_scene"):
        client = client.bind_scene("query_rewrite")
    template = {
        **settings.chat_completion_options,
        "model": model, "temperature": 0, "max_tokens": 2000, "timeout": 15,
        "messages": [{"role": "system", "content": QUERY_REWRITE_PROMPT + '\n' + IMAGE_REWRITE_NOTE}],
    }
    template.pop("response_format", None)
    response = call_model(client, context.request(template))
    return validate_rewrite(response.choices[0].message.content)
