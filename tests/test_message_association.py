import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.association import AssociationProposal
from app.support.store import SupportError, SupportStore
from app.tasks.association import AssociationRejected, AssociationService
from app.tasks.intake import MessageIntake
from app.tasks.repository import TaskRepository, VersionConflict
from tests.test_task_storage import empty_store
from tests.test_task_candidates import task, ask


@pytest.fixture
def store(empty_store):
    for sid, user in (("s", "alice"), ("old", "alice"), ("old2", "alice"), ("other", "bob")):
        empty_store.ensure(sid, user)
    return empty_store


def batch_for(store, *texts, sid="s"):
    intake = MessageIntake(store)
    for text in texts:
        intake.receive(sid, "alice", request_id=str(uuid4()), content=text)
    return intake.claim(sid, "alice")


def quote(batch, index=0, text=None):
    message = batch.messages[index]
    return dict(session_id=batch.session_id, sequence=message["sequence"], quote=text or message["content"])


def new(batch, goal="耳机退款", *, index=0, details=None, text=None):
    q = quote(batch, index, text)
    return dict(action="create", effect="new", goal=goal, sequences=[q["sequence"]], evidence=[q],
                details=[dict(field=k, value=v, evidence=q) for k, v in (details or {}).items()])


def update(batch, record, *, effect="supplement", details=None, question_id=None, index=0):
    q = quote(batch, index)
    return dict(action="update", effect=effect, task_id=record.task_id, task_version=record.version,
                sequences=[q["sequence"]], evidence=[q], question_id=question_id,
                details=[dict(field=k, value=v, evidence=q) for k, v in (details or {}).items()])


def proposal(*decisions):
    return AssociationProposal.model_validate({"decisions": list(decisions)})


def service(store):
    value = AssociationService(store, "alice")
    value.candidates.order_lookup = lambda **kwargs: {"success": True, "orders": []}
    return value


def test_one_message_two_tasks_and_exact_retry(store):
    batch = batch_for(store, "耳机退款，鞋子查物流")
    engine = service(store)
    plan = proposal(new(batch, details={"item_name": "耳机"}, text="耳机退款"),
                    new(batch, "鞋子查物流", details={"item_name": "鞋子"}, text="鞋子查物流"))
    snapshot = engine.snapshot(batch)
    result = engine.commit(batch, snapshot, plan)
    assert len(result["decisions"]) == 2 and result["clarification"] is None
    assert len(TaskRepository(store, "alice").list()) == 2
    assert engine.commit(batch, snapshot, plan) == result
    assert engine.process(batch, SimpleNamespace(associate=lambda _: pytest.fail("replay called model"))) == result
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM association_sources")["n"] == 2
        assert tx.one("SELECT COUNT(*) AS n FROM support_task_messages")["n"] == 2
    with pytest.raises(AssociationRejected):
        engine.commit(batch, snapshot, proposal(new(batch, "不同目标")))


def test_answer_uses_valid_question_and_preserves_other_details(store):
    record, source = task(store, "s", details={"order_id": "ORD-A", "item_name": "耳机", "quantity": 1})
    record = ask(store, record, source)
    batch = batch_for(store, "左耳没声音")
    engine = service(store)
    plan = proposal(update(batch, record, effect="answer", details={"reason": "左耳没声音"},
                           question_id=record.waiting_for["question_id"]))
    result = engine.commit(batch, engine.snapshot(batch), plan)
    current = engine.repo.get(record.task_id)
    assert current.details == {**record.details, "reason": "左耳没声音"}
    assert current.waiting_for is None and current.status == "in_progress"
    assert result["decisions"][0]["task_id"] == record.task_id


def test_two_questions_yes_is_clarified_without_field_changes(store):
    first, source1 = task(store, "s")
    second, source2 = task(store, "s", goal="鞋子退货", details={"order_id": "ORD-B"})
    first, second = ask(store, first, source1), ask(store, second, source2)
    batch = batch_for(store, "是的")
    engine = service(store)
    invalid = proposal(update(batch, first, effect="answer", question_id=first.waiting_for["question_id"]))
    result = engine.process(batch, SimpleNamespace(associate=lambda _: invalid))
    assert result["clarification"]
    assert result["rejections"] == ["ambiguous_short_answer"] * 2
    assert engine.repo.get(first.task_id) == first and engine.repo.get(second.task_id) == second


def test_clarification_answer_relinks_original_and_answer(store):
    first, _ = task(store, "s", details={"order_id": "ORD-A"})
    second, _ = task(store, "s", details={"order_id": "ORD-B"})
    batch = batch_for(store, "左耳没声音")
    engine = service(store)
    unclear = proposal(dict(action="clarify", sequences=[batch.messages[0]["sequence"]], evidence=[quote(batch)],
                            clarification="哪笔订单？第一笔 ORD-A，第二笔 ORD-B。",
                            candidate_task_ids=[first.task_id, second.task_id]))
    initial = engine.commit(batch, engine.snapshot(batch), unclear)
    question = initial["clarification"]
    with store.transaction() as tx:
        c = store._control(tx, "s", "alice")
        store._event(tx, c, "message", "bot", {"role": "assistant", "content": question["prompt"],
                     "clarification_id": question["clarification_id"], "clarification_version": question["version"]})
    answer = batch_for(store, "第二个")
    decision = update(answer, second, effect="answer")
    decision.update(clarification_id=question["clarification_id"],
                    sequences=[batch.messages[0]["sequence"], answer.messages[0]["sequence"]],
                    evidence=[quote(batch), quote(answer)],
                    details=[dict(field="reason", value="左耳没声音", evidence=quote(batch))])
    result = engine.commit(answer, engine.snapshot(answer), proposal(decision))
    assert result["decisions"][0]["task_id"] == second.task_id
    assert engine.repo.get(first.task_id) == first
    assert engine.repo.get(second.task_id).details["reason"] == "左耳没声音"
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT status FROM association_clarifications")["status"] == "answered"
        assert tx.one("SELECT COUNT(*) AS n FROM support_task_messages WHERE task_id=?", (second.task_id,))["n"] == 3


def test_cross_session_continuation_and_new_goal_are_distinct(store):
    old, _ = task(store, "old", goal="耳机退款", details={"order_id": "ORD-A"})
    batch = batch_for(store, "ORD-A 的退款继续核实", "ORD-A 另外开发票")
    engine = service(store)
    plan = proposal(update(batch, old), new(batch, "开发票", index=1, details={"order_id": "ORD-A"}))
    result = engine.commit(batch, engine.snapshot(batch), plan)
    assert result["decisions"][0]["task_id"] == old.task_id
    assert result["decisions"][1]["task_id"] != old.task_id
    assert len(engine.repo.list()) == 2


@pytest.mark.parametrize("violation", ["foreign_task", "forged_quote", "wrong_order", "ungrounded_field", "new_id", "short_without_question"])
def test_invalid_model_targets_and_sources_commit_nothing(store, violation):
    record, _ = task(store, "s")
    other, _ = task(store, "other", "bob")
    text = "是的" if violation == "short_without_question" else "ORD-A 左耳没声音"
    if violation == "wrong_order":
        text = "ORD-B 左耳没声音"
    batch = batch_for(store, text)
    decision = update(batch, record, details={"reason": "左耳没声音"} if "左耳" in text else {})
    if violation == "foreign_task":
        decision["task_id"] = other.task_id
    elif violation == "new_id":
        decision["task_id"] = str(uuid4())
    elif violation == "forged_quote":
        decision["evidence"][0]["quote"] = "用户从未说过的话"
    elif violation == "ungrounded_field":
        decision["details"][0]["value"] = "屏幕坏了"
    engine = service(store)
    with pytest.raises(AssociationRejected):
        engine.commit(batch, engine.snapshot(batch), proposal(decision))
    assert engine.repo.get(record.task_id) == record
    assert engine.intake.progress("s", "alice")["associated_sequence"] == 0


def test_stale_version_rolls_back_all_new_tasks(store):
    record, source = task(store, "s")
    batch = batch_for(store, "ORD-A 左耳没声音", "鞋子查物流")
    engine = service(store)
    snapshot = engine.snapshot(batch)
    engine.repo.update(record.task_id, "s", expected_version=1, change_id="concurrent",
                       source=source, patch={"details": {**record.details, "quantity": 2}})
    with pytest.raises(VersionConflict):
        engine.commit(batch, snapshot, proposal(new(batch, "鞋子查物流", index=1), update(batch, record)))
    assert len(engine.repo.list()) == 1
    assert engine.intake.progress("s", "alice")["associated_sequence"] == 0


def test_cancel_during_model_fences_old_snapshot_and_cannot_resume_old_input(store):
    record, _ = task(store, "s")
    batch = batch_for(store, "ORD-A 继续退款")
    engine = service(store)
    snapshot = engine.snapshot(batch)
    engine.intake.receive("s", "alice", request_id="cancel", content="取消这个退款",
                          action="cancel", task_id=record.task_id, task_version=record.version)
    with pytest.raises(VersionConflict):
        engine.commit(batch, snapshot, proposal(update(batch, record)))
    cancelled = engine.repo.get(record.task_id)
    latest = engine.snapshot(batch)
    with pytest.raises(AssociationRejected, match="superseded_by_control"):
        engine.commit(batch, latest, proposal(update(batch, cancelled, effect="resume")))
    assert engine.repo.get(record.task_id).status == "cancelled"


def test_new_input_does_not_get_acknowledged_by_old_batch(store):
    batch = batch_for(store, "耳机退款")
    engine = service(store)
    snapshot = engine.snapshot(batch)
    sent = engine.intake.receive("s", "alice", request_id="later", content="鞋子查物流")
    engine.commit(batch, snapshot, proposal(new(batch)))
    state = engine.intake.progress("s", "alice")
    assert state["associated_sequence"] == batch.messages[0]["sequence"]
    assert state["received_sequence"] == sent["sequence"]
    assert engine.intake.claim_next().messages[0]["content"] == "鞋子查物流"


def test_event_write_failure_rolls_back_decisions_tasks_and_cursor(store, monkeypatch):
    batch = batch_for(store, "耳机退款")
    engine = service(store)
    snapshot = engine.snapshot(batch)
    original = store._event
    def fail(tx, c, kind, *args, **kwargs):
        result = original(tx, c, kind, *args, **kwargs)
        if kind == "association_decision":
            raise RuntimeError("injected event failure")
        return result
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        engine.commit(batch, snapshot, proposal(new(batch)))
    assert engine.repo.list() == []
    assert engine.intake.progress("s", "alice")["associated_sequence"] == 0
    monkeypatch.setattr(store, "_event", original)
    assert engine.commit(batch, snapshot, proposal(new(batch)))["decisions"][0]["task_id"]


def test_duplicate_workers_commit_one_decision_batch(store):
    batch = batch_for(store, "耳机退款")
    snapshot = service(store).snapshot(batch)
    plan = proposal(new(batch))
    def commit(_):
        return service(SupportStore(store.dsn)).commit(batch, snapshot, plan)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(commit, [1, 2]))
    assert results[0] == results[1]
    assert len(TaskRepository(store, "alice").list()) == 1


def test_malformed_output_retries_are_bounded_and_original_preserved(store):
    batch = batch_for(store, "耳机怎么退")
    calls = []
    def malformed(_):
        calls.append(1)
        return {"decisions": [], "reasoning": "not accepted"}
    result = service(store).process(batch, SimpleNamespace(associate=malformed))
    assert len(calls) == 2 and result["clarification"]
    assert TaskRepository(store, "alice").list() == []
    assert store.public_history("s", "alice")[-1]["content"] == "耳机怎么退"
    assert "reasoning" not in json.dumps(result)


@pytest.mark.parametrize("blocked", ["handoff", "unknown_operation"])
def test_human_or_unknown_business_state_blocks_task_update(store, blocked):
    record, _ = task(store, "old")
    batch = batch_for(store, "ORD-A 补充原因")
    engine = service(store)
    if blocked == "handoff":
        store.handoff("old", "alice")
    else:
        with store.transaction() as tx:
            tx.execute("""INSERT INTO support_operations
                (operation_id,session_id,control_version,tool,status,created_at)
                VALUES ('op','old',0,'refund','unknown','2026-01-01')""")
    with pytest.raises(AssociationRejected):
        engine.commit(batch, engine.snapshot(batch), proposal(update(batch, record)))
    assert engine.repo.get(record.task_id) == record


def test_expired_or_not_yet_published_question_cannot_take_answer(store):
    record, source = task(store, "s")
    record = ask(store, record, source, publish=False)
    batch = batch_for(store, "左耳没声音")
    engine = service(store)
    with pytest.raises(AssociationRejected, match="expired_question"):
        engine.commit(batch, engine.snapshot(batch), proposal(update(
            batch, record, effect="answer", question_id=record.waiting_for["question_id"])))


def test_negated_cancel_is_rejected_but_clear_stop_is_saved(store):
    record, _ = task(store, "s")
    engine = service(store)
    batch = batch_for(store, "不要取消 ORD-A 的退款")
    with pytest.raises(AssociationRejected, match="negated"):
        engine.commit(batch, engine.snapshot(batch), proposal(update(batch, record, effect="cancel")))
    # Consume this input conservatively without changing the matter.
    engine.commit(batch, engine.snapshot(batch), proposal(dict(
        action="ignore", sequences=[batch.messages[0]["sequence"]], evidence=[quote(batch)])))
    batch = batch_for(store, "ORD-A 不退款了")
    engine.commit(batch, engine.snapshot(batch), proposal(update(batch, record, effect="cancel")))
    assert engine.repo.get(record.task_id).status == "cancelled"


def test_order_number_is_not_quantity_evidence(store):
    batch = batch_for(store, "退 ORD-1")
    engine = service(store)
    with pytest.raises(AssociationRejected, match="ungrounded_quantity"):
        engine.commit(batch, engine.snapshot(batch), proposal(new(batch, details={"quantity": 1})))


def test_assistant_question_is_context_but_not_customer_field_evidence(store):
    record, source = task(store, "s")
    record = ask(store, record, source)
    batch = batch_for(store, "左耳没声音")
    engine = service(store)
    snapshot = engine.snapshot(batch)
    prompt = next(m for m in snapshot.context.recent_messages if m["content"] == "请说明原因")
    basis = dict(session_id="s", sequence=prompt["sequence"], quote=prompt["content"])
    decision = update(batch, record, effect="answer", details={"reason": "左耳没声音"},
                      question_id=record.waiting_for["question_id"])
    decision["evidence"].append(basis)
    invalid = proposal(decision).model_copy(deep=True)
    invalid.decisions[0].details[0].evidence = type(invalid.decisions[0].evidence[0])(**basis)
    invalid.decisions[0].details[0].value = "说明原因"
    with pytest.raises(AssociationRejected, match="source_not_user_statement"):
        engine.commit(batch, snapshot, invalid)
    engine.commit(batch, snapshot, proposal(decision))
    assert engine.repo.get(record.task_id).details["reason"] == "左耳没声音"


def test_pause_and_resume_keep_pending_work_but_do_not_reuse_old_question(store):
    record, source = task(store, "s")
    record = ask(store, record, source)
    engine = service(store)
    batch = batch_for(store, "暂停 ORD-A 的退款")
    engine.commit(batch, engine.snapshot(batch), proposal(update(batch, record, effect="pause")))
    paused = engine.repo.get(record.task_id)
    assert paused.status == "paused" and paused.waiting_for == record.waiting_for
    batch = batch_for(store, "继续 ORD-A 的退款")
    engine.commit(batch, engine.snapshot(batch), proposal(update(batch, paused, effect="resume")))
    resumed = engine.repo.get(record.task_id)
    assert resumed.status == "in_progress" and resumed.waiting_for == record.waiting_for
    batch = batch_for(store, "左耳没声音")
    with pytest.raises(AssociationRejected, match="expired_question"):
        engine.commit(batch, engine.snapshot(batch), proposal(update(
            batch, resumed, effect="answer", question_id=record.waiting_for["question_id"])))
