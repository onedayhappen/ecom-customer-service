"""Opt-in REAL PostgreSQL/Redis process tests; no FLUSHDB or business DB.

Use deploy/compose.integration.yml and ECOM_INTEGRATION=1. Each Redis test uses
a UUID namespace; PostgreSQL is the dedicated ecom_concurrency_test database.
"""
import json
import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from uuid import uuid4

import pytest

from app.support.store import SupportStore, SupportError

DSN = 'postgresql://test_runner:isolated_test_only@127.0.0.1:25432/ecom_concurrency_test'
REDIS = 'redis://127.0.0.1:26379/0'
pytestmark = pytest.mark.skipif(os.getenv('ECOM_INTEGRATION') != '1', reason='dedicated integration environment only')


def consume_shared_budget(prefix):
    from app.runtime.budgets import Buckets, RateExceeded
    try:
        Buckets(REDIS, prefix).consume([('global', 1, 1), ('tenant', 1, 1)])
        return True
    except RateExceeded:
        return False


@pytest.fixture
def infra():
    from redis import Redis
    prefix = 'itest:budget:' + uuid4().hex
    client = Redis.from_url(REDIS)
    client.ping()
    try:
        yield prefix
    finally:
        keys = list(client.scan_iter(match=prefix + ':*'))
        if keys:
            client.delete(*keys)
        client.close()


def test_distributed_rate_budget_is_atomic_across_processes(infra):
    with ProcessPoolExecutor(4, mp_context=get_context('spawn')) as pool:
        assert sum(pool.map(consume_shared_budget, [infra] * 8)) == 1
