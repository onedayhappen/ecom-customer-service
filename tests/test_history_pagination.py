"""Bounded history, cursor continuity, and historical ticket access boundaries."""
from tests.legacy_fixtures import create_legacy_task
import pytest

from app.support.store import SupportStore, SupportError


@pytest.fixture
def store(tmp_path):
    value = SupportStore(f'sqlite:///{tmp_path / "history.db"}')
    value.ensure('s', 'u')
    return value


def add_messages(store, count):
    with store.transaction() as tx:
        control = store._control(tx, 's', 'u')
        for index in range(count):
            store._event(tx, control, 'message', 'u', {'role': 'user', 'content': f'message-{index}'})
            store._event(tx, control, 'tool_evidence', 'bot', {'secret': 'internal'}, private=True)


def test_transcript_bootstrap_then_cursor_and_older_pages(store):
    add_messages(store, 125)
    first = store.transcript('s', 'u')
    assert len(first['events']) == 50 and first['next_before']
    assert first['events'][-1]['payload']['content'] == 'message-124'
    add_messages(store, 1)
    live = store.events('s', 'u', first['cursor'])
    assert len(live['events']) == 1
    seen = {event['sequence'] for event in first['events']}
    before = first['next_before']
    while before:
        page = store.transcript('s', 'u', before)
        sequences = {event['sequence'] for event in page['events']}
        assert not seen & sequences
        assert all(event['visibility'] == 'public' for event in page['events'])
        seen |= sequences
        before = page['next_before']
    assert len(seen) == 125
    with pytest.raises(SupportError): store.transcript('s', 'intruder')


def test_task_history_seeks_stably_and_rejects_other_user(store):
    def completed_tasks(count):
        with store.transaction() as tx:
            control = store._control(tx, 's', 'u')
            for _ in range(count):
                task = create_legacy_task(store, tx, control, {'intent': 'logistics_inquiry', 'text': '查询物流'}, 'u')
                task['status'] = 'completed'
                store.tasks.save(tx, control, task, 'bot')
    completed_tasks(12)
    first = store.tasks.history('s', 'u', limit=5)
    completed_tasks(2)
    seen = {task['task_id'] for task in first['tasks']}
    before = first['next_before']
    while before:
        page = store.tasks.history('s', 'u', before, limit=5)
        ids = {task['task_id'] for task in page['tasks']}
        assert not seen & ids
        seen |= ids
        before = page['next_before']
    assert len(seen) == 12
    with pytest.raises(SupportError): store.tasks.history('s', 'intruder')


def test_archived_ticket_transcript_never_includes_newer_messages(store):
    store.seed_staff('staff', 'agent')
    state = store.handoff('s', 'u')
    tid = state['ticket']['ticket_id']
    store.change(tid, 'staff', state['ticket']['version'], 'claim')
    add_messages(store, 3)
    ticket = store.detail(tid, 'staff')['ticket']
    store.change(tid, 'staff', ticket['version'], 'resolved')
    store.resume('s', 'u')
    old = store.transcript('s', 'staff', staff=True, ticket_id=tid)
    add_messages(store, 2)
    restored = store.transcript('s', 'staff', staff=True, ticket_id=tid)
    assert restored['events'] == old['events']
    assert restored['cursor'] == old['cursor']
    assert restored['state']['mode'] == 'archived'
    store.seed_staff('other-staff', 'agent')
    with pytest.raises(SupportError): store.transcript('s', 'other-staff', staff=True, ticket_id=tid)
    # Simulate an old deployment that closed the ticket before snapshots existed.
    with store.transaction() as tx:
        tx.execute('DELETE FROM support_ticket_task_snapshots WHERE ticket_id=?', (tid,))
    from app.runtime.maintenance import backfill_ticket_snapshots
    assert backfill_ticket_snapshots(store) == 1
    assert backfill_ticket_snapshots(store) == 0
    assert store.transcript('s', 'staff', staff=True, ticket_id=tid) == restored
