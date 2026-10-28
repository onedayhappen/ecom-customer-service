"""Exercise the real publisher/association boundary, including legacy menus."""
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.tasks.association import AssociationRejected, AssociationService
from app.tasks.replies import ReplyCoordinator
from app.tasks.repository import TaskRepository, VersionConflict
from app.support.store import SupportStore
from tests.test_coordinated_replies import ready
from tests.test_message_association import batch_for, proposal, quote, store, update
from tests.test_task_candidates import task
from tests.test_task_storage import empty_store


def clarify(store, records, *, sid="s", text="想要 L 码"):
    batch = batch_for(store, text, sid=sid)
    svc = AssociationService(store, "alice")
    result = svc.commit(batch, svc.snapshot(batch), proposal(dict(
        action="clarify", sequences=[m["sequence"] for m in batch.messages], evidence=[quote(batch)],
        clarification="您说的是哪一件？", candidate_task_ids=[r.task_id for r in records])))
    return batch, result["clarification"]


def answer_plan(original, answer, record, cid, **kwargs):
    decision = update(answer, record, effect="answer", **kwargs)
    decision.update(clarification_id=cid,
                    sequences=[original.messages[0]["sequence"], answer.messages[0]["sequence"]],
                    evidence=[quote(original), quote(answer)])
    return proposal(decision)


def question_row(store, cid):
    with store.transaction(read_only=True) as tx:
        return tx.one("SELECT * FROM association_clarifications WHERE clarification_id=?", (cid,))


def input_status(store, batch):
    with store.transaction(read_only=True) as tx:
        return tx.one("SELECT association_status FROM message_intake_items WHERE session_id=? AND sequence=?",
                      (batch.session_id, batch.messages[0]["sequence"]))["association_status"]


def change(store, record, source):
    return TaskRepository(store, "alice").update(record.task_id, source["session_id"],
        expected_version=record.version, change_id=str(uuid4()), source=source,
        patch={"goal": record.goal + "补充信息"})


@pytest.mark.parametrize("composed", [False, True])
def test_actual_publication_round_trip_keeps_nondefault_version_and_original_input(store, composed):
    first, _ = task(store, "s", goal="红米手机查询", details={"item_name": "红米手机"})
    second, _ = task(store, "s", goal="衣服选购", details={"item_name": "衣服"})
    original, question = clarify(store, [first, second])
    cid = question["clarification_id"]
    with store.transaction() as tx:
        tx.execute("UPDATE association_clarifications SET version=7 WHERE clarification_id=?", (cid,))
    composer = (lambda _: pytest.fail("fixed menu must not call the model")) if composed else None
    coordinator = ReplyCoordinator(store, "alice", composer=composer)
    sent = coordinator.publish("s")
    assert sent["clarification_version"] == 7
    assert "1. 红米手机查询；2. 衣服选购" in sent["content"]
    assert coordinator.publish("s") is None
    answer = batch_for(store, "第二个")
    svc = AssociationService(store, "alice")
    snapshot = svc.snapshot(answer)
    assert snapshot.context.clarifications[0]["valid"]
    decision = answer_plan(original, answer, second, cid).model_dump()
    decision["decisions"][0]["details"] = [dict(field="conditions", value="L 码", evidence=quote(original))]
    result = svc.commit(answer, snapshot, decision)
    assert svc.commit(answer, snapshot, decision) == result
    assert question_row(store, cid)["status"] == "answered"
    assert input_status(store, original) == input_status(store, answer) == "associated"
    assert svc.repo.get(first.task_id) == first
    assert svc.repo.get(second.task_id).details["conditions"] == "L 码"


@pytest.mark.parametrize("fault", ["missing_version", "wrong_version", "candidate_changed"])
def test_invalid_menu_replaced_once_with_sources_and_current_versions(store, fault):
    first, source = task(store, "s")
    second, _ = task(store, "s", details={"order_id": "ORD-B"})
    original, question = clarify(store, [first, second])
    coordinator = ReplyCoordinator(store, "alice")
    sent = coordinator.publish("s")
    cid = question["clarification_id"]
    if fault == "candidate_changed":
        first = change(store, first, source)
    else:
        # Simulate persisted public events from the buggy release.
        with store.transaction() as tx:
            row = tx.one("SELECT payload FROM support_events WHERE session_id='s' AND sequence=?", (sent["sequence"],))
            payload = json.loads(row["payload"])
            payload.pop("clarification_version")
            if fault == "wrong_version":
                payload["clarification_version"] = 99
            tx.execute("UPDATE support_events SET payload=? WHERE session_id='s' AND sequence=?",
                       (json.dumps(payload, ensure_ascii=False), sent["sequence"]))
    replacement = coordinator.publish("s")
    new_id = replacement["clarification_id"]
    assert new_id != cid and replacement["clarification_version"] == 1
    assert replacement["task_versions"] == {first.task_id: first.version, second.task_id: second.version}
    assert coordinator.publish("s") is None
    assert question_row(store, cid)["status"] == "superseded"
    assert question_row(store, new_id)["sources"] == question_row(store, cid)["sources"]
    assert input_status(store, original) == "clarification"
    answer = batch_for(store, "第一个")
    svc = AssociationService(store, "alice")
    snapshot = svc.snapshot(answer)
    assert [c["clarification_id"] for c in snapshot.context.clarifications] == [new_id]
    assert snapshot.context.clarifications[0]["valid"]
    svc.commit(answer, snapshot, answer_plan(original, answer, first, new_id))
    assert input_status(store, original) == "associated"


def test_unselected_candidate_changed_after_snapshot_rejects_answer_atomically(store):
    first, _ = task(store, "s")
    second, source = task(store, "s", details={"order_id": "ORD-B"})
    original, question = clarify(store, [first, second])
    ReplyCoordinator(store, "alice").publish("s")
    answer = batch_for(store, "第一个")
    svc = AssociationService(store, "alice")
    snapshot = svc.snapshot(answer)
    assert snapshot.context.clarifications[0]["valid"]
    change(store, second, source)
    with pytest.raises(VersionConflict):
        svc.commit(answer, snapshot, answer_plan(original, answer, first, question["clarification_id"]))
    assert svc.repo.get(first.task_id) == first
    assert question_row(store, question["clarification_id"])["status"] == "open"
    assert input_status(store, original) == "clarification"
    assert input_status(store, answer) == "pending"


def test_stale_choice_is_rejected_but_explicit_current_task_supplement_survives(store):
    first, source = task(store, "s", details={"item_name": "衣服"})
    original, question = clarify(store, [first])
    coordinator = ReplyCoordinator(store, "alice")
    coordinator.publish("s")
    first = change(store, first, source)
    answer = batch_for(store, "衣服改成黑色")
    svc = AssociationService(store, "alice")
    snapshot = svc.snapshot(answer)
    with pytest.raises(AssociationRejected, match="expired_clarification"):
        svc.commit(answer, snapshot, answer_plan(original, answer, first, question["clarification_id"]))
    result = svc.commit(answer, snapshot, proposal(update(answer, first, details={"conditions": "黑色"})))
    assert result["decisions"][0]["action"] == "update"
    assert svc.repo.get(first.task_id).details["conditions"] == "黑色"
    replacement = coordinator.publish("s")
    assert replacement["clarification_id"] != question["clarification_id"]
    # The earlier L-size request is still awaiting association, not silently lost.
    assert input_status(store, original) == "clarification"
    assert json.loads(question_row(store, replacement["clarification_id"])["sources"]) == [original.messages[0]["sequence"]]


@pytest.mark.parametrize("empty_options", [False, True])
def test_other_session_invalid_or_unscoped_menu_does_not_block_ready_result(store, empty_options):
    record, source = task(store, "s")
    _, question = clarify(store, [] if empty_options else [record], sid="old")
    ReplyCoordinator(store, "alice").publish("old")
    if not empty_options:
        record = change(store, record, source)
    jid = ready(store, record, source)
    reply = ReplyCoordinator(store, "alice").publish("s")
    assert reply["result_ids"] == ["job:" + jid]
    assert "ORD-A" in reply["content"]
    assert question_row(store, question["clarification_id"])["status"] == "open"


def test_answer_failure_rolls_back_task_question_and_original_intake_together(store, monkeypatch):
    record, _ = task(store, "s")
    original, question = clarify(store, [record])
    ReplyCoordinator(store, "alice").publish("s")
    answer = batch_for(store, "第一个")
    svc = AssociationService(store, "alice")
    snapshot = svc.snapshot(answer)
    plan = answer_plan(original, answer, record, question["clarification_id"])
    complete = svc.intake.complete
    def fail(*args, **kwargs):
        raise RuntimeError("intake commit failed")
    monkeypatch.setattr(svc.intake, "complete", fail)
    with pytest.raises(RuntimeError, match="intake commit failed"):
        svc.commit(answer, snapshot, plan)
    assert svc.repo.get(record.task_id) == record
    assert question_row(store, question["clarification_id"])["status"] == "open"
    assert input_status(store, original) == "clarification"
    monkeypatch.setattr(svc.intake, "complete", complete)
    svc.commit(answer, snapshot, plan)
    assert question_row(store, question["clarification_id"])["status"] == "answered"
    assert input_status(store, original) == input_status(store, answer) == "associated"


def test_replacement_and_publication_rollback_together(store, monkeypatch):
    record, source = task(store, "s")
    _, question = clarify(store, [record])
    coordinator = ReplyCoordinator(store, "alice")
    coordinator.publish("s")
    change(store, record, source)
    publish = coordinator._publish
    def fail(*args, **kwargs):
        raise RuntimeError("event write failed")
    monkeypatch.setattr(coordinator, "_publish", fail)
    with pytest.raises(RuntimeError, match="event write failed"):
        coordinator.publish("s")
    assert question_row(store, question["clarification_id"])["status"] == "open"
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM association_clarifications")["n"] == 1
    monkeypatch.setattr(coordinator, "_publish", publish)
    assert coordinator.publish("s")["clarification_id"] != question["clarification_id"]
    assert coordinator.publish("s") is None


def test_concurrent_publishers_replace_stale_menu_only_once(store):
    record, source = task(store, "s")
    _, question = clarify(store, [record])
    ReplyCoordinator(store, "alice").publish("s")
    change(store, record, source)
    def publish(_):
        return ReplyCoordinator(SupportStore(store.dsn), "alice").publish("s")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sum(r is not None for r in results) == 1
    with store.transaction(read_only=True) as tx:
        rows = tx.all("SELECT clarification_id,status FROM association_clarifications")
    assert len(rows) == 2
    assert next(r["status"] for r in rows if r["clarification_id"] == question["clarification_id"]) == "superseded"
    assert sum(r["status"] == "open" for r in rows) == 1


def test_invalid_menu_with_no_unresolved_input_retires_without_blocking(store):
    record, source = task(store, "s")
    original, question = clarify(store, [record])
    ReplyCoordinator(store, "alice").publish("s")
    record = change(store, record, source)
    # Simulate input already resolved by another recovery path.
    with store.transaction() as tx:
        tx.execute("UPDATE message_intake_items SET association_status='associated' WHERE session_id=? AND sequence=?",
                   (original.session_id, original.messages[0]["sequence"]))
    jid = ready(store, record, source)
    sent = ReplyCoordinator(store, "alice").publish("s")
    assert sent["result_ids"] == ["job:" + jid]
    assert "clarification_id" not in sent
    assert question_row(store, question["clarification_id"])["status"] == "superseded"


def test_unpublished_clarification_cannot_authorize_choice(store):
    record, _ = task(store, "s")
    original, question = clarify(store, [record])
    answer = batch_for(store, "第一个")
    svc = AssociationService(store, "alice")
    snapshot = svc.snapshot(answer)
    assert not snapshot.context.clarifications[0]["published"]
    with pytest.raises(AssociationRejected, match="expired_clarification"):
        svc.commit(answer, snapshot, answer_plan(original, answer, record, question["clarification_id"]))
    assert input_status(store, original) == "clarification"
    assert svc.repo.get(record.task_id) == record


def test_candidate_changed_after_clarify_snapshot_cannot_publish_stale_options(store):
    record, source = task(store, "s")
    batch = batch_for(store, "那个有问题")
    svc = AssociationService(store, "alice")
    snapshot = svc.snapshot(batch)
    change(store, record, source)
    plan = proposal(dict(action="clarify", sequences=[batch.messages[0]["sequence"]], evidence=[quote(batch)],
                         clarification="您说的是哪一件？", candidate_task_ids=[record.task_id]))
    with pytest.raises(VersionConflict):
        svc.commit(batch, snapshot, plan)
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM association_clarifications")["n"] == 0
