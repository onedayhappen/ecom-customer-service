"""Held-out business receipts at the customer publication boundary.

Twelve archived receipts exclude the original phone/pan cases; six contract
fixtures cover less frequent operations. No live model or business writes.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.config.settings import settings
from app.support.store import SupportStore
from app.tasks.replies import ReplyCoordinator
from tests.test_reply_grounding import save_receipts
from tests.test_task_candidates import task


CASES = json.loads((Path(__file__).parent / "fixtures/customer_reply_variety.json").read_text(encoding="utf8"))["cases"]
FORBIDDEN = ("sku_id", "spec_values", "catalog_model_key", "source_example_unverified",
             "scope_id", "membership_id", "subscription_id", "record_type", "INTERNAL_",
             "商品编号", "规格编码", "明细 /", "字段 /", "image_url", "planned")


def publish_case(path, case, mode="no_model", receipts=None):
    store = SupportStore("sqlite:///" + path.as_posix())
    store.ensure("s", "alice")
    record, source = task(store, "s", goal=case["user"], details={"item_name": "本次咨询商品"})
    evidence = deepcopy(receipts if receipts is not None else case["receipts"])
    _, ids = save_receipts(store, record, source, evidence)
    calls = []

    def compose(context):
        calls.append(context)
        if mode == "invalid_model":
            return "sku_id: INTERNAL_MODEL_OUTPUT; spec_values: {}"
        return json.dumps({"claim_ids": [c["claim_id"] for m in context["matters"] for c in m["claims"]]})

    coordinator = ReplyCoordinator(store, "alice", composer=None if mode == "no_model" else compose)
    reply = coordinator.publish("s")
    assert reply and sorted(reply["evidence_ids"]) == sorted(ids)
    assert len(calls) == (0 if mode == "no_model" else 1)
    assert coordinator.publish("s") is None
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM support_operations")["n"] == 0
        saved = tx.all("SELECT payload FROM support_events WHERE kind='task_tool_result'")
        assert [json.loads(r["payload"])["result"] for r in saved] == [r["result"] for r in evidence]
    return reply["content"]


def errors(case, text):
    return (["缺少事实：" + v for v in case["must"] if v not in text]
            + ["泄漏字段：" + v for v in FORBIDDEN if v in text]
            + (["回复超过1800字符"] if len(text) > 1800 else []))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
@pytest.mark.parametrize("mode", ["selected", "invalid_model", "no_model"])
def test_varied_final_replies(tmp_path, monkeypatch, case, mode):
    monkeypatch.setattr(settings, "memory_enabled", False)
    monkeypatch.setattr(settings, "short_term_memory_enabled", False)
    text = publish_case(tmp_path / "support.db", case, mode)
    assert not errors(case, text), (errors(case, text), text)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_irrelevant_nested_metadata_does_not_change_reply(tmp_path, monkeypatch, case):
    monkeypatch.setattr(settings, "memory_enabled", False)
    monkeypatch.setattr(settings, "short_term_memory_enabled", False)
    original = publish_case(tmp_path / "before.db", case)

    def inject(value):
        if isinstance(value, list):
            return [inject(v) for v in value]
        if isinstance(value, dict):
            # New nested keys and field order must not become customer prose.
            return {"debug_metadata": {"trace_id": "INTERNAL_TRACE", "items": ["INTERNAL_SECRET"]},
                    **{k: inject(v) for k, v in reversed(list(value.items()))}}
        return value

    changed = deepcopy(case["receipts"])
    for receipt in changed:
        receipt["result"]["data"] = inject(receipt["result"]["data"])
    assert publish_case(tmp_path / "after.db", case, receipts=changed) == original
