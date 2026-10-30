# 文件说明：通过持久消息入口构造测试输入，与客户前端保持一致的接收路径。
"""Use the same durable admission API as the customer frontend."""
from app.tasks.intake import MessageIntake


def receive_message(store, sid, user, content, request_id):
    return MessageIntake(store).receive(sid, user, content=content, request_id=request_id)
