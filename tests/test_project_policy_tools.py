# 文件说明：通过注册工具和真实政策索引测试订单归属、规则版本及证据重验。
"""Registered policy tools through disposable MySQL and a real Chroma release."""
import json
from pathlib import Path
import shutil

import pytest

from app.config.settings import settings
from app.rag.releases import PolicyIndex
from app.rag.retriever import PolicyRetriever
from app.rag.updater import KnowledgeBaseUpdater
from app.services.commerce import service_factory
from app.services.commerce.common import decoded, dumps, ref
from app.tools.registry import execute_tool
from app.tools.request_context import reset_current_user_id, set_current_user_id
from scripts.verify_policy_knowledge import verify_review
from tests.test_core_tools import database, svc
from tests.test_policy_retrieval import Embedder


@pytest.fixture(scope='module')
def project_index(tmp_path_factory):
    root = tmp_path_factory.mktemp('project-policy')
    embedder = Embedder()
    index = PolicyIndex(root / 'chroma')
    verify_review()
    # Keep the fixture focused on the ownership/version contract; ranking of
    # all 33 topics is separately checked with the configured real embedder.
    knowledge = root / 'knowledge'
    knowledge.mkdir()
    for name in ('cancel_order', 'register', 'password_reset'):
        for suffix in ('.md', '.policy.json'):
            shutil.copyfile(Path('app/rag/knowledge') / (name + suffix), knowledge / (name + suffix))
    KnowledgeBaseUpdater(knowledge, root / 'manifest.json', embedder, None).publish_policies(index)
    return PolicyRetriever(embedder, index)


def test_registered_policy_tool_checks_ownership_versions_and_live_evidence(svc, project_index, monkeypatch):
    from app.rag import service as rag_service
    monkeypatch.setattr(rag_service, '_policy_retriever', project_index)
    monkeypatch.setattr(settings, 'retrieval_service_url', '')
    monkeypatch.setattr(settings, 'tool_allowlist', '*')
    with svc.store.transaction(write=True) as tx:
        row = tx.one("SELECT rules FROM business_policy_versions WHERE action='cancel_order'")
        rule = decoded(row['rules'])
        rule.update(rag_policy_id='project_service_cancel_order', rag_policy_version='2026-10-30.1')
        tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='cancel_order'", (dumps(rule),))
    token = set_current_user_id('alice')
    try:
        with service_factory(lambda user, scope: svc):
            result = json.loads(execute_tool('search_policies', {
                'query': '取消订单已发货运费', 'subject_ref': ref('order', 'shop', 'paid')}))
            assert result['success'], result
            evidence = result['data']['policy_evidence']
            assert evidence['context']['order_id'] == 'paid'
            assert evidence['context']['order_evidence_id'].startswith('mysql:')
            assert project_index.current(evidence, evidence['context'])
            assert any(c['action'] == 'cancel_order' and c['text_version_matches']
                       for c in result['data']['rule_checks']), result
            blocked = json.loads(execute_tool('search_policies', {
                'query': '取消订单', 'subject_ref': ref('order', 'shop', 'other')}))
            assert not blocked['success'] and blocked['error']['code'] == 'not_found'
            security = json.loads(execute_tool('search_policies', {'query': '注册密码长度验证码'}))
            assert security['success'], security
            assert any('密码' in hit['text'] for hit in security['data']['policy_evidence']['results'])
            with svc.store.transaction(write=True) as tx:
                rule['rag_policy_version'] = 'unpublished-next-version'
                tx.execute("UPDATE business_policy_versions SET rules=%s WHERE action='cancel_order'", (dumps(rule),))
            mismatch = json.loads(execute_tool('search_policies', {
                'query': '取消订单已发货运费', 'subject_ref': ref('order', 'shop', 'paid')}))
            assert any(not c['text_version_matches'] for c in mismatch['data']['rule_checks'])
    finally:
        reset_current_user_id(token)
