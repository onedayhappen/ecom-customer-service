"""Processes used ONLY by explicit isolated integration tests.

API/queue/transactions/SSE/HTTP are real. Business and model data are synthetic.
Never import this module in a production entry point.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if os.environ.get('ECOM_INTEGRATION') != '1':
    raise RuntimeError('This entry point is only for isolated integration tests')

role, port = sys.argv[1], int(sys.argv[2])


def fixture_business():
    from app.api import main
    from app.tools import registry
    from app.agents.engine import AgentEngine
    from app.data_plan.enums import Intent
    import app.intent
    app.intent.recognize = lambda *args, **kw: Intent.GREETINGS
    registry._TOOL_MAP['query_order'] = lambda order_id: {'success': True, 'order': {'order_id': order_id, 'status': 'pending', 'items': []}}
    registry._TOOL_MAP['list_user_orders'] = lambda **kw: {'success': True, 'orders': [{'order_id': 'ORD-A', 'status': 'pending', 'items_summary': 'fixture item'}]}
    def refund(**kw):
        from app.support.runtime import get_store
        with get_store().transaction() as tx:
            tx.execute('INSERT INTO integration_effects(order_id) VALUES (?)', (kw['order_id'],))
        if kw['order_id'] == 'ORD-CRASH':
            time.sleep(30)  # Business committed, response not recorded: kill this Worker.
        return {'success': True, 'message': '测试退款已提交'}
    registry._TOOL_MAP['apply_refund'] = refund


if role in {'api', 'worker'}:
    fixture_business()

if role == 'api':
    import uvicorn
    uvicorn.run('app.api.main:app', host='127.0.0.1', port=port, log_level='warning')
elif role == 'worker':
    from scripts.task_worker import main
    main()
elif role == 'dispatcher':
    from scripts.notification_worker import main
    main()
elif role == 'model':
    import json
    import re
    import asyncio
    import uvicorn
    from fastapi import FastAPI, Request
    app = FastAPI()
    @app.get('/health')
    def health(): return {'status': 'ok'}
    @app.post('/v1/chat/completions')
    async def completion(request: Request):
        data = await request.json()
        messages = data['messages']
        latest = next(m['content'] for m in reversed(messages) if m['role'] == 'user')
        if 'SLOW' in latest:
            await asyncio.sleep(3)
        def output(message):
            return {'id': 'fixture', 'object': 'chat.completion', 'created': int(time.time()), 'model': data['model'],
                    'choices': [{'index': 0, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop', 'message': message}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
        context = json.loads(latest)
        if data.get('response_format', {}).get('type') == 'json_object':
            decisions = []
            for item in context['current_messages']:
                quoted = dict(session_id=context['session_id'], sequence=item['sequence'], quote=item['content'])
                found = re.search(r'ORD-[A-Z0-9-]+', item['content'])
                oid = found.group() if found else None
                candidate = next((c for c in context['candidates'] if oid and c['details'].get('order_id') == oid), None)
                if candidate is None and oid:
                    waiting = [c for c in context['candidates'] if (c.get('waiting_for') or {}).get('field') == 'order_id']
                    candidate = waiting[0] if len(waiting) == 1 else None
                detail = [dict(field='order_id', value=oid, evidence=quoted)] if oid else []
                if candidate:
                    question = candidate.get('waiting_for') or {}
                    decision = dict(action='update', effect='answer' if question.get('question_id') else 'supplement',
                        task_id=candidate['task_id'], task_version=candidate['version'],
                        question_id=question.get('question_id'), details=detail)
                else:
                    decision = dict(action='create', effect='new', goal='查询订单', details=detail)
                decisions.append(dict(**decision, sequences=[item['sequence']], evidence=[quoted]))
            return output({'role': 'assistant', 'content': json.dumps(dict(decisions=decisions), ensure_ascii=False)})
        if any(m['role'] == 'tool' for m in messages):
            return output({'role': 'assistant', 'content': '查询结果已保存。'})
        oid = context['task']['details'].get('order_id')
        if oid:
            name, args = 'query_order', {'order_id': oid}
        else:
            name, args = 'ask_user', {'field': 'order_id', 'prompt': '请提供订单号', 'options': []}
        return output({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call-fixture', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]})
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')
elif role == 'retrieval':
    import uvicorn
    from app.config.settings import settings
    from app.tools import knowledge
    from app.rag.backends.chroma_backend import ChromaBackend
    from app.rag.chunker import Chunk
    from app.rag.retriever import KnowledgeRetriever
    # Real persisted Chroma/HNSW and the real retriever pipeline; only the
    # encoder is deterministic so no model download or GPU is required.
    class FixtureEmbedder:
        model = 'fixture-vector-v1'
        def encode_one(self, text):
            if 'SLOW' in text: time.sleep(2)
            return [1., 0., 0.]
    backend = ChromaBackend(Path(os.environ['CASE_DIR']).parent / f'chroma-{port}', 'fixture_policy')
    backend.upsert([
        Chunk('returns', 'fixture-policy', 'returns', '这是合成的检索依据'),
        Chunk('shipping', 'fixture-shipping', 'shipping', '这是合成的物流依据'),
        Chunk('invoice', 'fixture-invoice', 'invoice', '这是合成的发票依据'),
    ], [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]], FixtureEmbedder.model)
    knowledge._retriever = KnowledgeRetriever(FixtureEmbedder(), backend)
    # This fixture exercises the retained generic search endpoint. Scoped policy
    # publication, readiness and revocation use test_policy_http's real index.
    settings.task_intake_enabled = False
    uvicorn.run('app.services.retrieval:app', host='127.0.0.1', port=port, log_level='warning')
else:
    raise ValueError(role)
