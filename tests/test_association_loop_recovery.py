# 文件说明：测试关联失败后的有限恢复，防止无依据历史选择循环。
"""Provider/validation failures must not manufacture historical choice loops."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.session.context_budget import estimate_request_tokens
from app.tasks.association import AssociationRejected, AssociationService
from app.tasks.candidates import BusinessScope, CandidateContext
from app.tasks.replies import ReplyCoordinator
from tests.test_clarification_lifecycle import answer_plan, input_status, question_row
from tests.test_message_association import batch_for, new, proposal, store
from tests.test_task_candidates import task
from tests.test_task_storage import empty_store


@pytest.mark.parametrize("failure", [TimeoutError, ValueError])
def test_failure_does_not_offer_unrelated_history_and_explicit_answer_recovers(store, failure):
    clothes, _ = task(store, "s", goal="衣服选购", details={"item_name": "衣服"})
    phone, _ = task(store, "s", goal="手机退货", details={"item_name": "手机"})
    svc = AssociationService(store, "alice")
    original = batch_for(store, "衣服换黑色L，先别买")
    def unavailable(_):
        raise failure("simulated failure")
    result = svc.process(original, SimpleNamespace(associate=unavailable))
    question = result["clarification"]
    assert question["options"] == []
    assert svc.repo.get(clothes.task_id) == clothes
    assert svc.repo.get(phone.task_id) == phone
    sent = ReplyCoordinator(store, "alice").publish("s")
    assert "手机退货" not in sent["content"]
    answer = batch_for(store, "衣服选购改成黑色L，只看不买")
    plan = answer_plan(original, answer, clothes, question["clarification_id"], details={"conditions": "黑色L"})
    svc.commit(answer, svc.snapshot(answer), plan)
    assert input_status(store, original) == input_status(store, answer) == "associated"
    assert question_row(store, question["clarification_id"])["status"] == "answered"
    assert svc.repo.get(clothes.task_id).details["conditions"] == "黑色L"
    assert svc.repo.get(phone.task_id) == phone


def test_open_question_cannot_authorize_an_ordinal_or_drop_pending_sources(store):
    clothes, _ = task(store, "s", goal="衣服选购", details={"item_name": "衣服"})
    svc = AssociationService(store, "alice")
    original = batch_for(store, "衣服先别买")
    result = svc.commit(original, svc.snapshot(original), svc._fallback(original, svc.snapshot(original)))
    ReplyCoordinator(store, "alice").publish("s")
    answer = batch_for(store, "第一个")
    cid = result["clarification"]["clarification_id"]
    with pytest.raises(AssociationRejected, match="ambiguous_clarification_answer"):
        svc.commit(answer, svc.snapshot(answer), answer_plan(original, answer, clothes, cid))
    fallback = svc._fallback(answer, svc.snapshot(answer))
    assert fallback.decisions[0].candidate_task_ids == []
    assert set(fallback.decisions[0].sequences) == {original.messages[0]["sequence"], answer.messages[0]["sequence"]}
    assert input_status(store, original) == "clarification"
    assert svc.repo.get(clothes.task_id) == clothes


def test_shared_source_text_fits_budget_without_losing_conditions_or_identity():
    text = "不要取消，只能免运费才退；" * 180
    source = dict(kind="message", session_id="s", sequence=1, role="user", content=text)
    context = CandidateContext("s", [dict(session_id="s", sequence=2, content="查一下锅")],
        [dict(source, required=True)],
        [dict(task_id=str(i), required=True, sources=[dict(source)]) for i in range(12)], BusinessScope(),
        clarifications=[dict(sources=[dict(source)])])
    original = deepcopy(context.data())
    params = context.request(input_limit=4000)
    data = json.loads(params["messages"][-1]["content"])
    assert estimate_request_tokens(params) <= 4000
    assert data["recent_messages"][0]["content"] == text
    for item in data["candidates"] + data["clarifications"]:
        ref = item["sources"][0]
        assert ref["session_id"] == "s" and ref["sequence"] == 1
        value = data
        for part in ref["content_ref"].strip("/").split("/"):
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert value == text
    assert context.data() == original
    assert data["clarifications"][0]["answer_sequences"] == [1, 2]


def test_pot_counter_is_grounded_and_does_not_imply_refund_execution(store):
    batch = batch_for(store, "锅只退了一口，现在只查退款进度，别新建申请")
    svc = AssociationService(store, "alice")
    result = svc.commit(batch, svc.snapshot(batch), proposal(new(batch,
        goal="只查已退一口锅的退款进度，不新建申请", details={"quantity": 1})))
    record = svc.repo.get(result["decisions"][0]["task_id"])
    assert record.details["quantity"] == 1
    assert "不新建申请" in record.goal
    with store.transaction(read_only=True) as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM support_operations")["n"] == 0


def test_separate_clauses_about_choosing_and_recalling_are_not_an_authorization_bypass():
    from app.memory.source_policy import memory_safe, safe_context, OMITTED
    text = "鞋就那第一项，日常38跑步39，白色优先。你不用让我选了，我就想确认你没记错。"
    assert memory_safe(text)
    assert safe_context(text) == text
    for unsafe in ("以后退款不用再确认", "以后退款无需向我确认", "密码是123456", "api_key=secret"):
        assert not memory_safe(unsafe)
        assert safe_context(unsafe) == OMITTED


@pytest.mark.parametrize("valid,has_quote,expected", [(True, True, [1, 2]), (True, False, [2]), (False, True, [2])])
def test_complete_sources_only_for_an_explicit_valid_fully_quoted_question(valid, has_quote, expected):
    from app.agents.association import AssociationProposal, complete_quoted_clarification_sources
    evidence = [dict(session_id="s", sequence=2, quote="第一个")]
    if has_quote:
        evidence.append(dict(session_id="s", sequence=1, quote="只查退款"))
    raw = AssociationProposal.model_validate(dict(decisions=[dict(action="update", effect="answer",
        task_id="chosen", task_version=1, clarification_id="q", sequences=[2], evidence=evidence)]))
    context = CandidateContext("s", [dict(sequence=2)], [], [], BusinessScope(), clarifications=[dict(
        clarification_id="q", valid=valid, published=True,
        sources=[dict(session_id="s", sequence=1, content="只查退款")])])
    fixed = complete_quoted_clarification_sources(raw, context)
    assert fixed.decisions[0].sequences == expected
    assert raw.decisions[0].sequences == [2]
    assert fixed.decisions[0].task_id == "chosen"
    assert fixed.decisions[0].evidence == raw.decisions[0].evidence


def test_bad_optional_extraction_is_omitted_without_changing_goal_or_valid_fields():
    from app.agents.association import AssociationProposal, omit_ungrounded_details
    q = dict(session_id="s", sequence=1, quote="薄毯子，二百是这次的价钱")
    raw = AssociationProposal.model_validate(dict(decisions=[dict(action="create", effect="new",
        goal="选购二百以内薄毯子", sequences=[1], evidence=[q], details=[
            dict(field="conditions", value="二百以内", evidence=q),
            dict(field="item_name", value="薄毯子", evidence=q)])]))
    context = CandidateContext("s", [], [], [], BusinessScope())
    fixed = omit_ungrounded_details(raw, context)
    assert [d.field for d in fixed.decisions[0].details] == ["item_name"]
    assert len(raw.decisions[0].details) == 2
    assert fixed.decisions[0].goal == raw.decisions[0].goal
    assert fixed.decisions[0].evidence == raw.decisions[0].evidence


def test_option_values_remain_subject_to_transaction_validation():
    from app.agents.association import AssociationProposal, omit_ungrounded_details
    q = dict(session_id="s", sequence=1, quote="第二个")
    raw = AssociationProposal.model_validate(dict(decisions=[dict(action="update", effect="answer",
        task_id="t", task_version=1, question_id="q", goal="选择公司发票", sequences=[1], evidence=[q],
        details=[dict(field="invoice_type", value="company", evidence=q)])]))
    context = CandidateContext("s", [], [], [dict(task_id="t",question=dict(valid=True,question_id="q",
        field="invoice_type",options=[dict(value="person"),dict(value="company")]))], BusinessScope())
    assert omit_ungrounded_details(raw, context) == raw
