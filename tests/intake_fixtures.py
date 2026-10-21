"""Use the same durable admission API as the customer frontend."""
from app.tasks.intake import MessageIntake


def receive_message(store, sid, user, content, request_id):
    return MessageIntake(store).receive(sid, user, content=content, request_id=request_id)
