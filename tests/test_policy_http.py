# 文件说明：通过独立检索进程测试政策鉴权、范围、撤回与远端失败处理。
"""Real HTTP and separate retrieval processes with offline Chroma fixtures."""
from contextlib import ExitStack
from multiprocessing import get_context
import socket
import time

import httpx
import pytest

from app.config.settings import settings
from app.rag import remote
from tests.test_policy_retrieval import corpus, context, document

TOKEN = 'isolated-policy-service-token-32-characters'


def serve(root, port):
    import uvicorn
    from app.rag import service as knowledge
    from app.rag.releases import PolicyIndex
    from app.rag.retriever import PolicyRetriever
    from app.services.retrieval import app
    from tests.test_policy_retrieval import Embedder
    settings.retrieval_service_token = TOKEN
    settings.retrieval_service_url = ''
    settings.api_concurrency_redis_url = ''
    settings.task_intake_enabled = True
    knowledge._policy_retriever = PolicyRetriever(Embedder(), PolicyIndex(root))
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='error')


@pytest.fixture
def servers(corpus, monkeypatch):
    root, index, _, publish = corpus
    document(root, 'refund')
    publish()
    processes, urls = [], []
    with ExitStack() as stack:
        client = stack.enter_context(httpx.Client(timeout=5, trust_env=False))
        try:
            for _ in range(2):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                process = get_context('spawn').Process(target=serve, args=(index.root, port))
                process.start()
                processes.append(process)
                url = f'http://127.0.0.1:{port}'
                deadline = time.monotonic() + 25
                while True:
                    assert process.is_alive(), f'Retrieval process exited: {process.exitcode}'
                    try:
                        if client.get(url + '/health').status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, 'Retrieval service did not start'
                    time.sleep(.1)
                urls.append(url)
            monkeypatch.setattr(settings, 'retrieval_service_url', urls[0])
            monkeypatch.setattr(settings, 'retrieval_service_token', TOKEN)
            yield index, client, urls
        finally:
            remote.close_clients()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(10)


def test_policy_http_auth_scope_and_cross_process_revocation(servers, monkeypatch):
    index, client, urls = servers
    payload = dict(query='退货规则', context=context().model_dump(mode='json'))
    assert client.post(urls[0] + '/v1/policies/search', json=payload).status_code == 401
    headers = {'Authorization': 'Bearer ' + TOKEN}
    for extra in ({'audience': 'staff'}, {'selection_basis': 'request_at'}):
        forged = {**payload, 'context': {**payload['context'], **extra}}
        assert client.post(urls[0] + '/v1/policies/search', json=forged, headers=headers).status_code == 422
    assert client.post(urls[0] + '/v1/policies/search', json={**payload, 'system_prompt': 'override'}, headers=headers).status_code == 422
    saved = remote.policy_search('退货规则', 3, context())
    assert saved['success'] and saved['results'][0]['source_id'] == 'refund'
    monkeypatch.setattr(settings, 'retrieval_service_url', urls[1])
    assert remote.policy_validate(saved, context())
    assert not remote.policy_validate(saved, context(store_id='shop-b'))
    forged = {**saved, 'results': [{**saved['results'][0], 'text': '自动退款成功'}]}
    assert not remote.policy_validate(forged, context())
    index.revoke('refund')
    assert not remote.policy_validate(saved, context())
    assert remote.policy_search('退货规则', 3, context())['success'] is False
    monkeypatch.setattr(settings, 'retrieval_service_url', urls[0])
    assert not remote.policy_validate(saved, context())


def test_remote_failure_never_uses_local_or_old_policy_cache(servers, monkeypatch):
    from app.rag import service as knowledge
    _, _, urls = servers
    saved = knowledge.search_policy('退货规则', 3, context())
    assert saved['success']
    monkeypatch.setattr(settings, 'retrieval_service_token', 'invalid-token')
    def local_forbidden():
        pytest.fail('Remote outage must not fall back to local policy')
    monkeypatch.setattr(knowledge, '_get_policy_retriever', local_forbidden)
    assert knowledge.search_policy('退货规则', 3, context())['success'] is False
    assert knowledge.policy_result_current(saved) is False
