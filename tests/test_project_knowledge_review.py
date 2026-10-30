# 文件说明：测试审核摘要的跨平台一致性及正文、元数据和源码变更检测。
"""Review fingerprints remain portable, but cannot silently accept new content."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.verify_policy_knowledge import ROOT, verify_review


@pytest.fixture
def portable_review(tmp_path):
    review = deepcopy(verify_review())
    record = review['documents']['register.md']
    review['documents'] = {'register.md': record}
    paths = ['app/rag/knowledge/register.md', 'app/rag/knowledge/register.policy.json',
             *record['review_basis']]
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8'))
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs/knowledge_review.json').write_text(json.dumps(review), encoding='utf-8')
    assert verify_review(tmp_path) == review
    return tmp_path, paths


@pytest.mark.parametrize('index', [0, 1, 2])
def test_body_metadata_and_source_changes_need_review(portable_review, index):
    root, paths = portable_review
    path = root / paths[index]
    path.write_text(path.read_text(encoding='utf-8') + '\n ', encoding='utf-8')
    with pytest.raises(ValueError, match='stale|metadata changed|basis changed'):
        verify_review(root)


def test_new_document_cannot_reuse_existing_review(portable_review):
    root, _ = portable_review
    (root / 'app/rag/knowledge/new.md').write_text('# New policy', encoding='utf-8')
    with pytest.raises(ValueError, match='inventory differs'):
        verify_review(root)
