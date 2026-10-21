"""基于内容哈希的 RAG 文档增量更新。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.rag.backends.base import VectorBackend
from app.rag.chunker import chunk_markdown_file
from app.rag.embedder import Embedder


@dataclass
class UpdateResult:
    added: list[str]
    updated: list[str]
    removed: list[str]
    unchanged: list[str]


class KnowledgeBaseUpdater:
    """将 Markdown 文件变更同步到向量索引，并维护可追溯的哈希清单。"""

    def __init__(self, kb_dir: Path, manifest_path: Path, embedder: Embedder, backend: VectorBackend):
        self.kb_dir = Path(kb_dir)
        self.manifest_path = Path(manifest_path)
        self.embedder = embedder
        self.backend = backend

    def update(self) -> UpdateResult:
        manifest = self._load_manifest()
        current = self._scan_documents()
        previous_docs = manifest.get("documents", {})
        added = sorted(set(current) - set(previous_docs))
        changed = sorted(
            path for path in set(current) & set(previous_docs)
            if current[path]["sha256"] != previous_docs[path].get("sha256")
        )
        removed = sorted(set(previous_docs) - set(current))

        # 首次建库或 Embedding 模型切换时全量覆盖，避免混用不同向量空间。
        full_rebuild = (
            not previous_docs
            or manifest.get("embedding_model") != self.embedder.model
        )
        if full_rebuild:
            all_chunks = []
            for item in current.values():
                all_chunks.extend(chunk_markdown_file(self.kb_dir / item["path"]))
            if not all_chunks:
                raise ValueError("知识库中没有可索引的 Markdown 文档")
            vectors = self.embedder.encode([chunk.text for chunk in all_chunks])
            self.backend.upsert(all_chunks, vectors, self.embedder.model)
        else:
            for relative_path in added + changed:
                item = current[relative_path]
                chunks = chunk_markdown_file(self.kb_dir / item["path"])
                vectors = self.embedder.encode([chunk.text for chunk in chunks])
                self.backend.replace_document(item["doc"], chunks, vectors, self.embedder.model)
            for relative_path in removed:
                self.backend.remove_document(previous_docs[relative_path]["doc"])

        self._save_manifest({
            "version": 1,
            "embedding_model": self.embedder.model,
            "documents": current,
        })
        return UpdateResult(
            added=added if not full_rebuild else sorted(set(current) - set(previous_docs)),
            updated=changed if not full_rebuild else sorted(set(current) & set(previous_docs)),
            removed=removed,
            unchanged=sorted(set(current) - set(added) - set(changed)),
        )

    def publish_policies(self, index):
        """Build a complete reviewed generation before atomically publishing it.

        Policy and sidecar changes are inseparable. The legacy incremental
        document updater remains available until the rollout retires it.
        """
        from app.rag.chunker import policy_chunks
        revision = index.revision()
        chunks = policy_chunks(self.kb_dir)
        identity = self.embedder.index_identity
        vectors = self.embedder.encode([chunk.text for chunk in chunks])
        if self.embedder.index_identity != identity:
            raise ValueError('Embedding identity changed during policy build')
        return index.publish(chunks, vectors, identity, expected_revision=revision)

    def _scan_documents(self) -> dict[str, dict]:
        docs = {}
        for path in sorted(self.kb_dir.glob("*.md")):
            relative_path = path.relative_to(self.kb_dir).as_posix()
            docs[relative_path] = {
                "path": relative_path,
                "doc": path.stem,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return docs

    def _load_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"documents": {}}
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"知识库清单格式错误: {self.manifest_path}") from error

    def _save_manifest(self, data: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.manifest_path.with_suffix(self.manifest_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.manifest_path)
