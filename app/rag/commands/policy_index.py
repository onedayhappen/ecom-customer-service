# 文件说明：提供审核政策索引的发布、核验、回退与来源撤回命令。
"""Publish, verify, roll back or revoke reviewed policy indexes."""
import argparse
import json
from pathlib import Path

from app.config.settings import settings
from app.rag.embedder import Embedder
from app.rag.releases import PolicyIndex
from app.rag.updater import KnowledgeBaseUpdater


def main():
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kb-dir', type=Path, default=root / settings.kb_dir)
    parser.add_argument('--persist-dir', type=Path, default=root / settings.chroma_persist_dir)
    parser.add_argument('--collection', default=settings.chroma_collection)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('publish')
    sub.add_parser('verify')
    rollback = sub.add_parser('rollback')
    rollback.add_argument('generation')
    revoke = sub.add_parser('revoke')
    revoke.add_argument('source_id')
    revoke.add_argument('--version', default='*')
    args = parser.parse_args()
    index = PolicyIndex(args.persist_dir, args.collection)
    if args.action == 'revoke':
        index.revoke(args.source_id, args.version)
    else:
        embedder = Embedder(settings.embedding_api_key or settings.openai_api_key,
            settings.embedding_base_url or settings.openai_base_url, settings.embedding_model,
            provider=settings.embedding_provider, local_model_path=root / settings.local_embedding_model_path)
        if args.action == 'rollback':
            index.activate(args.generation, expected_revision=index.revision(), model=embedder.index_identity)
        elif args.action == 'verify':
            from app.rag.chunker import policy_chunks
            result = index.verify_corpus(policy_chunks(args.kb_dir), embedder.index_identity)
            print(json.dumps(result, ensure_ascii=False))
            return
        else:
            updater = KnowledgeBaseUpdater(args.kb_dir, root / settings.kb_manifest_path, embedder, backend=None)
            updater.publish_policies(index)
    print(json.dumps(index.token(index.head()), ensure_ascii=False))


if __name__ == '__main__':
    main()
