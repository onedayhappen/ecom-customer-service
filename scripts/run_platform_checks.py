"""Run tests without inheriting business connections from the local .env.

Default: full offline regression. --integration: dedicated real infrastructure,
already running via deploy/compose.integration.yml. No shared Redis FLUSHDB.
"""
import os
import argparse
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--integration', action='store_true')
    parser.add_argument('paths', nargs='*', help='Optional test files or node IDs')
    args = parser.parse_args()
    integration = args.integration
    artifact_root = ROOT / '.codex'
    artifact_root.mkdir(exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix='platform-checks-', dir=artifact_root))
    print(f'Test artifacts: {artifacts}', flush=True)
    with tempfile.TemporaryDirectory(prefix='ecom-checks-') as directory:
        os.environ.update({
            'ORDER_DB_DSN': '', 'ORDER_CACHE_REDIS_URL': '', 'MEMORY_REDIS_URL': '',
            'API_CONCURRENCY_REDIS_URL': '',
            'API_SESSION_POSTGRES_DSN': '', 'MEMORY_POSTGRES_DSN': '',
            'SUPPORT_DATABASE_URL': 'sqlite:///' + str(Path(directory) / 'support.db'),
            'MEMORY_STORAGE_BACKEND': 'json', 'MEMORY_ENABLED': 'false',
            'MEMORY_DIR': str(Path(directory) / 'memory'),
            'INTENT_MODEL_ENABLED': 'false', 'OBSERVABILITY_ENABLED': 'true', 'AUDIT_ENABLED': 'false',
            'MCP_ENABLED': 'false', 'RETRIEVAL_SERVICE_URL': '',
            'DEEPSEEK_API_KEY': '', 'OPENAI_API_KEY': '', 'DASHSCOPE_API_KEY': '', 'ROUTER_API_KEY': '',
            'EMBEDDING_API_KEY': '', 'EMBEDDING_BASE_URL': '', 'AUDIT_POSTGRES_DSN': '',
            'API_AUTH_USERS': '', 'API_JWT_SECRET': 'isolated-test-runner-jwt-key-32-characters',
            'ECOM_INTEGRATION': '1' if integration else '0',
        })
        import pytest
        files = ['tests']
        files = args.paths or files
        return pytest.main(['-q', '-p', 'no:cacheprovider', '--basetemp', str(artifacts / 'tmp'), *files])


if __name__ == '__main__':
    raise SystemExit(main())
