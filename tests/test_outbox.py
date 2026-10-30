# 文件说明：测试任务归档后关联进度、已接收输入和执行去重依据的保留。
"""Archival preserves current association jobs and their execution tombstones."""
import json
import pytest
from app.support.store import SupportStore, SupportError
from app.tasks.intake import MessageIntake
from app.runtime.maintenance import archive_jobs


def test_archive_preserves_completed_job_and_accepted_input(tmp_path):
    store = SupportStore(f"sqlite:///{tmp_path / 'archive.db'}")
    store.ensure('session', 'user')
    intake = MessageIntake(store)
    intake.receive('session', 'user', request_id='message', content='hello')
    batch = intake.claim_next()
    with store.transaction() as tx:
        store.jobs.completed(tx, batch.job_id, {'done': True})
        tx.execute('UPDATE runtime_jobs SET finished_at=0 WHERE job_id=?', (batch.job_id,))
    assert archive_jobs(store, retention_days=1, limit=1) == 1
    assert archive_jobs(store, retention_days=1) == 0
    with store.transaction(read_only=True) as tx:
        job = tx.one('SELECT * FROM runtime_jobs WHERE job_id=?', (batch.job_id,))
        archived = tx.one('SELECT * FROM runtime_job_archives WHERE job_id=?', (batch.job_id,))
    assert job['status'] == 'completed' and job['payload'] == '{}'
    assert json.loads(archived['response']) == {'done': True}
    assert intake.progress('session', 'user')['messages']
    with pytest.raises(SupportError):
        intake.progress('session', 'intruder')


def test_single_message_jobs_cannot_be_admitted(tmp_path):
    store = SupportStore(f"sqlite:///{tmp_path / 'admit.db'}")
    store.ensure('session', 'user')
    with store.transaction() as tx:
        control = store._control(tx, 'session', 'user')
        with pytest.raises(SupportError):
            store.jobs.admit(tx, control, 'old', kind='conversation', max_pending=10,
                             queue_timeout=10, run_timeout=10)
