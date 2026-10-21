"""Cutover must fence old work without losing accepted input or business facts."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app.support.store import SupportStore, SupportError
from app.tasks.intake import MessageIntake
from app.tasks.repository import TaskRepository
from app.tasks.rollout import Rollout, running
from tests.test_task_candidates import store
from tests.test_task_storage import empty_store, message
from tests.test_task_operations import flags, ready, confirm


def switch(store, mode, user=None):
    return Rollout(store).set_mode(mode, user=user, actor='test-operator', reason='isolated rollback drill')


def test_retired_environment_options_cannot_restore_old_execution(tmp_path):
    from app.config.settings import Settings
    config = tmp_path / 'retired.env'
    config.write_text('ASSISTANT_JUST_REACT=false\nAPI_CHAT_EXECUTION_MODE=sync\n'
                      'API_CHAT_WORKER_ENABLED=true\nROUTER_API_KEY=old-unused-placeholder\n', encoding='utf-8')
    loaded = Settings(_env_file=config)
    assert loaded.task_intake_enabled and loaded.task_worker_enabled
    assert not any(name in loaded.model_dump() for name in (
        'assistant_just_react', 'api_chat_execution_mode', 'api_chat_worker_enabled', 'router_api_key'))


def test_user_route_is_shared_across_sessions_and_instances(store):
    switch(store, 'selected')
    switch(store, 'enabled', 'alice')
    intake = MessageIntake(SupportStore(store.dsn))
    for sid, user in [('s', 'alice'), ('old', 'alice'), ('other', 'bob')]:
        intake.receive(sid, user, request_id=sid, content='查物流')
        assert bool(intake.claim(sid, user)) == (user == 'alice')
    switch(store, 'disabled', 'alice')
    assert intake.claim('s', 'alice') is None
    assert intake.claim('old', 'alice') is None


def test_stop_keeps_input_and_fences_late_association_after_resume(store):
    intake = MessageIntake(store)
    receipt = intake.receive('s', 'alice', request_id='first', content='查订单')
    batch = intake.claim('s', 'alice')
    switch(store, 'stopped')
    assert intake.receive('s', 'alice', request_id='first', content='查订单') == receipt
    intake.receive('s', 'alice', request_id='second', content='再查物流')
    assert intake.claim('s', 'alice') is None
    switch(store, 'all')
    with pytest.raises(SupportError):
        intake.complete(batch, [dict(sequence=receipt['sequence'], status='associated', decision_id='late')])
    recovered = MessageIntake(SupportStore(store.dsn)).claim('s', 'alice')
    assert [m['content'] for m in recovered.messages] == ['查订单', '再查物流']
    assert recovered.job_id != batch.job_id
    assert len([m for m in store.public_history('s', 'alice') if m['role'] == 'user']) == 2


def test_stop_blocks_confirmations_and_resume_does_not_reuse_old_token(store):
    service, _, _, card = ready(store)
    switch(store, 'stopped')
    with pytest.raises(SupportError):
        confirm(service, card)
    assert not service.business.writes
    switch(store, 'all')
    with pytest.raises(SupportError):
        confirm(service, card)
    assert not service.business.writes


def test_stop_during_reserved_write_preserves_receipt_without_retry(store):
    service, record, _, card = ready(store)
    service.business.before_write = lambda: switch(store, 'stopped')
    assert confirm(service, card)['status'] == 'succeeded'
    assert confirm(service, card)['status'] == 'succeeded'
    switch(store, 'all')
    assert confirm(service, card)['status'] == 'succeeded'
    assert service.business.writes == [card['operation_id']]
    assert TaskRepository(store, 'alice').get(record.task_id).waiting_for['operation_id'] == card['operation_id']


def seed_legacy(store, *, rewritten=False):
    source = message(store, 's', 'alice', '查 ORD-A 的物流')
    with store.transaction() as tx:
        tx.execute('''INSERT INTO conversation_tasks
            (task_id,session_id,user_id,intent,title,request_text,slots,status,version,created_at,updated_at,created_sequence)
            VALUES ('legacy','s','alice','logistics_inquiry','物流',?,?,'waiting_input',3,'old','old',?)''',
            ('查 ORD-B 的物流' if rewritten else '查 ORD-A 的物流', json.dumps(dict(order_id='ORD-INFERRED')), source['sequence']))
        tx.execute('INSERT INTO support_task_messages VALUES (?,?,?)', ('legacy', 's', source['sequence']))
        tx.execute("INSERT INTO task_sessions VALUES ('legacy','s','old')")
    return source


def test_migration_uses_only_exact_original_and_preserves_legacy_source(store):
    source = seed_legacy(store)
    rollout = Rollout(store)
    with pytest.raises(SupportError):
        rollout.import_legacy('legacy', user='alice', expected_version=3, actor='op', reason='verified backup')
    switch(store, 'disabled', 'alice')
    record = rollout.import_legacy('legacy', user='alice', expected_version=3, actor='op', reason='verified backup')
    assert record.status == 'paused' and record.details == {} and record.goal == '查 ORD-A 的物流'
    assert rollout.import_legacy('legacy', user='alice', expected_version=3, actor='op', reason='retry') == record
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT storage_mode FROM conversation_tasks WHERE task_id='legacy'")['storage_mode'] == 'legacy'
        assert tx.one('SELECT * FROM support_task_messages WHERE task_id=?', (record.task_id,))['sequence'] == source['sequence']
        assert tx.one('SELECT COUNT(*) AS n FROM support_operations')['n'] == 0
    with pytest.raises(SupportError):
        rollout.import_legacy('legacy', user='bob', expected_version=3, actor='op', reason='bad owner')


@pytest.mark.parametrize('ambiguous', ['rewrite', 'multiple', 'invalidated', 'unknown_write'])
def test_uncertain_history_never_becomes_executable(store, ambiguous):
    if ambiguous == 'unknown_write':
        service, _, _, card = ready(store)
        service.business.uncertain = True
        assert confirm(service, card)['status'] == 'unknown'
    source = seed_legacy(store, rewritten=ambiguous == 'rewrite')
    if ambiguous == 'multiple':
        second = message(store, 's', 'alice', '不是这笔订单')
        with store.transaction() as tx:
            tx.execute('INSERT INTO support_task_messages VALUES (?,?,?)', ('legacy', 's', second['sequence']))
    if ambiguous == 'invalidated':
        from app.tasks.provenance import event_ref
        with store.transaction() as tx:
            tx.execute('INSERT INTO task_invalid_sources VALUES (?,?,?,?)',
                       ('alice', 'event', event_ref('s', source['sequence']), 'correction'))
    switch(store, 'stopped')
    with pytest.raises(SupportError):
        Rollout(store).import_legacy('legacy', user='alice', expected_version=3, actor='op', reason='backup checked')
    with store.transaction(read_only=True) as tx:
        assert not tx.one('SELECT * FROM task_legacy_imports')


def test_failed_stop_rolls_back_every_control_and_job(store, monkeypatch):
    intake = MessageIntake(store)
    intake.receive('s', 'alice', request_id='before', content='查物流')
    batch = intake.claim('s', 'alice')
    original = store._event
    def fail(*args, **kwargs):
        if args[2] == 'pipeline_control':
            raise RuntimeError('failure during cutover')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, '_event', fail)
    with pytest.raises(RuntimeError):
        switch(store, 'stopped')
    with store.transaction(read_only=True) as tx:
        assert running(tx, 'alice')
        assert tx.one('SELECT status FROM runtime_jobs WHERE job_id=?', (batch.job_id,))['status'] == 'running'
        assert not tx.one('SELECT * FROM task_rollout_audit')


def test_stop_serializes_with_database_reservations(store):
    held, release, stopping = threading.Event(), threading.Event(), threading.Event()
    def reserve():
        with store.transaction() as tx:
            assert running(tx, 'alice')
            held.set()
            assert release.wait(5)
    def stop():
        stopping.set()
        return switch(SupportStore(store.dsn), 'stopped')
    with ThreadPoolExecutor(2) as pool:
        reservation = pool.submit(reserve)
        assert held.wait(5)
        transition = pool.submit(stop)
        assert stopping.wait(5)
        assert not transition.done()
        release.set()
        reservation.result(5)
        transition.result(5)
    with store.transaction(read_only=True) as tx:
        assert not running(tx, 'alice')


@pytest.mark.parametrize('completed', [False, True])
def test_resume_schedules_new_read_instead_of_reusing_old_generation(store, completed):
    from tests.test_task_candidates import task
    from tests.test_task_execution import engine
    from tests.test_task_scheduling import QueryClient, result, drain
    from app.tasks.conversation_worker import ConversationWorker
    record, _ = task(store, 's', goal='查询 ORD-A 当前状态')
    worker = ConversationWorker(store, engine(QueryClient()), tools=result)
    try:
        worker.reconcile()
        with store.transaction(read_only=True) as tx:
            old = tx.one('SELECT job_id FROM task_execution_wakes WHERE task_id=?', (record.task_id,))['job_id']
        if completed:
            drain(worker.executions, [old])
        switch(store, 'stopped')
        switch(store, 'all')
        worker.reconcile()
        with store.transaction(read_only=True) as tx:
            fresh = tx.one('SELECT job_id FROM task_execution_wakes WHERE task_id=?', (record.task_id,))['job_id']
        assert fresh and fresh != old
        assert drain(worker.executions, [fresh])[0]['result']['state'] == 'ready'
    finally:
        worker.close()


@pytest.mark.parametrize('fail_once', [False, True])
def test_populated_v19_upgrade_starts_stopped_and_can_retry_atomically(store, monkeypatch, fail_once):
    from app.tasks import migrations
    message(store, 's', 'alice', '旧版本原话')
    with store.transaction() as tx:
        for table in ('task_legacy_imports', 'task_rollout_audit', 'task_pipeline_users', 'task_pipeline_state'):
            tx.execute('DROP TABLE ' + table)
        tx.execute('DELETE FROM support_schema_versions WHERE version>=20')
        tx.execute("INSERT INTO support_schema_versions VALUES (19,'before-upgrade') ON CONFLICT DO NOTHING")
    store._initialized = False
    original = migrations.migrate_pipeline_rollout
    if fail_once:
        def interrupted(tx):
            original(tx)
            raise RuntimeError('interrupted migration')
        monkeypatch.setattr(migrations, 'migrate_pipeline_rollout', interrupted)
        with pytest.raises(RuntimeError):
            store.initialize()
        with store._connection(read_only=True) as tx:
            assert tx.one('SELECT MAX(version) AS v FROM support_schema_versions')['v'] == 19
        monkeypatch.setattr(migrations, 'migrate_pipeline_rollout', original)
    store.initialize()
    store = SupportStore(store.dsn)
    with store.transaction(read_only=True) as tx:
        assert not running(tx, 'alice')
        from app.support.store import SCHEMA_VERSION
        assert tx.one('SELECT MAX(version) AS v FROM support_schema_versions')['v'] == SCHEMA_VERSION
    assert store.public_history('s', 'alice')[-1]['content'] == '旧版本原话'


def test_metrics_report_database_rollout_and_unknown_operations(store, monkeypatch):
    from app.support import runtime
    from app.runtime.metrics import PlatformCollector
    monkeypatch.setattr(runtime, 'get_store', lambda: store)
    switch(store, 'selected')
    samples = [sample for metric in PlatformCollector().collect() for sample in metric.samples]
    modes = {s.labels['mode']: s.value for s in samples if s.name == 'platform_pipeline_mode'}
    assert modes == {'all': 0, 'selected': 1, 'stopped': 0}
    operations = {s.labels['status']: s.value for s in samples if s.name == 'platform_business_operations'}
    assert operations == {'running': 0, 'unknown': 0}
    # Missing business tables must not advertise a healthy scrape.
    with store.transaction() as tx:
        tx.execute('ALTER TABLE support_operations RENAME TO unavailable_operations')
    samples = [sample for metric in PlatformCollector().collect() for sample in metric.samples]
    assert next(s.value for s in samples if s.name == 'platform_store_up') == 0
    assert not any(s.name == 'platform_pipeline_mode' for s in samples)
