# 文件说明：使用离线后端测试知识文档新增、修改和删除的增量索引同步。
"""RAG 增量更新的无网络测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from app.rag.updater import KnowledgeBaseUpdater


class FakeEmbedder:
    model = "test-embedding-v1"

    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]


class InMemoryBackend:
    """只验证增量同步契约，不依赖 Chroma 运行时。"""

    def __init__(self):
        self.documents: dict[str, list] = {}

    def upsert(self, chunks, vectors, embedding_model):
        self.documents = {}
        for chunk in chunks:
            self.documents.setdefault(chunk.doc, []).append(chunk)

    def replace_document(self, doc, chunks, vectors, embedding_model):
        self.documents[doc] = list(chunks)

    def remove_document(self, doc):
        self.documents.pop(doc, None)

    def size(self):
        return sum(len(chunks) for chunks in self.documents.values())


def _write_doc(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")


def test_incremental_document_update_and_delete():
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        kb_dir = root / "knowledge"
        kb_dir.mkdir()
        _write_doc(kb_dir, "政策.md", "# 政策\n\n## 退货\n\n支持七天无理由退货。")
        _write_doc(kb_dir, "配送.md", "# 配送\n\n## 时效\n\n预计三天送达。")
        embedder = FakeEmbedder()
        backend = InMemoryBackend()
        updater = KnowledgeBaseUpdater(kb_dir, root / "manifest.json", embedder, backend)

        first = updater.update()
        assert first.added == ["政策.md", "配送.md"]
        assert backend.size() == 2
        assert len(embedder.calls) == 1

        second = updater.update()
        assert second.unchanged == ["政策.md", "配送.md"]
        assert len(embedder.calls) == 1

        _write_doc(kb_dir, "政策.md", "# 政策\n\n## 退货\n\n特殊商品不支持七天无理由退货。")
        changed = updater.update()
        assert changed.updated == ["政策.md"]
        assert backend.size() == 2
        assert len(embedder.calls) == 2

        (kb_dir / "配送.md").unlink()
        deleted = updater.update()
        assert deleted.removed == ["配送.md"]
        assert backend.size() == 1
