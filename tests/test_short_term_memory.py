"""Evidence, scope, cursor and failure invariants for support short-term memory."""
import json

import pytest

from app.config.settings import settings
from app.session.short_term import ContextService, process_context_jobs, raw_context
from app.session.context_budget import estimate_text_tokens
from app.support.store import SupportError, SupportStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'short_term_memory_enabled', True)
    monkeypatch.setattr(settings, 'short_term_recent_turns', 4)
    monkeypatch.setattr(settings, 'short_term_recent_tokens', 4000)
    monkeypatch.setattr(settings, 'short_term_summary_tokens', 1200)
    monkeypatch.setattr(settings, 'short_term_recall_tokens', 1200)
    monkeypatch.setattr(settings, 'short_term_compaction_batch_messages', 64)
    store = SupportStore(f"sqlite:///{tmp_path / 'context.db'}")
    store.ensure('s', 'u')
    return store


def append(store, pairs, task_id=None, private=False):
    sequences = []
    with store.transaction() as tx:
        control = store._control(tx, 's', 'u')
        for role, content in pairs:
            sequences.append(store._event(tx, control, 'message', 'u' if role == 'user' else 'bot',
                             dict(role=role, content=content, task_id=task_id), private=private))
    return sequences


def turns(store, count, *, task_id=None, prefix='普通内容'):
    return append(store, [(role, f'{prefix} {i} {role}') for i in range(count)
                          for role in ('user', 'assistant')], task_id=task_id)


def extract(previous, messages):
    source = messages[0]
    return {'items': [{'sequence': source['sequence'], 'role': source['role'],
                       'kind': 'confirmed_details', 'quote': source['content']}]}


def snapshot(store, scope='session'):
    with store.transaction(read_only=True) as tx:
        return tx.one('SELECT * FROM support_context_snapshots WHERE session_id=? AND scope_key=?', ('s', scope))


def job(store, scope='session'):
    with store.transaction(read_only=True) as tx:
        return tx.one('SELECT * FROM support_context_jobs WHERE session_id=? AND scope_key=?', ('s', scope))


def new_task(store, text):
    with store.transaction() as tx:
        control = store._control(tx, 's', 'u')
        return store.tasks.create(tx, control, {'intent': 'logistics_inquiry', 'text': text}, 'u')


def test_early_public_recall_and_internal_exclusion(store):
    append(store, [('user', '回电安排是什么'), ('assistant', '已承诺周五回电，核验码 EARLY-CALLBACK')])
    append(store, [('assistant', '回电秘密 INTERNAL-ONLY')], private=True)
    turns(store, 50)
    context = ContextService(store).build('s', 'u', query='之前承诺的回电时间是什么')
    serialized = json.dumps(context, ensure_ascii=False)
    assert 'EARLY-CALLBACK' in serialized
    assert 'INTERNAL-ONLY' not in serialized
    assert len(raw_context(context)) == 8
    assert not any('EARLY-CALLBACK' in m['content'] for m in raw_context(context))
    assert len(store.public_history('s', 'u')) == 102


def test_task_scope_and_membership_after_admission(store):
    first, second = new_task(store, '订单 A'), new_task(store, '订单 B')
    append(store, [('user', '早期 ORD-A'), ('assistant', '承诺 ORD-A 周五回电')], task_id=first['task_id'])
    append(store, [('user', '早期 ORD-B'), ('assistant', '承诺 ORD-B 周六回电')], task_id=second['task_id'])
    turns(store, 12, task_id=first['task_id'])
    # The event payload can have no task_id when routing adopts it later.
    sequence = append(store, [('user', '后来关联的首条消息')])[0]
    with store.transaction() as tx:
        tx.execute('INSERT INTO support_task_messages VALUES (?,?,?)', (first['task_id'], 's', sequence))
    context = ContextService(store).build('s', 'u', first['task_id'], query='承诺 ORD-A ORD-B 回电')
    serialized = json.dumps(context, ensure_ascii=False)
    assert 'ORD-A' in serialized and 'ORD-B' not in serialized
    assert '后来关联的首条消息' in serialized
    assert context[0]['name'] == 'short_term_state'
    with pytest.raises(SupportError):
        ContextService(store).build('s', 'other-user', first['task_id'])
    with pytest.raises(SupportError):
        ContextService(store).build('s', 'u', 'missing-task')


def test_same_text_input_exclusion_uses_event_id_and_snapshot_ceiling(store):
    append(store, [('user', '相同内容'), ('assistant', '之前回复')])
    store.receive('s', 'u', '相同内容', 'job-current')
    append(store, [('user', '后到消息'), ('assistant', '后到回复')])
    context = ContextService(store).build('s', 'u', exclude_latest_user=True)
    assert raw_context(context) == [{'role': 'user', 'content': '相同内容'},
                                    {'role': 'assistant', 'content': '之前回复'}]


def test_budget_removes_whole_old_turns_and_keeps_multiple_staff_replies(store, monkeypatch):
    monkeypatch.setattr(settings, 'short_term_recent_tokens', 256)
    append(store, [('user', '早期问题' * 100), ('assistant', '早期回复' * 100),
                   ('user', '最新问题'), ('assistant', '第一段人工回复'), ('assistant', '第二段人工回复')])
    assert [m['content'] for m in raw_context(ContextService(store).build('s', 'u'))] == [
        '最新问题', '第一段人工回复', '第二段人工回复']


def test_incremental_compaction_keeps_recent_turns_and_never_skips_backlog(store, monkeypatch):
    monkeypatch.setattr(settings, 'short_term_compaction_batch_messages', 8)
    turns(store, 80)
    batches = []

    def summarizer(previous, messages):
        batches.append([m['sequence'] for m in messages])
        return extract(previous, messages)

    for _ in range(25):
        process_context_jobs(store, summarizer)
        if job(store) is None:
            break
    assert [sequence for batch in batches for sequence in batch] == list(range(1, 153))
    assert max(map(len, batches)) <= 8
    assert snapshot(store)['through_sequence'] == 152
    assert job(store) is None
    assert any(m.get('name') == 'short_term_summary' for m in ContextService(store).build('s', 'u'))


@pytest.mark.parametrize('bad', [
    {'items': [{'sequence': 1, 'role': 'user', 'kind': 'confirmed_details', 'quote': '捏造退款已成功'}]},
    {'items': [{'sequence': 100000, 'role': 'user', 'kind': 'confirmed_details', 'quote': '普通内容 0 user'}]},
    {'items': [{'sequence': 1, 'role': 'assistant', 'kind': 'agent_commitments', 'quote': '普通内容 0 user'}]},
    {'items': []},
])
def test_invalid_summary_keeps_cursor_and_retries_without_breaking_chat(store, bad):
    turns(store, 8)
    result = process_context_jobs(store, lambda previous, messages: bad)
    assert result['failed'] == 1
    assert snapshot(store) is None
    assert job(store)['attempts'] == 1
    assert job(store)['last_error'] == 'ValueError'
    assert len(raw_context(ContextService(store).build('s', 'u'))) == 8


def test_internal_quote_cannot_enter_summary_and_raw_events_unchanged(store):
    private_seq = append(store, [('user', '私有核验信息')], private=True)[0]
    turns(store, 8)
    result = process_context_jobs(store, lambda previous, messages: {'items': [
        {'sequence': private_seq, 'role': 'user', 'kind': 'confirmed_details', 'quote': '私有核验信息'}]})
    assert result['failed'] == 1
    assert snapshot(store) is None
    with store.transaction(read_only=True) as tx:
        assert tx.one('SELECT COUNT(*) AS n FROM support_events WHERE session_id=?', ('s',))['n'] == 17


def test_snapshot_compare_and_swap_rejects_stale_worker(store):
    turns(store, 10)

    def raced(previous, messages):
        # This second worker commits while the first is outside its DB txn.
        assert process_context_jobs(store, extract)['processed'] == 1
        return extract(previous, messages)

    result = process_context_jobs(store, raced)
    assert result['stale'] == 1
    assert snapshot(store)['version'] == 1
    assert snapshot(store)['through_sequence'] == 12


def test_new_events_during_summary_preserve_pending_job(store):
    turns(store, 10)

    def arriving(previous, messages):
        turns(store, 1, prefix='推理期间新消息')
        return extract(previous, messages)

    result = process_context_jobs(store, arriving)
    assert result['processed'] == 1
    assert snapshot(store)['through_sequence'] == 12
    assert job(store)['requested_sequence'] == 22
    assert process_context_jobs(store, extract)['processed'] == 1
    assert snapshot(store)['through_sequence'] == 14


def test_unconfigured_model_defers_without_losing_durable_job(store, monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', '')
    turns(store, 10)
    assert process_context_jobs(store)['deferred'] == 1
    assert job(store)['requested_sequence'] == 20
    assert snapshot(store) is None


def test_task_state_excludes_confirmation_secrets(store):
    task = new_task(store, '查询订单')
    with store.transaction() as tx:
        tx.execute('UPDATE conversation_tasks SET slots=?,question=? WHERE task_id=?', (
            json.dumps({'order_id': 'ORD-CURRENT', 'confirmation_token': 'SECRET-SLOT'}),
            json.dumps({'field': 'details', 'prompt': '补充信息', 'confirmation_token': 'SECRET-QUESTION'}),
            task['task_id']))
    context = ContextService(store).build('s', 'u', task['task_id'])
    assert 'ORD-CURRENT' in context[0]['content']
    assert 'SECRET' not in json.dumps(context)


def test_public_human_reply_is_available_in_recent_recall_and_summary(store):
    append(store, [('user', '请安排回电'), ('agent', '人工承诺周五回电 STAFF-PROMISE')])
    assert raw_context(ContextService(store).build('s', 'u'))[-1] == {
        'role': 'assistant', 'content': '人工承诺周五回电 STAFF-PROMISE'}
    turns(store, 8)
    recalled = ContextService(store).build('s', 'u', query='staff-promise 回电')
    assert 'STAFF-PROMISE' in json.dumps(recalled)

    def human_quote(previous, messages):
        source = next(m for m in messages if 'STAFF-PROMISE' in m['content'])
        assert source['role'] == 'assistant'
        return {'items': [dict(sequence=source['sequence'], role=source['role'],
                               kind='agent_commitments', quote=source['content'])]}

    assert process_context_jobs(store, human_quote)['processed'] == 1
    assert 'STAFF-PROMISE' in snapshot(store)['payload']


def test_consecutive_unanswered_messages_remain_bounded(store):
    append(store, [('user', f'连续补充 {i}') for i in range(45)])
    context = raw_context(ContextService(store).build('s', 'u'))
    assert 1 <= len(context) <= 5
    assert context[-1]['content'] == '连续补充 44'


def test_large_batch_obeys_input_budget_and_compacts_every_evicted_turn(store):
    append(store, [(role, f'{i} ' + ('较长业务描述' * 550 if role == 'user' else '已收到补充内容'))
                   for i in range(12) for role in ('user', 'assistant')])
    visited = []

    def bounded(previous, messages):
        assert estimate_text_tokens(json.dumps({'previous': previous, 'messages': messages},
                                               ensure_ascii=False, separators=(',', ':'))) <= 7000
        visited.extend(m['sequence'] for m in messages)
        source = messages[0]
        return {'items': [dict(sequence=source['sequence'], role=source['role'],
                               kind='confirmed_details', quote=source['content'][:20])]}

    # Online 4k budget retains one large turn; all earlier turns must eventually
    # be covered, even though the configured turn count would have kept four.
    assert len(raw_context(ContextService(store).build('s', 'u'))) == 2
    for _ in range(20):
        assert process_context_jobs(store, bounded)['failed'] == 0
        if job(store) is None:
            break
    assert visited == list(range(1, 23))
    assert snapshot(store)['through_sequence'] == 22


def test_oversized_source_does_not_advance_cursor_or_disappear(store):
    append(store, [('user', '超长来源消息' * 3000), ('assistant', '已收到')])
    turns(store, 8)
    assert process_context_jobs(store, extract)['failed'] == 1
    assert snapshot(store) is None
    assert job(store)['requested_sequence'] == 18
    context = ContextService(store).build('s', 'u', query='超长来源消息')
    assert any(m.get('name') == 'short_term_recall' for m in context)


def test_separate_old_quotes_cannot_authorize_a_synthetic_join():
    from app.session.short_term import _validate_summary

    previous = {'items': [dict(sequence=1, role='user', kind='confirmed_details', quote=quote)
                          for quote in ('第一段', '另一段')]}
    candidate = {'items': [dict(sequence=1, role='user', kind='confirmed_details', quote='第一段\n另一段')]}
    with pytest.raises(ValueError, match='exact public source'):
        _validate_summary(candidate, previous, [])
