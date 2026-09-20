"""业务链日志监控（第11期）测试：BusinessTracer 写 JSONL / client 插桩 / 幂等 / Token 摘要。"""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

from app.config.settings import settings
from app.execution.types import ToolStep
from app.observability.query import load_traces, summarize
from app.observability.tracer import BusinessTracer, patch_client


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0
        self.create = self._create

    def _create(self, *args, **kwargs):  # noqa: ANN001, ANN202
        self.calls += 1
        return SimpleNamespace(
            model="test-model",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=[]))],
        )


def _fake_client() -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))


def test_tracer_writes_jsonl(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "observability_log_dir", str(tmp_path))
    tracer = BusinessTracer.start("s1", "u1", "你好")
    tracer.record("intent", intent="greetings")
    tracer.set_response(
        reply="您好，请问有什么可以帮您？", requires_human=False,
        follow_up_question=None, intent="greetings", execution="rule", confidence=1.0,
    )
    payload = tracer.finish()

    assert payload["message"] == "你好"
    assert payload["response"]["intent"] == "greetings"
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["request_id"] == tracer.request_id
    assert data["response"]["reply"].startswith("您好")


def test_patch_client_records_llm_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "observability_log_dir", str(tmp_path))
    client = _fake_client()
    patch_client(client)
    tracer = BusinessTracer.start("s1", "u1", "查订单")
    client.chat.completions.create(model="m", messages=[])
    assert len(tracer.llm_calls) == 1
    call = tracer.llm_calls[0]
    assert call["total_tokens"] == 15
    assert call["model"] == "test-model"
    assert call["latency_ms"] >= 0
    tracer.finish()


def test_patch_is_idempotent() -> None:
    client = _fake_client()
    patch_client(client)
    patch_client(client)
    patch_client(client)
    tracer = BusinessTracer.start("s1", "u1", "x")
    client.chat.completions.create(model="m", messages=[])
    assert client.chat.completions.calls == 1  # wrapper 只包一层
    tracer.finish()


def test_no_active_tracer_is_noop() -> None:
    client = _fake_client()
    patch_client(client)
    # 未 start 时调用 create：不崩溃、不采集
    client.chat.completions.create(model="m", messages=[])
    assert client.chat.completions.calls == 1


def test_confirmation_token_only_sha256(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "observability_log_dir", str(tmp_path))
    tracer = BusinessTracer.start("s1", "u1", "退款")
    tracer.set_tool_steps([
        ToolStep(
            name="apply_refund", arguments={"order_id": "O1"}, result={"success": True},
            confirmation_token="secret-token",
        ),
    ])
    tracer.set_response(
        reply="已受理", requires_human=False, follow_up_question=None,
        intent="after_sale_request", execution="workflow", confidence=0.95,
    )
    payload = tracer.finish()
    raw = json.dumps(payload, ensure_ascii=False)
    assert "secret-token" not in raw
    assert payload["tool_steps"][0]["confirmation_token_sha256"] == hashlib.sha256(
        b"secret-token"
    ).hexdigest()


def test_query_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "observability_log_dir", str(tmp_path))
    for i in range(3):
        tracer = BusinessTracer.start(f"s{i}", "u1", f"消息{i}")
        time.sleep(0.001)
        tracer.set_response(
            reply="ok", requires_human=False, follow_up_question=None,
            intent="order_inquiry", execution="workflow", confidence=0.95,
        )
        tracer.finish()
    traces = load_traces(str(tmp_path))
    assert len(traces) == 3
    summary = summarize(traces)
    assert summary["n"] == 3
    assert summary["intents"] == {"order_inquiry": 3}
    assert summary["executions"] == {"workflow": 3}
    assert summary["duration"]["avg_ms"] > 0
