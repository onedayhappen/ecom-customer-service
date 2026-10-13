"""离线构建知识库向量索引。

用法：
  # 默认走 settings.rag_backend
  python -m app.rag.commands.build_index

  python -m app.rag.commands.build_index --backend chroma

流程：
  1. 扫描 knowledge/ 下的所有 .md 文件，按二级标题切分。
  2. 调用配置的 Embedding 后端将每个 chunk 向量化。
  3. 通过 ChromaBackend 写入 PersistentClient 持久化索引。
"""

import argparse
import sys
from pathlib import Path

# 支持从任意工作目录直接执行脚本，并将生成的相对路径统一锚定到仓库根目录。
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from app.rag.backends import create_backend  # noqa: E402
from app.rag.chunker import chunk_markdown_dir  # noqa: E402
from app.rag.embedder import Embedder  # noqa: E402


def _build_backend(name: str):
    if name == "chroma":
        return create_backend(
            "chroma",
            persist_dir=ROOT / settings.chroma_persist_dir,
            collection_name=settings.chroma_collection,
        ), f"{ROOT / settings.chroma_persist_dir} (collection={settings.chroma_collection})"
    raise ValueError(f"未知后端: {name}")


def main():
    parser = argparse.ArgumentParser(description="构建知识库向量索引")
    parser.add_argument(
        "--backend",
        choices=["chroma"],
        default=settings.rag_backend,
        help=f"向量后端（默认: {settings.rag_backend}）",
    )
    args = parser.parse_args()

    kb_dir = ROOT / settings.kb_dir

    if not kb_dir.exists():
        print(f"❌ 知识库目录不存在: {kb_dir}")
        sys.exit(1)

    backend, target_desc = _build_backend(args.backend)

    print("=" * 60)
    print("  并夕夕 · 知识库索引构建")
    print(f"  后端      : {args.backend}")
    print(f"  源目录    : {kb_dir}")
    print(f"  索引目标  : {target_desc}")
    print(f"  Embedding : {settings.embedding_provider} / {settings.embedding_model}")
    print("=" * 60)

    print("\n[1/3] 扫描并切分 markdown 文档...")
    chunks = chunk_markdown_dir(kb_dir)
    if not chunks:
        print("❌ 未发现任何文档，请检查 knowledge/ 目录")
        sys.exit(1)

    by_doc: dict[str, int] = {}
    for c in chunks:
        by_doc[c.doc] = by_doc.get(c.doc, 0) + 1
    for doc, n in by_doc.items():
        print(f"   - {doc}: {n} chunk")
    print(f"   合计 {len(chunks)} 个 chunk")

    print(f"\n[2/3] 调用 {settings.embedding_provider} Embedding 批量向量化...")
    embedder = Embedder(
        api_key=settings.embedding_api_key or settings.openai_api_key,
        base_url=settings.embedding_base_url or settings.openai_base_url,
        model=settings.embedding_model,
        provider=settings.embedding_provider,
        local_model_path=ROOT / settings.local_embedding_model_path,
    )
    # 先完成所有 chunk 的向量化，再由后端全量覆盖索引。
    vectors = embedder.encode([c.text for c in chunks])
    dim = len(vectors[0]) if vectors else 0
    print(f"   完成，向量维度 = {dim}")

    print(f"\n[3/3] 写入 {args.backend} 索引...")
    backend.upsert(
        chunks=chunks,
        vectors=vectors,
        embedding_model=embedder.model,
    )
    print(f"   已写入 {backend.size()} 条向量")

    print("\n🎉 索引构建完成。")


if __name__ == "__main__":
    main()
