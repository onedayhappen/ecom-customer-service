# 文件说明：绑定补问、人工交接、记忆和图片等事项流程工具入口。
"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

read_task_context = bind('read_task_context')
inspect_image = bind('inspect_image')
recall_user_memory = bind('recall_user_memory')
ask_user = bind('ask_user')
handoff_to_human = bind('handoff_to_human')
