# 文件说明：测试业务读取回执、缺项收集和请求级服务替换的恢复。
"""Read envelopes and request-local service overrides need no business database."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services.commerce import CommerceService, service, service_factory
from app.services.commerce.common import ok


@pytest.mark.parametrize(
    "data,status",
    [
        ({"total": 0}, "no_match"),
        ({"total": 0, "after_sale": {"status": "processing"}}, "ok"),
        ({"membership": None}, "no_match"),
        ({"membership": None, "vip": {"records": [1]}}, "ok"),
        ({"membership": None, "store_memberships": [1]}, "ok"),
        ({"total": 1}, "ok"),
    ],
)
def test_read_envelope_keeps_status_sources_and_transaction(data, status):
    events = []
    tx = SimpleNamespace(tables={"orders", "memberships"})

    @contextmanager
    def transaction():
        events.append("begin")
        yield tx
        events.append("end")

    svc = CommerceService(SimpleNamespace(transaction=transaction), "alice")

    def query(actual_tx):
        assert actual_tx is tx
        events.append("query")
        return ok(data, limitations=["existing limitation"])

    svc.query = query
    result = svc.read("query", {})
    assert events == ["begin", "query", "end"]
    assert result["status"] == status and result["data"] == data
    assert result["sources"] == [{"kind": "mysql", "tables": ["memberships", "orders"]}]
    assert result["data_as_of"] is None and result["completeness"] == "partial"
    assert result["limitations"] == [
        "existing limitation",
        "读取时刻不代表上游同步时刻；数据同步时间未记录",
    ]


def test_missing_fields_keep_paths_and_stop_after_limit():
    @contextmanager
    def transaction():
        yield SimpleNamespace(tables=set())

    class BoundedList(list):
        def __iter__(self):
            for index in range(10000):
                if index > 100:
                    pytest.fail(
                        "read continued traversing after reaching the missing-field limit"
                    )
                yield {"field": None}

    svc = CommerceService(SimpleNamespace(transaction=transaction), "alice")
    svc.query = lambda tx: ok({"head": None, "items": BoundedList()})
    result = svc.read("query", {})
    assert result["missing_fields"] == [
        "head",
        *[f"items[{index}].field" for index in range(99)],
    ]


def test_service_factory_restores_nested_context_after_error():
    outer, inner = object(), object()
    with service_factory(lambda user, scope: outer):
        assert service("alice") is outer
        with pytest.raises(RuntimeError):
            with service_factory(lambda user, scope: inner):
                assert service("alice") is inner
                raise RuntimeError("request failed")
        assert service("alice") is outer


def test_service_factory_isolated_between_concurrent_requests():
    async def request(user):
        with service_factory(lambda actual, scope: (user, actual)):
            await asyncio.sleep(0)
            return service(user)

    async def run():
        return await asyncio.gather(request("alice"), request("bob"))

    assert asyncio.run(run()) == [("alice", "alice"), ("bob", "bob")]
