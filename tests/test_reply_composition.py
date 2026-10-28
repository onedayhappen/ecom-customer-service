"""Real publication transactions with deterministic reply models."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.reply_composition import compose
from app.tasks.reply_claims import render_plan
from app.config.settings import settings
from app.tasks.intake import MessageIntake
from app.tasks.replies import ReplyCoordinator
from app.tasks.repository import TaskRepository
from tests.test_coordinated_replies import ready, replies
from tests.test_task_candidates import store, task, ask
from tests.test_task_execution import Client, response, request
from tests.test_task_storage import empty_store, message
from app.tasks.scheduling import TaskSchedule


def selection(context):
    return json.dumps({'claim_ids': [m['claims'][0]['claim_id'] for m in context['matters'] if m['claims']]})


def test_combines_verified_results_with_latest_raw_dialogue(store):
    a, sa = task(store, 's', goal='查耳机物流')
    b, sb = task(store, 's', goal='键盘今天能到吗', details={'order_id': 'ORD-B'})
    ready(store, a, sa)
    ready(store, b, sb)
    seen = []
    def model(context):
        seen.append(context)
        # Acquiring a write transaction here proves composition owns no DB lock.
        with store.transaction() as tx:
            tx.execute("UPDATE task_reply_sessions SET checked_at=0 WHERE session_id='s'")
        return selection(context)
    result = ReplyCoordinator(store, 'alice', composer=model).publish('s')
    assert 'ORD-A' in result['content'] and 'ORD-B' in result['content']
    assert len(result['result_ids']) == 2
    assert result['content'] == render_plan(selection(seen[0]), seen[0])
    assert [m['content'] for m in seen[0]['current_messages']] == ['查耳机物流', '键盘今天能到吗']
    assert len(seen[0]['matters']) == 2
    assert '退款999' not in json.dumps(seen, ensure_ascii=False)  # ReAct draft isn't evidence.
    assert ReplyCoordinator(store, 'alice', composer=model).publish('s') is None
    assert len(seen) == 1 and len(replies(store)) == 1


@pytest.mark.parametrize('sid', ['s', 'old'])
def test_new_input_during_composition_discards_draft(store, sid):
    a, source = task(store, 's')
    ready(store, a, source)
    def model(_):
        MessageIntake(store).receive(sid, 'alice', request_id=str(uuid4()), content='说错了，先不要查')
        return '这段旧回复不应该发送'
    assert ReplyCoordinator(store, 'alice', composer=model).publish('s') is None
    assert not replies(store)
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM task_reply_items WHERE disposition='published'")['n'] == 0


@pytest.mark.parametrize('change', ['pause', 'handoff', 'expired_evidence'])
def test_changed_state_or_evidence_prevents_sending(store, change):
    a, source = task(store, 's')
    ready(store, a, source)
    def model(_):
        if change == 'handoff':
            store.handoff('s', 'alice')
        elif change == 'pause':
            TaskRepository(store, 'alice').update(a.task_id, 's', expected_version=1,
                change_id='pause', source=source, patch={'status': 'paused'})
        else:
            with store.transaction() as tx:
                row = tx.one("SELECT sequence,payload FROM support_events WHERE kind='task_tool_result'")
                data = json.loads(row['payload']); data['observed_epoch'] = 0
                tx.execute('UPDATE support_events SET payload=? WHERE session_id=? AND sequence=?',
                           (json.dumps(data), 's', row['sequence']))
        return '过时结果'
    assert ReplyCoordinator(store, 'alice', composer=model).publish('s') is None
    assert not replies(store)


def test_competing_composers_publish_only_once(store):
    a, source = task(store, 's')
    ready(store, a, source)
    barrier = threading.Barrier(2)
    def model(context):
        barrier.wait(5)
        return selection(context)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(ReplyCoordinator(store, 'alice', composer=model).publish, 's') for _ in range(2)]
        results = [f.result(10) for f in futures]
    assert sum(r is not None for r in results) == 1
    assert len(replies(store)) == 1


@pytest.mark.parametrize('output', ['', '{"reply":"hello"}', RuntimeError('unavailable')])
def test_model_failure_uses_qualified_evidence_fallback(store, output):
    a, source = task(store, 's')
    ready(store, a, source)
    def model(_):
        if isinstance(output, Exception):
            raise output
        return output
    result = ReplyCoordinator(store, 'alice', composer=model).publish('s')
    assert 'ORD-A' in result['content'] and '已发货' in result['content']
    assert '数据同步时间未记录' in result['content']


def test_timed_out_model_cannot_publish_late(store, monkeypatch):
    a, source = task(store, 's')
    ready(store, a, source)
    monkeypatch.setattr(settings, 'reply_composition_timeout_seconds', .05)
    release, finished = threading.Event(), threading.Event()
    def model(_):
        try:
            release.wait(5)
            return '迟到的回答'
        finally:
            finished.set()
    result = ReplyCoordinator(store, 'alice', composer=model).publish('s')
    release.set(); assert finished.wait(5)
    assert '迟到' not in result['content'] and len(replies(store)) == 1


def test_question_options_remain_code_owned_when_combined_with_facts(store):
    a, sa = task(store, 's')
    b, sb = task(store, 's', details={'order_id': 'ORD-B'})
    ready(store, a, sa)
    b = ask(store, b, sb, publish=False)
    TaskSchedule(store, 'alice').enqueue(request(b, sb))
    seen = []
    def model(context):
        seen.append(context)
        return selection(context)
    result = ReplyCoordinator(store, 'alice', composer=model).publish('s')
    assert 'ORD-A' in result['content'] and '已发货' in result['content']
    assert seen[0]['required_notices'][0] in result['content']
    assert result['question_id'] == b.waiting_for['question_id']


def test_composition_model_receives_no_tools_and_uses_separate_scene():
    captured = {}
    class Model:
        def bind_scene(self, scene):
            captured['scene'] = scene
            return self
        @property
        def chat(self):
            return SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self, **params):
            captured.update(params)
            return response('{"claim_ids":["c1"]}')
    context = dict(current_messages=[{'role': 'user', 'content': '今天能到吗'}], recent_messages=[],
                   matters=[], required_notices=[], other_matters=[])
    assert compose(Model(), 'test', context) == '{"claim_ids":["c1"]}'
    assert captured['scene'] == 'reply_composition'
    assert 'tools' not in captured and captured['response_format'] == {'type': 'json_object'}


def test_worker_does_not_block_on_reply_model_and_journals_result(store, monkeypatch):
    from app.tasks.conversation_worker import ConversationWorker
    a, source = task(store, 's')
    ready(store, a, source)
    with store.transaction() as tx:
        tx.execute("INSERT INTO task_reply_sessions(session_id) VALUES ('s') ON CONFLICT DO NOTHING")
    started, release = threading.Event(), threading.Event()
    def model(context):
        started.set()
        assert release.wait(5)
        return selection(context)
    worker = ConversationWorker(store, SimpleNamespace(compose_reply=model), concurrency=2)
    monkeypatch.setattr(worker, 'reconcile', lambda: None)
    monkeypatch.setattr(worker.executions, 'tick', lambda: None)
    try:
        worker.tick()
        assert started.wait(5)
        before = time.monotonic()
        assert worker.tick() == []
        assert time.monotonic() - before < 1
        assert len(worker.reply_futures) == 1
        release.set()
        deadline = time.monotonic() + 5
        while not replies(store) and time.monotonic() < deadline:
            worker.tick(); time.sleep(.02)
        assert 'ORD-A' in replies(store)[0]['content'] and '已发货' in replies(store)[0]['content']
    finally:
        release.set(); worker.close()
    with store.transaction(read_only=True) as tx:
        stages = [r['stage'] for r in tx.all('SELECT stage FROM interaction_trace_events')]
        assert 'reply.composition.started' in stages and 'reply.composition.finished' in stages


def test_required_context_over_budget_fails_before_model_call(monkeypatch):
    from app.session.request_budget import ContextBudgetExceeded
    monkeypatch.setattr(settings, 'context_request_input_tokens', 200)
    client = Client()
    context = dict(current_messages=[{'role': 'user', 'content': '原始需求' * 2000}],
                   recent_messages=[{'role': 'assistant', 'content': '历史消息'}],
                   matters=[], required_notices=[], other_matters=[])
    with pytest.raises(ContextBudgetExceeded):
        compose(client, 'test', context)
    assert client.requests == []


def test_already_answered_matters_are_not_described_as_still_running(store):
    a, sa = task(store, 's')
    ready(store, a, sa)
    ReplyCoordinator(store, 'alice').publish('s')
    b, sb = task(store, 's', details={'order_id': 'ORD-B'})
    ready(store, b, sb)
    seen = []
    def model(context):
        seen.append(context)
        return selection(context)
    assert ReplyCoordinator(store, 'alice', composer=model).publish('s')
    assert not seen[0]['other_matters']


def test_missing_evidence_notice_is_preserved_beside_a_verified_matter(store):
    a, sa = task(store, 's')
    b, sb = task(store, 's', details={'order_id': 'ORD-B'})
    ready(store, a, sa)
    ready(store, b, sb, result={'success': True, 'order': {'order_id': 'wrong-order'}})
    result = ReplyCoordinator(store, 'alice', composer=selection).publish('s')
    assert 'ORD-A' in result['content'] and '已发货' in result['content']
    assert '订单 ORD-B：暂时没有可核实的查询结果' in result['content']
