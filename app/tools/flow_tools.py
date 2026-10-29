"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

read_task_context = bind('read_task_context')
inspect_image = bind('inspect_image')
ask_user = bind('ask_user')
handoff_to_human = bind('handoff_to_human')
