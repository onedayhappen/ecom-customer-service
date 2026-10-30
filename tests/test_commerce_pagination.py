# 文件说明：测试业务游标格式、查询范围绑定及数据变化后的翻页行为。
"""Cursor compatibility, isolation and bounded-query regression checks."""

import base64
import hmac
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.commerce.base import BaseService
from app.services.commerce.common import BusinessError, digest, dumps


SECRET = "pagination-test-signing-key"
NOW = 1700000000


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr("app.services.commerce.pagination.time.time", lambda: NOW)
    return BaseService(
        None,
        "alice",
        scope=SimpleNamespace(platform_code="shop", store_id="m"),
        secret=SECRET,
    )


def legacy_cursor(tool, conditions, **payload):
    """The pre-refactor wire format, independent of the cursor implementation."""
    body = dict(
        binding=digest(["alice", tool, conditions, "shop", "m"]),
        expires=NOW + 900,
        **payload,
    )
    raw = base64.urlsafe_b64encode(dumps(body).encode()).decode()
    return raw + "." + hmac.new(SECRET.encode(), raw.encode(), "sha256").hexdigest()


def test_snapshot_cursor_preserves_wire_format_and_continues_old_page(svc):
    rows = [{"id": n} for n in range(5)]
    args = {"status": "all"}
    first = svc.page(rows, "orders", args, size=2)
    cursor = legacy_cursor("orders", args, snapshot=digest(rows), offset=2)
    assert first["next_cursor"] == cursor
    second = svc.page(rows, "orders", {**args, "cursor": cursor}, size=2)
    last = svc.page(rows, "orders", {**args, "cursor": second["next_cursor"]}, size=2)
    assert (
        first["data"]["items"] + second["data"]["items"] + last["data"]["items"] == rows
    )
    assert last["next_cursor"] is None and not last["data"]["has_more"]
    assert second["data"]["applied_conditions"] == args
    assert args == {"status": "all"}


@pytest.mark.parametrize(
    "change",
    ["user", "platform", "store", "tool", "filter", "secret", "signature", "expired"],
)
def test_cursor_cannot_cross_identity_scope_or_query(svc, monkeypatch, change):
    rows = [{"id": n} for n in range(3)]
    token = svc.page(rows, "orders", {"status": "all"}, size=1)["next_cursor"]
    tool, args = "orders", {"status": "all", "cursor": token}
    if change == "user":
        svc.user = "bob"
    elif change == "platform":
        svc.scope.platform_code = "other"
    elif change == "store":
        svc.scope.store_id = "other"
    elif change == "tool":
        tool = "coupons"
    elif change == "filter":
        args["status"] = "paid"
    elif change == "secret":
        svc.secret = "another-secret"
    elif change == "signature":
        args["cursor"] = token[:-1] + ("0" if token[-1] != "0" else "1")
    else:
        monkeypatch.setattr(
            "app.services.commerce.pagination.time.time", lambda: NOW + 901
        )
    with pytest.raises(BusinessError) as error:
        svc.page(rows, tool, args, size=1)
    assert error.value.code == "invalid_cursor"


def test_snapshot_changes_require_restart(svc):
    rows = [{"id": 1}, {"id": 2}]
    token = svc.page(rows, "orders", {}, size=1)["next_cursor"]
    with pytest.raises(BusinessError) as error:
        svc.page([*rows, {"id": 3}], "orders", {"cursor": token}, size=1)
    assert error.value.code == "state_changed"


@pytest.mark.parametrize("offset", [-1, 5, "1", None])
def test_signed_but_invalid_snapshot_offset_is_rejected(svc, offset):
    rows = [{"id": n} for n in range(5)]
    token = legacy_cursor("orders", {}, snapshot=digest(rows), offset=offset)
    with pytest.raises(BusinessError) as error:
        svc.page(rows, "orders", {"cursor": token})
    assert error.value.code == "invalid_cursor"


class PageTransaction:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def one(self, sql, args):
        self.calls.append((sql, args))
        return {"n": 3}

    def all(self, sql, args):
        self.calls.append((sql, args))
        return self.rows


def test_keyset_legacy_cursor_keeps_datetime_tiebreaker_and_two_queries(svc):
    date = datetime(2026, 10, 1)
    rows = [{"created_at": date, "id": n} for n in range(3)]
    query = "SELECT created_at,id FROM events WHERE user_id=%s"
    keys = ("created_at", "id")
    first_tx = PageTransaction(rows)
    first = svc.sql_page(first_tx, query, ["alice"], "events", {}, keys=keys, size=2)
    token = legacy_cursor(
        "events",
        {},
        keys=list(keys),
        last=[
            {"type": "datetime", "value": date},
            {"type": "value", "value": 1},
        ],
    )
    assert first["next_cursor"] == token
    second_tx = PageTransaction(rows[2:])
    second = svc.sql_page(
        second_tx, query, ["alice"], "events", {"cursor": token}, keys=keys, size=2
    )
    assert first["data"]["items"] + second["data"]["items"] == rows
    assert second["next_cursor"] is None
    assert len(first_tx.calls) == len(second_tx.calls) == 2
    assert second_tx.calls[0] == (
        "SELECT COUNT(*) AS n FROM (" + query + ") page_count",
        ("alice",),
    )
    assert second_tx.calls[1] == (
        "SELECT * FROM ("
        + query
        + ") page_rows WHERE (created_at,id) > (%s,%s) ORDER BY created_at,id LIMIT %s",
        ("alice", date, 1, 3),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"keys": ["other"], "last": []},
        {"keys": ["id"], "last": []},
        {"keys": ["id"], "last": [{"type": "datetime", "value": "invalid"}]},
    ],
)
def test_invalid_keyset_cursor_does_not_query_database(svc, payload):
    tx = PageTransaction([])
    token = legacy_cursor("events", {}, **payload)
    with pytest.raises(BusinessError) as error:
        svc.sql_page(
            tx, "SELECT id FROM events", [], "events", {"cursor": token}, keys=("id",)
        )
    assert error.value.code == "invalid_cursor"
    assert tx.calls == []


def test_pagination_limits_and_signing_requirement(svc):
    with pytest.raises(BusinessError) as error:
        svc.page([{}] * 10001, "orders", {})
    assert error.value.code == "insufficient_data"
    svc.secret = ""
    assert svc.page([], "orders", {})["next_cursor"] is None
    with pytest.raises(BusinessError) as error:
        svc.page([{}, {}], "orders", {}, size=1)
    assert error.value.code == "temporarily_unavailable"
