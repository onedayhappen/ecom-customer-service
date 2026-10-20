from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4

import pytest

from app.memory.provenance import DerivedData
from app.session.short_term import ContextService, process_context_jobs
from app.support.store import SupportError, SupportStore
from app.tasks.correction import CorrectionService
from app.tasks.provenance import ProvenanceGuard
from app.tasks.repository import TaskRepository, VersionConflict
from tests.test_message_association import batch_for, new, proposal, service, store, update
from tests.test_task_candidates import task
from tests.test_task_storage import empty_store, message


def mistaken(store, *, create=False):
    target, _ = task(store, "old", details={"order_id": "ORD-A", "item_name": "耳机"})
    wrong, _ = task(store, "s", details={"order_id": "ORD-B", "reason": "正常", "item_name": "耳机"})
    batch = batch_for(store, "左耳没声音")
    agent = service(store)
    plan = proposal(new(batch, details={"reason": "左耳没声音"}) if create else
                    update(batch, wrong, details={"reason": "左耳没声音"}))
    decision = agent.commit(batch, agent.snapshot(batch), plan)["decisions"][0]
    return agent.repo.get(decision["task_id"]), target, decision


def request(store, wrong, target, decision, *, correction_id=None, reason="user_correction"):
    source = message(store, "s", "alice", "刚才说的是 ORD-A，不是 ORD-B")
    return dict(correction_id=correction_id or str(uuid4()), decision_id=decision["decision_id"],
                source=source, reason=reason, target_task_id=target.task_id if target else None,
                expected_versions={wrong.task_id: wrong.version, **({target.task_id: target.version} if target else {})})


def public_reply(store, record, did):
    with store.transaction() as tx:
        c = store._control(tx, "s", "alice")
        return store._event(tx, c, "message", "bot", {
            "role": "assistant", "content": "ORD-B 的问题是左耳没声音",
            "task_id": record.task_id, "task_version": record.version, "decision_id": did})


def test_only_mistaken_fields_are_restored_and_sources_move(store):
    wrong, target, decision = mistaken(store)
    source = message(store, "s", "alice", "ORD-B 退两件")
    repo = TaskRepository(store, "alice")
    wrong = repo.update(wrong.task_id, "s", expected_version=wrong.version, change_id="later",
                        source=source, patch={"details": {**wrong.details, "quantity": 2}})
    result = CorrectionService(store, "alice").correct("s", request(store, wrong, target, decision))
    assert result["restored_fields"] == ["details.reason"]
    assert repo.get(wrong.task_id).details == {**wrong.details, "reason": "正常"}
    assert repo.get(target.task_id).details["reason"] == "左耳没声音"
    with store.transaction(read_only=True) as tx:
        original = tx.one("SELECT session_id,sequence FROM association_sources WHERE decision_id=?", (decision["decision_id"],))
        assert not tx.one("SELECT * FROM support_task_messages WHERE task_id=? AND session_id=? AND sequence=?",
                          (wrong.task_id, original["session_id"], original["sequence"]))
        assert tx.one("SELECT * FROM support_task_messages WHERE task_id=? AND session_id=? AND sequence=?",
                      (target.task_id, original["session_id"], original["sequence"]))
        assert tx.one("SELECT COUNT(*) AS n FROM task_corrections")["n"] == 1
    assert any("左耳没声音" in m["content"] for m in store.public_history("s", "alice"))


@pytest.mark.parametrize("later_reason", ["右耳没声音", "左耳没声音"])
def test_later_correct_edit_or_reaffirmation_is_preserved(store, later_reason):
    wrong, target, decision = mistaken(store)
    batch = batch_for(store, "ORD-B " + later_reason)
    agent = service(store)
    agent.commit(batch, agent.snapshot(batch), proposal(update(batch, wrong, details={"reason": later_reason})))
    wrong = agent.repo.get(wrong.task_id)
    result = CorrectionService(store, "alice").correct("s", request(store, wrong, target, decision))
    assert "details.reason" in result["preserved_fields"]
    assert agent.repo.get(wrong.task_id).details["reason"] == later_reason
    assert agent.repo.get(target.task_id).details["reason"] == "左耳没声音"


def test_old_execution_confirmation_reply_and_cache_stamps_expire(store):
    wrong, target, decision = mistaken(store)
    other, _ = task(store, "old2", goal="鞋子物流", details={"order_id": "ORD-C"})
    guard = ProvenanceGuard(store, "alice")
    stamps = [guard.capture([wrong.task_id], [decision["decision_id"]]) for _ in ("execution", "confirmation", "reply")]
    unrelated = guard.capture([other.task_id])
    cache = DerivedData(store, "alice")
    stamp = cache.begin([wrong.task_id], [decision["decision_id"]])
    envelope = cache.finish(stamp, {"summary": "wrong"})
    user_cache = cache.finish(cache.begin(), {"memory": "old unscoped result"})
    CorrectionService(store, "alice").correct("s", request(store, wrong, target, decision))
    for value in stamps:
        assert not guard.valid(value)
        with store.transaction() as tx, pytest.raises(SupportError):
            guard.require_current(value, tx=tx, lock=True)
    assert guard.valid(unrelated)
    assert cache.read(envelope) is None and cache.read(user_cache) is None
    with pytest.raises(SupportError):
        cache.finish(stamp, {"summary": "late output"})
    with pytest.raises(SupportError):
        cache.begin([wrong.task_id], [decision["decision_id"]])
    fresh = cache.finish(cache.begin([target.task_id]), {"summary": "correct"})
    assert cache.read(fresh) == {"summary": "correct"}
    assert DerivedData(store, "bob").read(fresh) is None


def test_duplicate_correction_returns_receipt_and_cannot_change_content(store):
    wrong, target, decision = mistaken(store)
    req = request(store, wrong, target, decision)
    corrections = CorrectionService(store, "alice")
    result = corrections.correct("s", req)
    assert corrections.correct("s", req) == result
    with pytest.raises(SupportError):
        corrections.correct("s", {**req, "reason": "validation_failure"})
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM task_corrections")["n"] == 1
        assert tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE kind='task_invalidation'")["n"] == 1


def test_concurrent_correction_is_exactly_once(store):
    wrong, target, decision = mistaken(store)
    req = request(store, wrong, target, decision)
    def run(_):
        return CorrectionService(SupportStore(store.dsn), "alice").correct("s", req)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0] == results[1]
    assert TaskRepository(store, "alice").get(wrong.task_id).version == wrong.version + 1


def test_version_conflict_and_transaction_failure_leave_no_partial_correction(store, monkeypatch):
    wrong, target, decision = mistaken(store)
    req = request(store, wrong, target, decision)
    with pytest.raises(VersionConflict):
        CorrectionService(store, "alice").correct("s", {**req, "expected_versions": {wrong.task_id: 999, target.task_id: 1}})
    original = store._event
    def fail(tx, control, kind, actor, payload, **kwargs):
        if kind == "association_correction":
            raise RuntimeError("injected event failure")
        return original(tx, control, kind, actor, payload, **kwargs)
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        CorrectionService(store, "alice").correct("s", req)
    assert TaskRepository(store, "alice").get(wrong.task_id) == wrong
    assert TaskRepository(store, "alice").get(target.task_id) == target
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM task_invalid_sources")["n"] == 0
        assert tx.one("SELECT COUNT(*) AS n FROM task_corrections")["n"] == 0


def test_unresolved_target_can_be_selected_later_and_new_decision_can_be_corrected_again(store):
    wrong, target, decision = mistaken(store)
    corrections = CorrectionService(store, "alice")
    first = corrections.correct("s", request(store, wrong, None, decision))
    assert first["needs_clarification"] and first["prompt"]
    wrong = corrections.repo.get(wrong.task_id)
    assert wrong.status == "paused" and wrong.details["reason"] == "正常"
    second = corrections.correct("s", request(store, wrong, target, decision))
    assert second["new_decision_id"]
    third, _ = task(store, "old2", details={"order_id": "ORD-C"})
    new_source = message(store, "s", "alice", "再更正，是 ORD-C")
    target = corrections.repo.get(target.task_id)
    third_result = corrections.correct("s", dict(correction_id="again", decision_id=second["new_decision_id"],
        source=new_source, reason="user_correction", target_task_id=third.task_id,
        expected_versions={target.task_id: target.version, third.task_id: third.version}))
    assert third_result["new_decision_id"]
    assert "reason" not in corrections.repo.get(target.task_id).details
    assert corrections.repo.get(third.task_id).details["reason"] == "左耳没声音"


def test_wrong_empty_created_task_is_closed_not_deleted(store):
    wrong, target, decision = mistaken(store, create=True)
    result = CorrectionService(store, "alice").correct("s", request(store, wrong, target, decision))
    current = TaskRepository(store, "alice").get(wrong.task_id)
    assert current.status == "cancelled" and current.details == {}
    assert len(TaskRepository(store, "alice").history(wrong.task_id)) == 2
    assert result["target_task_id"] == target.task_id


@pytest.mark.parametrize("status", ["running", "succeeded", "unknown"])
def test_started_business_is_preserved_and_handed_to_human(store, status):
    wrong, target, decision = mistaken(store)
    public_reply(store, wrong, decision["decision_id"])
    with store.transaction() as tx:
        tx.execute("""INSERT INTO support_operations
            (operation_id,session_id,control_version,tool,status,created_at,result)
            VALUES ('op','s',0,'apply_refund',?,'2026-01-01','{"reference":"business-receipt"}')""", (status,))
    result = CorrectionService(store, "alice").correct("s", request(store, wrong, target, decision))
    assert result["operations_to_verify"] == [{"operation_id": "op", "status": status}]
    assert store.state("s", "alice")["mode"] == "handoff_pending"
    assert all(TaskRepository(store, "alice").get(tid).status == "paused" for tid in (wrong.task_id, target.task_id))
    with store.transaction(read_only=True) as tx:
        op = tx.one("SELECT status,result FROM support_operations WHERE operation_id='op'")
        assert op["status"] == status and "business-receipt" in op["result"]
        summary = tx.one("SELECT summary FROM support_tickets")["summary"]
        assert wrong.task_id in summary and '"operation_id": "op"' in summary
    assert "不能确认已撤回" in store.public_history("s", "alice")[-1]["content"]


def test_known_unrelated_operation_does_not_block_correction(store):
    wrong, target, decision = mistaken(store)
    other, _ = task(store, "s", goal="鞋子退货", details={"order_id": "ORD-C"})
    with store.transaction() as tx:
        tx.execute("""INSERT INTO support_operations
            (operation_id,session_id,control_version,tool,status,created_at) VALUES ('op','s',0,'refund','succeeded','2026')""")
        tx.execute("INSERT INTO support_operation_tasks VALUES ('op',?)", (other.task_id,))
    result = CorrectionService(store, "alice").correct("s", request(store, wrong, target, decision))
    assert result["operations_to_verify"] == []
    assert store.state("s", "alice")["mode"] == "bot"


def test_conflicting_target_value_is_not_overwritten(store):
    wrong, target, decision = mistaken(store)
    target = TaskRepository(store, "alice").update(target.task_id, "old", expected_version=1, change_id="target-reason",
        source=message(store, "old", "alice", "右耳没声音"), patch={"details": {**target.details, "reason": "右耳没声音"}})
    result = CorrectionService(store, "alice").correct("s", request(store, wrong, target, decision))
    assert result["needs_clarification"] and result["conflicts"] == ["details.reason"]
    assert TaskRepository(store, "alice").get(target.task_id).details["reason"] == "右耳没声音"


def test_public_wrong_reply_is_removed_from_context_but_kept_in_transcript(store):
    wrong, target, decision = mistaken(store)
    public_reply(store, wrong, decision["decision_id"])
    req = request(store, wrong, target, decision)
    result = CorrectionService(store, "alice").correct("s", req)
    assert result["corrected_public_reply"]
    history = store.public_history("s", "alice")
    assert any(m["content"] == "ORD-B 的问题是左耳没声音" for m in history)
    context = ContextService(store).build("s", "alice", task_id=wrong.task_id)
    assert "ORD-B 的问题是左耳没声音" not in json.dumps(context, ensure_ascii=False)
    batch = batch_for(store, "接下来呢")
    assert "ORD-B 的问题是左耳没声音" not in json.dumps(service(store).snapshot(batch).context.data(), ensure_ascii=False)
    assert sum("已修正相关记录" in m["content"] for m in history) == 1


def test_summary_worker_started_before_correction_cannot_reinsert_old_snapshot(store, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "short_term_memory_enabled", True)
    monkeypatch.setattr(settings, "short_term_recent_turns", 1)
    wrong, target, decision = mistaken(store)
    public_reply(store, wrong, decision["decision_id"])
    for i in range(5):
        message(store, "s", "alice", f"补充无关消息{i}")
    req = request(store, wrong, target, decision)
    with store.transaction() as tx:
        tx.execute("DELETE FROM support_context_jobs WHERE session_id<>'s' OR scope_key<>'session'")
    def summarize(previous, messages):
        CorrectionService(store, "alice").correct("s", req)
        first = messages[0]
        return {"items": [{"sequence": first["sequence"], "role": first["role"],
                           "kind": "user_request", "quote": first["content"]}]}
    counts = process_context_jobs(store, summarize_fn=summarize, limit=1)
    assert counts["stale"] == 1 and counts["failed"] == 0
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM support_context_snapshots")["n"] == 0
        assert tx.one("SELECT COUNT(*) AS n FROM support_context_jobs")["n"] > 0
    def rebuild(previous, messages):
        assert all(m["content"] != "ORD-B 的问题是左耳没声音" for m in messages)
        first = next(m for m in messages if m["role"] == "user")
        return {"items": [{"sequence": first["sequence"], "role": "user",
                           "kind": "user_request", "quote": first["content"]}]}
    rebuilt = process_context_jobs(store, summarize_fn=rebuild)
    assert rebuilt["failed"] == 0 and rebuilt["processed"] > 0
    with store.transaction(read_only=True) as tx:
        assert all("ORD-B 的问题是左耳没声音" not in r["payload"]
                   for r in tx.all("SELECT payload FROM support_context_snapshots"))


def test_foreign_target_and_negated_selection_are_rejected(store):
    wrong, target, decision = mistaken(store)
    other, _ = task(store, "other", "bob", details={"order_id": "ORD-A"})
    with pytest.raises(SupportError):
        CorrectionService(store, "alice").correct("s", request(store, wrong, other, decision))
    req = request(store, wrong, target, decision)
    req["source"] = message(store, "s", "alice", "不是 ORD-A，先别改")
    with pytest.raises(SupportError, match="尚未明确"):
        CorrectionService(store, "alice").correct("s", req)
    assert TaskRepository(store, "alice").get(wrong.task_id) == wrong


@pytest.mark.parametrize("reason", ["tool_conflict", "validation_failure"])
def test_server_conflict_entries_require_existing_evidence(store, reason):
    wrong, target, decision = mistaken(store)
    req = request(store, wrong, target, decision, reason=reason)
    if reason == "tool_conflict":
        req["source"] = {"kind": "tool_result", "session_id": "s", "event_id": "tool-check"}
        with pytest.raises(SupportError):
            CorrectionService(store, "alice").correct("s", req)
        with store.transaction() as tx:
            tx.execute("INSERT INTO case_snapshots VALUES (?,?)", ("alice", json.dumps({
                "events": [{"event_id": "tool-check", "event_type": "tool_result", "user_id": "alice",
                            "session_id": "s", "payload": {"actual_order": "ORD-A"}}]})))
    result = CorrectionService(store, "alice").correct("s", req)
    assert not result["needs_clarification"]
    assert TaskRepository(store, "alice").get(target.task_id).details["reason"] == "左耳没声音"


def test_channel_scope_and_partial_order_number_cannot_select_wrong_target(store):
    from app.tasks.candidates import BusinessScope
    wrong, target, decision = mistaken(store)
    req = request(store, wrong, target, decision)
    with pytest.raises(SupportError):
        CorrectionService(store, "alice", scope=BusinessScope(store_id="restricted-shop")).correct("s", req)
    req["source"] = message(store, "s", "alice", "刚才说的是 ORD-AB")
    with pytest.raises(SupportError, match="尚未明确"):
        CorrectionService(store, "alice").correct("s", req)
