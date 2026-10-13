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
    from app.intent import query_rewrite
    from app.tools import registry
    from app.agents.engine import AgentEngine
    from app.data_plan.enums import Intent
    import app.intent
    query_rewrite.rewrite_query = lambda message, **kw: query_rewrite.RewriteOutcome(message=message)
    app.intent.recognize = lambda *args, **kw: Intent.GREETINGS
    main._order_locate_execute = lambda user: None
    AgentEngine._workflow_reply_generator = lambda *args: None
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
    from scripts.chat_worker import main
    main()
elif role == 'dispatcher':
    from scripts.chat_dispatcher import main
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
        order = re.search(r'ORD-[A-Z0-9-]+', latest)
        if not order:
            name, args = 'ask_user', {'prompt': '请提供退款订单号', 'options': []}
        elif messages[-1]['role'] == 'tool':
            name, args = 'apply_refund', {'order_id': order.group(), 'reason': '用户申请退款'}
        else:
            name, args = 'query_order', {'order_id': order.group()}
        return {'id': 'fixture', 'object': 'chat.completion', 'created': int(time.time()), 'model': data['model'],
                'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'call-fixture', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')
elif role == 'retrieval':
    import uvicorn
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
    uvicorn.run('app.services.retrieval:app', host='127.0.0.1', port=port, log_level='warning')
else:
    raise ValueError(role)
