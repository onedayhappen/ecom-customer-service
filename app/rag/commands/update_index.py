"""按文档内容哈希增量更新 RAG 知识库索引。"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from app.rag.backends import create_backend  # noqa: E402
from app.rag.embedder import Embedder  # noqa: E402
from app.rag.updater import KnowledgeBaseUpdater  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="增量更新知识库向量索引")
    parser.add_argument("--backend", choices=["chroma"], default=settings.rag_backend)
    args = parser.parse_args()

    backend = create_backend(
        "chroma",
        persist_dir=ROOT / settings.chroma_persist_dir,
        collection_name=settings.chroma_collection,
    )
    updater = KnowledgeBaseUpdater(
        kb_dir=ROOT / settings.kb_dir,
        manifest_path=ROOT / settings.kb_manifest_path,
        embedder=Embedder(
            settings.openai_api_key,
            settings.openai_base_url,
            settings.embedding_model,
            provider=settings.embedding_provider,
            local_model_path=ROOT / settings.local_embedding_model_path,
        ),
        backend=backend,
    )
    result = updater.update()
    print(f"新增: {result.added or '无'}")
    print(f"更新: {result.updated or '无'}")
    print(f"删除: {result.removed or '无'}")
    print(f"未变更: {len(result.unchanged)} 个文档")


if __name__ == "__main__":
    main()
