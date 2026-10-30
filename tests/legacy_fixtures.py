# 文件说明：构造历史数据库结构与记录，供兼容迁移测试使用。
"""Historical database fixtures; never exposed as an application execution path."""
from uuid import uuid4
from app.support.store import now


def create_legacy_task(store, tx, control, spec, actor):
    timestamp = now()
    task = dict(task_id=str(uuid4()), session_id=control['session_id'], intent=spec['intent'],
                title=spec['intent'], request_text=spec['text'], slots={}, status='queued',
                version=0, question=None, created_at=timestamp, updated_at=timestamp,
                created_sequence=control['sequence'] + 1)
    tx.execute("""INSERT INTO conversation_tasks
        (task_id,session_id,intent,title,request_text,slots,status,version,question,
         created_at,updated_at,created_sequence,user_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task['task_id'], control['session_id'], task['intent'], task['title'], task['request_text'],
         '{}', 'queued', 0, None, timestamp, timestamp, task['created_sequence'], control['user_id']))
    tx.execute('INSERT INTO task_sessions VALUES (?,?,?)', (task['task_id'],control['session_id'],timestamp))
    store.tasks.save(tx, control, task, actor)
    return task
