"""Provider-boundary context budgets: real client paths with fake providers."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.observability.llm_client import LlmClient
from app.session.context_budget import estimate_request_tokens
from app.session.request_budget import ContextBudgetExceeded, apply_request_budget, request_input_limit


@pytest.fixture(autouse=True)
def budget_settings(monkeypatch):
    monkeypatch.setattr(settings, "context_request_budget_enabled", True)
    monkeypatch.setattr(settings, "context_request_input_tokens", 1200)
    monkeypatch.setattr(settings, "context_model_window_tokens", 0)
    monkeypatch.setattr(settings, "context_window_margin_tokens", 128)
    monkeypatch.setattr(settings, "model_max_output_tokens", 256)


def call_message(*ids):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "query_order", "arguments": '{"order_id":"A-123"}'}}
        for call_id in ids
    ]}


def tool_message(call_id, **data):
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(data, ensure_ascii=False)}


def test_optional_recall_drops_before_summary_or_history(monkeypatch):
    messages = [
        {"role": "system", "content": "Always check business evidence."},
        {"role": "system", "name": "short_term_state", "content": '{"order_id":"current-1"}'},
        {"role": "system", "name": "short_term_recall", "content": "旧原文" * 400},
        {"role": "system", "name": "short_term_summary", "content": "历史承诺" * 20},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]
    params = {"messages": messages}
    expected = {"messages": [message for message in messages if message.get("name") != "short_term_recall"]}
    monkeypatch.setattr(settings, "context_request_input_tokens", estimate_request_tokens(expected))
    original = deepcopy(params)
    assert apply_request_budget(params) == expected
    assert params == original


def test_remove_whole_old_turn_with_parallel_tool_results(monkeypatch):
    old = [{"role": "user", "content": "old"}, call_message("old-a", "old-b"),
           tool_message("old-a", success=True, detail="a" * 1000),
           tool_message("old-b", success=False, error="b" * 1000),
           {"role": "assistant", "content": "old reply"}]
    current = [{"role": "user", "content": "current"}, call_message("new-a", "new-b"),
               tool_message("new-a", success=True, order_id="A-123"),
               tool_message("new-b", success=False, error="not found")]
    params = {"messages": [{"role": "system", "content": "rules"}] + old + current}
    expected = {"messages": [params["messages"][0]] + current}
    monkeypatch.setattr(settings, "context_request_input_tokens", estimate_request_tokens(expected))
    result = apply_request_budget(params)
    assert result == expected
    # The remaining batch is independently valid when budgeted again.
    assert apply_request_budget(result) == result


def test_large_tool_result_retains_business_facts_and_original():
    params = {"messages": [
        {"role": "system", "content": "Use confirmed tool fields only."},
        {"role": "user", "content": "退款结果"},
        call_message("one"),
        tool_message("one", success=False, error="refund declined", order_id="A-123", status="failed",
                     detail="long detail " * 10000, data={"operation_id": "op-1", "description": "细节" * 5000}),
    ]}
    original = deepcopy(params)
    result = apply_request_budget(params)
    assert estimate_request_tokens(result) <= settings.context_request_input_tokens
    projected = json.loads(result["messages"][-1]["content"])
    assert projected["_context_projection"]["incomplete"] is True
    assert projected["result"]["success"] is False
    assert projected["result"]["error"] == "refund declined"
    assert projected["result"]["order_id"] == "A-123"
    assert projected["result"]["status"] == "failed"
    assert projected["result"]["data"]["operation_id"] == "op-1"
    assert result["messages"][-1]["tool_call_id"] == "one"
    assert params == original


def test_consecutive_old_user_messages_can_be_removed():
    messages = [{"role": "user", "content": "old details" * 1000},
                {"role": "user", "content": "more old details" * 1000},
                {"role": "user", "content": "current request"}]
    result = apply_request_budget({"messages": messages})
    assert result["messages"] == [messages[-1]]


def test_sql_projection_keeps_column_alignment():
    columns = [f"column_{i}" for i in range(14)]
    rows = [[f"value_{i}" for i in range(14)] for _ in range(300)]
    params = {"messages": [{"role": "user", "content": "query"}, call_message("sql"),
                           tool_message("sql", success=True, columns=columns, rows=rows, row_count=300)]}
    result = apply_request_budget(params)
    payload = json.loads(result["messages"][-1]["content"])["result"]
    assert payload["columns"] == columns
    assert all(len(row) == len(columns) for row in payload["rows"] if isinstance(row, list))
    assert payload["row_count"] == 300
    assert "_context_omitted_items" in payload["rows"][-1]


def test_many_current_tool_calls_keep_every_result(monkeypatch):
    monkeypatch.setattr(settings, "context_request_input_tokens", 6500)
    ids = [f"call-{i}" for i in range(18)]
    params = {"messages": [{"role": "user", "content": "check each order"}, call_message(*ids)] + [
        tool_message(call_id, success=True, order_id=f"order-{i}", detail="z" * 6000)
        for i, call_id in enumerate(ids)
    ]}
    result = apply_request_budget(params)
    assert estimate_request_tokens(result) <= 6500
    assert [message["tool_call_id"] for message in result["messages"] if message["role"] == "tool"] == ids
    assert result["messages"][1] == params["messages"][1]
    assert apply_request_budget(result) == result


@pytest.mark.parametrize("message", [
    {"role": "user", "content": "当前输入" * 1000},
    {"role": "system", "content": "固定规则" * 1000},
    {"role": "system", "name": "short_term_state", "content": "当前状态" * 1000},
])
def test_pinned_content_is_never_silently_truncated(message):
    params = {"messages": [message]}
    original = deepcopy(params)
    with pytest.raises(ContextBudgetExceeded) as captured:
        apply_request_budget(params)
    assert captured.value.estimated_tokens > captured.value.limit_tokens
    assert captured.value.reason == "required_context"
    assert params == original


def test_tool_schemas_and_output_reserve_share_window(monkeypatch):
    monkeypatch.setattr(settings, "context_request_input_tokens", 12000)
    monkeypatch.setattr(settings, "context_model_window_tokens", 4000)
    params = {"messages": [{"role": "user", "content": "question"}], "max_completion_tokens": 2500,
              "tools": [{"type": "function", "function": {"name": "query", "description": "规则" * 3000}}]}
    assert request_input_limit(params) == 4000 - 2500 - 128
    with pytest.raises(ContextBudgetExceeded):
        apply_request_budget(params)


@pytest.mark.parametrize("messages", [
    [tool_message("orphan", success=True)],
    [call_message("a", "b"), tool_message("a", success=True)],
    [call_message("a"), tool_message("a", success=True), tool_message("a", success=True)],
    [call_message("a", "a"), tool_message("a", success=True)],
    [call_message("a"), {"role": "user", "content": "unanswered tool call"}],
])
def test_existing_invalid_protocol_fails_predictably(messages):
    with pytest.raises(ContextBudgetExceeded, match="工具上下文不完整") as captured:
        apply_request_budget({"messages": messages})
    assert captured.value.reason == "invalid_tool_protocol"


def test_disable_budget_is_explicit_and_still_returns_independent_copy(monkeypatch):
    monkeypatch.setattr(settings, "context_request_budget_enabled", False)
    params = {"messages": [{"role": "user", "content": "large" * 5000}]}
    result = apply_request_budget(params)
    assert result == params
    result["messages"][0]["content"] = "changed"
    assert params["messages"][0]["content"] != "changed"


@pytest.mark.parametrize("async_provider", [False, True])
def test_client_applies_budget_before_sync_and_async_provider(monkeypatch, async_provider):
    import app.observability.llm_client as llm_module
    import app.runtime.budgets as runtime_budgets

    calls, audits = [], []
    monkeypatch.setattr(runtime_budgets, "model_budget", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(llm_module, "audit_event", lambda **kwargs: audits.append(kwargs))
    monkeypatch.setattr(llm_module, "emit_llm", lambda **kwargs: None)

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="fake", usage=SimpleNamespace(prompt_tokens=81, completion_tokens=9),
                               choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))])

    async def async_create(**kwargs):
        return create(**kwargs)

    raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=async_create if async_provider else create)))
    client = LlmClient(raw_client=raw, model="fake", default_scene="react")
    messages = [{"role": "system", "content": "rules"},
                {"role": "user", "content": "old" * 1000},
                {"role": "assistant", "content": "answer" * 1000},
                {"role": "user", "content": "latest input"}]
    original = deepcopy(messages)
    if async_provider:
        result = asyncio.run(client.generate_async(messages=messages))
    else:
        result = client.generate(messages=messages)
        # Exercise async-to-sync provider fallback too.
        asyncio.run(client.generate_async(messages=messages))
    assert result.content == "ok"
    assert all(estimate_request_tokens(call) <= 1200 for call in calls)
    assert all(call["max_tokens"] == 256 for call in calls)
    assert all(call["messages"] == [original[0], original[-1]] for call in calls)
    assert messages == original
    assert audits[0]["name"] == "react"
    summary = audits[0]["summary"]
    assert summary["context_input_estimated_before"] > summary["context_input_estimated_after"]
    assert summary["context_messages_removed"] == 2
    assert summary["prompt_tokens"] == 81


def test_client_rejects_oversized_input_before_contacting_provider(monkeypatch):
    client = LlmClient(raw_client=object(), model="fake")
    calls = []
    monkeypatch.setattr(client, "_raw_create", lambda **kwargs: calls.append(kwargs))
    params = {"scene": "rewrite", "model": "fake", "messages": [{"role": "user", "content": "当前输入" * 1000}]}
    original = deepcopy(params)
    with pytest.raises(ContextBudgetExceeded):
        client._sync_call(params)
    assert params == original
    assert calls == []
