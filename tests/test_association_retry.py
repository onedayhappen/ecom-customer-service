"""Production-wired retry regressions and recorded evaluation replays."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tasks.association import AssociationService, AssociationRejected
from app.tasks.candidates import BusinessScope
from app.tasks.conversation_worker import ConversationWorker
from app.tasks.execution_models import TaskExecutionInput
from app.tasks.execution_store import TaskRuns
from app.tasks.replies import ReplyCoordinator
from tests.test_message_association import batch_for, new, proposal, store
from tests.test_task_candidates import task
from tests.test_task_execution import request
from tests.test_task_storage import empty_store


CASES = json.loads((Path(__file__).parent / "fixtures/association_retry_cases.json").read_text(encoding="utf-8"))["cases"]


def install_case(store, case):
    target = case["target"]
    existing = task(store, "s", goal=target["goal"], details=target["details"])[0] if target else None
    batch = batch_for(store, case["user"])
    attempts = deepcopy(case["attempts"])
    for attempt in attempts:
        for decision in (attempt["proposal"] or {}).get("decisions", []):
            decision["sequences"] = [batch.messages[0]["sequence"]]
            if decision["task_id"]:
                decision.update(task_id=existing.task_id, task_version=existing.version)
            quotes = decision["evidence"] + [d["evidence"] for d in decision["details"]]
            for quote in quotes:
                quote.update(session_id=batch.session_id, sequence=batch.messages[0]["sequence"])
    return batch, existing, attempts


def replay_case(store, case):
    batch, existing, attempts = install_case(store, case)
    contexts = []

    def associate(context):
        attempt = attempts[len(contexts)]
        contexts.append(deepcopy(context))
        if attempt["proposal"] is None:
            assert attempt["error"]["type"] == "TimeoutError"
            raise TimeoutError("recorded first-attempt timeout")
        return attempt["proposal"]

    # Run the real worker construction path; no order adapter injection.
    worker = SimpleNamespace(store=store, engine=SimpleNamespace(associate=associate))
    result = ConversationWorker._associate(worker, batch)
    expected_error = case["attempts"][0]["error"]
    if isinstance(expected_error, dict):
        expected_error = expected_error["code"]
    assert result["rejections"] == [expected_error]
    assert result["clarification"] is None
    assert [context.gaps for context in contexts] == [[], []]
    assert contexts[1].retry_feedback == [expected_error]
    assert len(contexts) == 2
    decision = result["decisions"][0]
    assert decision["action"] == ("update" if existing else "create")
    current = AssociationService(store, "alice").repo.get(decision["task_id"])
    if existing:
        assert current.task_id == existing.task_id and current.version == existing.version + 1
        assert current.details == existing.details  # Unsupported item_name was never persisted.
    else:
        assert "L码" in current.goal
    return batch, current, result, contexts


@pytest.mark.parametrize("case", CASES, ids=["s3-r2-redmi-field-evidence", "s7-r2-size-timeout"])
def test_real_cases_recover_without_commerce_dependency(store, case):
    from app.services.commerce.service import service_factory
    with service_factory(lambda *_: pytest.fail("retry must not query the commerce backend")):
        batch, current, result, contexts = replay_case(store, case)
    # Idempotent replay does not call the model or apply the update twice.
    assert AssociationService(store, "alice").process(batch, SimpleNamespace(
        associate=lambda _: pytest.fail("duplicate model call"))) == result
    assert AssociationService(store, "alice").repo.get(current.task_id).version == current.version


@pytest.mark.parametrize("case", CASES, ids=["redmi", "size"])
def test_recovered_association_is_not_itself_business_evidence(store, case):
    batch, current, _, _ = replay_case(store, case)
    runs = TaskRuns(store, "alice")
    source = dict(kind="message", session_id=batch.session_id, sequence=batch.messages[0]["sequence"])
    req = TaskExecutionInput.model_validate(request(current, source))
    reservation, _ = runs.begin(req)
    runs.finish(req, reservation["payload"], dict(state="done", task_version=current.version,
        reply="红米已经发货，L码已永久记入长期记忆。", evidence_ids=[]))
    output = ReplyCoordinator(store, "alice", composer=lambda _: pytest.fail(
        "no evidence: do not ask a model to invent business facts")).publish("s")
    assert output is not None
    assert "暂时没有可核实的查询结果" in output["content"]
    assert "红米已经发货" not in output["content"]
    assert "已永久记入长期记忆" not in output["content"]
    assert "您说的是哪一件" not in output["content"]


@pytest.mark.parametrize("failure", ["schema", "timeout", "ungrounded_field", "version"])
def test_retry_stays_bounded_and_feedback_never_weakens_validation(store, failure):
    batch = batch_for(store, "红米手机还没收到")
    value = AssociationService(store, "alice")
    calls = []
    commit = value.commit

    def first_conflict(*args, **kwargs):
        from app.tasks.repository import VersionConflict
        if len(calls) == 1 and failure == "version":
            raise VersionConflict()
        return commit(*args, **kwargs)

    value.commit = first_conflict

    def associate(context):
        calls.append(context.data())
        if len(calls) == 1:
            if failure == "schema":
                return {"wrong_schema": []}
            if failure == "timeout":
                raise TimeoutError("provider credentials must never enter retry context")
            if failure == "ungrounded_field":
                return proposal(new(batch, details={"item_name": "凭空编造"}))
        return proposal(new(batch, "红米手机查物流", details={"item_name": "红米"}))

    result = value.process(batch, SimpleNamespace(associate=associate))
    assert len(calls) == 2 and result["clarification"] is None
    assert "retry_feedback" not in calls[0]
    assert calls[1]["retry_feedback"] == result["rejections"]
    assert "provider credentials" not in json.dumps(calls[1])
    assert value.repo.get(result["decisions"][0]["task_id"]).details["item_name"] == "红米"


def test_repeated_unsupported_fields_still_clarify_with_no_guessed_values(store):
    batch = batch_for(store, "红米手机")
    value = AssociationService(store, "alice")
    calls = []

    def associate(context):
        calls.append(context)
        return proposal(new(batch, details={"item_name": "小米12"}))

    result = value.process(batch, SimpleNamespace(associate=associate))
    assert len(calls) == 2
    assert result["rejections"] == ["ungrounded_field", "ungrounded_field"]
    assert result["clarification"] and not value.repo.list()


def test_real_candidate_gaps_are_still_blocking(store):
    batch = batch_for(store, "红米手机")
    value = AssociationService(store, "alice")
    snapshot = value.snapshot(batch)
    snapshot.context.gaps.append("history_unavailable")
    with pytest.raises(AssociationRejected, match="incomplete_candidate_context"):
        value.commit(batch, snapshot, proposal(new(batch)))


@pytest.mark.parametrize("scope", [BusinessScope(), BusinessScope("pdd", "shop-1")])
def test_explicit_order_enrichment_is_lazily_wired_to_current_commerce_contract(store, scope):
    from app.services.commerce.service import service_factory
    batch = batch_for(store, "红米手机")
    calls = []

    def factory(user, received_scope):
        calls.append((user, received_scope))
        def read(name, args):
            calls.append((name, args))
            return dict(success=True, data=dict(items=[dict(order_id="A", platform_code="pdd",
                merchant_id="shop-1", order_ref="opaque-ref", purchase_summary=[dict(name="红米", specs={}, quantity=1)])]),
                next_cursor=None)
        return SimpleNamespace(read=read)

    with service_factory(factory):
        value = AssociationService(store, "alice", scope=scope)
        assert calls == []
        context = value.snapshot(batch, check_orders=True).context
    assert calls == [("alice", scope), ("find_orders", {})]
    assert context.gaps == []
    assert context.orders[0]["items_summary"] == "红米"
    assert context.orders[0]["order_ref"] == "opaque-ref"


@pytest.mark.parametrize("response,expected", [
    (dict(success=True, data=dict(items=[], total=0)), []),
    (dict(success=True, data=dict(items=[], has_more=True), next_cursor="next"), ["order_candidates_limited"]),
    (dict(success=True, orders=[]), ["orders_unavailable"]),
    (dict(success=False, data=dict(items=[])), ["orders_unavailable"]),
    (dict(success=True, data=dict(items=[dict(order_id="A", platform_code="pdd", merchant_id="other")])),
        ["orders_unavailable"]),
])
def test_explicit_enrichment_preserves_empty_partial_failed_and_scope_results(store, response, expected):
    from app.services.commerce.service import service_factory
    batch = batch_for(store, "红米手机")
    calls = []

    def read(*args):
        calls.append(args)
        return response

    with service_factory(lambda *_: SimpleNamespace(read=read)):
        context = AssociationService(store, "alice", scope=BusinessScope("pdd", "shop-1")).snapshot(
            batch, check_orders=True).context
    assert context.gaps == expected and context.orders == []
    assert len(calls) == 1  # Never crawl extra pages during candidate enrichment.
