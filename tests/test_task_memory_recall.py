"""Real v3 recall through the task tool, execution fences and reply publication."""
import asyncio
import json

import pytest

from app.config.settings import settings
from app.memory.runtime import process_memory_jobs
from app.tasks.replies import ReplyCoordinator
from app.tools.registry import execute_tool
from app.tools.selection import for_task
from tests.test_memory_v3 import change, message
from tests.test_message_association import store
from tests.test_task_candidates import task
from tests.test_task_execution import Client, engine, request, response
from tests.test_task_storage import empty_store


@pytest.fixture(autouse=True)
def memory_config(monkeypatch, tmp_path, store):
    for key, value in dict(memory_engine='v3', memory_enabled=True, memory_storage_backend='json',
            memory_dir=str(tmp_path / 'legacy'), memory_redis_url='', memory_retrieval_enabled=False,
            memory_extraction_debounce_seconds=0, semantic_memory_retrieval_top_k=20,
            memory_retrieval_min_score=.01, api_concurrency_redis_url='', tool_allowlist='*',
            short_term_memory_enabled=False).items():
        monkeypatch.setattr(settings, key, value)


def remember(store, text='以后衣服喜欢蓝色', value='blue', *, user='alice', sid='old', action='UPDATE'):
    mid = message(store, text, sid=sid, user=user)
    item = change(mid, text, value, action=action)
    # Only extract the command from its authenticated source batch. Other
    # pending task messages are deliberately not fabricated into preferences.
    def extract(messages, _existing):
        changes = [item] if any(m['message_id'] == mid and m['is_new'] for m in messages) else []
        return dict(schema_version=3, changes=changes)
    result = process_memory_jobs(store, extract_fn=extract)
    assert result['failed'] == 0, result
    return mid


def recall(store, *after, query='本人衣服颜色偏好'):
    record, source = task(store, 's', goal='我以前喜欢什么颜色的衣服')
    client = Client(response(calls=[('recall_user_memory', {'query': query})]),
                    *(after or [response('根据有来源的历史偏好回答。')]))
    result = engine(client).execute_task(store, 'alice', request(record, source))
    return result, client


def tool_data(client):
    replies = [m for m in client.requests[1]['messages'] if m['role'] == 'tool']
    return json.loads(replies[-1]['content'])['data']


def test_real_tool_recalls_owned_v3_facts_and_publishes_a_grounded_reply(store):
    remember(store)
    remember(store, '以后衣服喜欢红色', 'red', user='bob', sid='other')
    result, client = recall(store)
    assert result['state'] == 'ready' and result['tool_calls'] == 1
    assert 'recall_user_memory' in {t['function']['name'] for t in client.requests[0]['tools']}
    data = tool_data(client)
    assert data['available'] is True and data['view'] == 'current'
    assert [f['value'] for f in data['long_term_facts']] == ['blue']
    fact = data['long_term_facts'][0]
    assert fact['source_event_id'].startswith('old:') and fact['qualifiers']['product_category'] == 'clothing'
    assert fact['version'] and fact['observed_at']
    assert result['provenance']['memory']
    reply = ReplyCoordinator(store, 'alice').publish('s')
    assert fact['content'] in reply['content'] and '红' not in reply['content']
    assert '本次明确要求优先' in reply['content'] and '办理授权' in reply['content']


def test_no_automatic_recall_until_the_model_requests_it(store, monkeypatch):
    from app.memory.task_context import TaskMemory
    monkeypatch.setattr(TaskMemory, 'recall', lambda *_: pytest.fail('unsolicited recall'))
    record, source = task(store, 's', goal='查询订单物流')
    result = engine(Client(response('需要查询物流。'))).execute_task(store, 'alice', request(record, source))
    assert result['state'] == 'ready' and 'memory' not in result['provenance']


def test_empty_recall_is_not_an_assertion_that_the_user_never_said_it(store):
    result, client = recall(store)
    assert tool_data(client)['long_term_facts'] == []
    assert result['provenance']['memory'][0]['facts'] == []
    reply = ReplyCoordinator(store, 'alice').publish('s')
    assert '本次没有查到' in reply['content'] and '不表示你从未说过' in reply['content']


def test_disabled_memory_degrades_without_claiming_empty_or_transfer(store, monkeypatch):
    monkeypatch.setattr(settings, 'memory_enabled', False)
    result, client = recall(store)
    assert result['state'] == 'ready' and tool_data(client)['available'] is False
    assert 'memory' not in result['provenance']
    reply = ReplyCoordinator(store, 'alice').publish('s')
    assert '暂时无法读取' in reply['content']
    assert '本次没有查到' not in reply['content'] and '人工队列' not in reply['content']


@pytest.mark.parametrize('action,text,value', [
    ('UPDATE', '以后衣服改为喜欢黑色', 'black'),
    ('FORGET', '衣服蓝色偏好忘掉吧', 'blue'),
])
def test_update_or_forget_during_model_call_stops_stale_execution(store, action, text, value):
    remember(store)
    def changed(_):
        remember(store, text, value, sid='old2', action=action)
        return response('blue 是当前偏好。')
    result, _ = recall(store, changed)
    assert result['state'] == 'interrupted', result
    assert ReplyCoordinator(store, 'alice').publish('s') is None
    assert not [m for m in store.public_history('s', 'alice') if m['role'] == 'assistant']


@pytest.mark.parametrize('action,text,value', [
    ('UPDATE', '以后衣服改为喜欢黑色', 'black'),
    ('FORGET', '衣服蓝色偏好忘掉吧', 'blue'),
])
def test_update_or_forget_after_execution_blocks_publication_and_evidence_reuse(store, action, text, value):
    from app.tasks.execution_store import TaskRuns
    remember(store)
    result, _ = recall(store)
    assert result['state'] == 'ready'
    remember(store, text, value, sid='old2', action=action)
    assert ReplyCoordinator(store, 'alice').publish('s') is None
    runs = TaskRuns(store, 'alice')
    assert runs.evidence(runs.repo.get(result['task_id'])) == []


def test_change_during_final_composition_discards_the_old_reply(store):
    remember(store)
    recall(store)
    def compose(context):
        ids = [c['claim_id'] for m in context['matters'] for c in m['claims']]
        remember(store, '衣服蓝色偏好忘掉吧', sid='old2', action='FORGET')
        return json.dumps({'claim_ids': ids})
    assert ReplyCoordinator(store, 'alice', composer=compose).publish('s') is None


def test_question_progress_keeps_memory_dependencies(store):
    remember(store)
    result, _ = recall(store, response(calls=[('ask_user', {'question': '这次想选什么颜色？'})]))
    assert result['state'] == 'waiting_user' and result['provenance']['memory']
    remember(store, '衣服蓝色偏好忘掉吧', sid='old2', action='FORGET')
    assert ReplyCoordinator(store, 'alice').publish('s') is None


def test_new_execution_does_not_reinject_previous_recall_results(store):
    from app.tasks.repository import TaskRepository
    remember(store)
    result, _ = recall(store)
    record = TaskRepository(store, 'alice').get(result['task_id'])
    with store.transaction(read_only=True) as tx:
        original = json.loads(tx.one('SELECT payload FROM runtime_jobs WHERE job_id=?', (result['job_id'],))['payload'])
    client = Client(response('按当前问题处理。'))
    newer = engine(client).execute_task(store, 'alice', request(record, original['request']['source']))
    assert newer['state'] == 'ready' and 'memory' not in newer['provenance']
    body = json.loads(client.requests[0]['messages'][-1]['content'])
    assert body['recent_tool_evidence'] == []


def test_memory_dependent_product_queries_also_lose_publication_eligibility(store):
    from app.tools.registry import execute_tool
    remember(store)
    record, source = task(store, 's', goal='按本人衣服颜色偏好找商品')
    def tools(name, args):
        if name == 'recall_user_memory':
            return execute_tool(name, args)
        assert name == 'search_products'
        return {'success': True, 'data': {'items': [], 'total': 0}, 'limitations': [],
                'completeness': 'complete', 'data_as_of': None}
    client = Client(response(calls=[('recall_user_memory', {'query': '本人衣服颜色偏好'})]),
                    response(calls=[('search_products', {'query': '蓝色衣服'})]), response('已查询。'))
    result = engine(client).execute_task(store, 'alice', request(record, source), tools=tools)
    assert result['state'] == 'ready' and result['tool_calls'] == 2
    with store.transaction(read_only=True) as tx:
        rows = tx.all("SELECT payload FROM support_events WHERE kind='task_tool_result'")
        receipts = [json.loads(r['payload']) for r in rows]
    assert len(receipts) == 2 and all(r['provenance']['memory'] for r in receipts)
    remember(store, '衣服蓝色偏好忘掉吧', sid='old2', action='FORGET')
    assert ReplyCoordinator(store, 'alice').publish('s') is None


def test_prepared_operation_keeps_the_memory_fence_until_confirmation(store, monkeypatch):
    from app.tasks.operations import TaskOperations
    from app.tasks.runtime import current_task
    from app.support.store import SupportError
    monkeypatch.setattr(settings, 'task_operations_enabled', True)
    monkeypatch.setattr(settings, 'api_jwt_secret', 'memory-confirmation-test-key-with-more-than-32-characters')
    class Business:
        def preview(self, record, action):
            return dict(action=action, arguments={'order_id': 'ORD-A', 'platform_code': 'pdd'},
                        business_version='v1', review={'order_id': 'ORD-A', 'title': '测试办理'})
        def write(self, *args):
            pytest.fail('stale preference must block confirmation')
    operations, card = TaskOperations(store, 'alice', business=Business()), {}
    remember(store)
    def prepare(_):
        ctx = current_task()
        saved = operations.prepare('s', ctx.record.task_id, ctx.record.version, 'apply_refund',
                                   fence=(ctx.request, ctx.payload))
        card.update(next(c for c in operations.list('s') if c['operation_id'] == saved['operation_id']))
        return response('已准备测试内容。')
    result, _ = recall(store, prepare)
    assert result['state'] == 'ready' and card['can_confirm']
    remember(store, '衣服蓝色偏好忘掉吧', sid='old2', action='FORGET')
    assert not operations.list('s')[0]['can_confirm']
    with pytest.raises(SupportError):
        operations.confirm('s', card['operation_id'], card['confirmation_token'])


def test_historical_recall_is_labeled_and_does_not_become_current_preference(store):
    remember(store)
    remember(store, '以后衣服改为喜欢黑色', 'black', sid='old2')
    result, client = recall(store, query='以前的衣服颜色偏好')
    assert result['state'] == 'ready'
    assert tool_data(client)['view'] == 'historical'
    reply = ReplyCoordinator(store, 'alice').publish('s')
    assert '不代表当前偏好' in reply['content']


def test_tool_does_not_allow_user_id_or_untrusted_standalone_access(monkeypatch):
    from app.mcp.server import call_business_tool, list_business_tools
    assert 'recall_user_memory' not in {t.name for t in asyncio.run(list_business_tools())}
    assert not asyncio.run(call_business_tool('recall_user_memory', {'query': '衣服'}))['success']
    result = json.loads(execute_tool('recall_user_memory', {'query': '衣服', 'user_id': 'bob'}))
    assert result['success'] is False
    monkeypatch.setattr(settings, 'tool_allowlist', 'read_task_context')
    assert 'recall_user_memory' not in for_task('others', '我的偏好')


@pytest.mark.parametrize('reading', [True, False])
def test_evaluation_driver_exercises_the_real_recall_tool_and_read_gate(store, reading):
    from app.evaluation.agent.memory import MemoryObserver
    from app.evaluation.agent.runtime import run_inputs
    from tests.test_agent_e2e import ScriptedClient, scenario
    remember(store)
    case = scenario()
    case.inputs.steps[0].text = '查 pdd 订单 ORD-A，另外我衣服颜色偏好是什么'
    observer = MemoryObserver(reading, None)
    with observer.installed():
        capture = run_inputs(case.inputs, client=ScriptedClient('recall_user_memory', {'query': '本人衣服颜色偏好'}),
            model='memory-tool-test', run_id='memory-tool-on' if reading else 'memory-tool-off',
            scenario_id=case.id, store=store, memory_observer=observer)
    assert capture.termination == 'settled', capture.error
    calls = [r for r in capture.observations['tool_calls'] if r['tool'] == 'recall_user_memory']
    assert calls and calls[0]['result']['success']
    facts = calls[0]['result']['data']['long_term_facts']
    assert [f['value'] for f in facts] == (['blue'] if reading else [])
    reads = [r for r in observer.records if r.get('method') == 'retrieve_relevant_long_term_memory']
    assert len(reads) == 1 and reads[0]['blocked'] == (not reading)

