"""Evaluation correctness, current-policy integration and explicit live-call guards."""
import json
from math import log2
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.evaluation.dataset import EvalCase, EvalDataset, ReplayDataset, ReplayTrace, load_dataset, load_traces
from app.evaluation.evaluator import Evaluator, current_policy_search, evidence_for
from app.evaluation.judges import ModelGrader
from app.evaluation.metrics import retrieval_metrics
from app.evaluation.run import DATA, main


def fixture():
    return load_dataset(DATA / "rag_demo.json"), load_traces(DATA / "rag_demo_traces.json")


def test_retrieval_metrics_account_for_grades_duplicates_and_cutoff():
    labels = {"a": 3, "b": 1}
    actual = retrieval_metrics(labels, ["x", "b", "b", "a"], 4)
    expected = (1 / log2(3) + 7 / log2(5)) / (7 + 1 / log2(3))
    assert actual == pytest.approx(dict(recall=1, precision=.5, mrr=.5, ndcg=expected))
    assert retrieval_metrics(labels, ["x", "b", "b", "a"], 3)["recall"] == .5
    assert retrieval_metrics(labels, [], 5) == dict(recall=0, precision=0, mrr=0, ndcg=0)
    assert retrieval_metrics(labels, ["a"], 5)["precision"] == .2
    assert all(v is None for v in retrieval_metrics({}, ["a"], 3).values())
    assert retrieval_metrics(labels, ["a", "b"], 5)["ndcg"] == 1
    with pytest.raises(ValueError):
        retrieval_metrics(labels, [], 0)


def test_version_qualified_labels_and_macro_denominators():
    dataset, replay = fixture()
    report = Evaluator().run_all(dataset, traces=replay.traces)
    assert report["summary"]["retrieval"]["recall@1"] == dict(mean=.5, count=4)
    assert report["summary"]["abstention"] == dict(mean=1, count=2)
    assert report["cases"][3]["metrics"]["mrr@3"] == .5
    assert report["summary"]["faithfulness"]["count"] == 0
    assert report["summary"]["faithfulness"]["mean"] is None


@pytest.mark.parametrize("case_index", [0, 4])
def test_retrieval_outage_is_not_correct_abstention(case_index):
    dataset, replay = fixture()
    trace = replay.traces[case_index].model_dump(mode="json")
    trace["retrieval"] = dict(success=False, error_code="policy_retrieval_unavailable", results=[])
    result = Evaluator().run_case(dataset.cases[case_index], trace=ReplayTrace.model_validate(trace))
    assert result["status"] == "retrieval_error"
    assert not result["retrieval_passed"] and not result["checks_passed"]
    if case_index == 0:
        assert result["metrics"]["recall@5"] == 0


@pytest.mark.parametrize("field,value", [("query", "different"), ("requested_k", 1)])
def test_mismatched_or_short_replays_fail(field, value):
    dataset, replay = fixture()
    trace = replay.traces[0].model_copy(update={field: value})
    result = Evaluator().run_case(dataset.cases[0], trace=trace)
    assert result["status"] == "evaluation_error" and not result["checks_passed"]


def test_dataset_labels_and_trace_structure_are_strict():
    dataset, replay = fixture()
    case = dataset.cases[0].model_dump(mode="json")
    case["relevance"] *= 2
    with pytest.raises(ValidationError):
        EvalCase.model_validate(case)
    case = dataset.cases[0].model_dump(mode="json")
    case["expected_error"] = "policy_evidence_missing"
    with pytest.raises(ValidationError):
        EvalCase.model_validate(case)
    trace = replay.traces[0].model_dump(mode="json")
    trace["retrieval"]["success"] = "false"
    with pytest.raises(ValidationError):
        ReplayTrace.model_validate(trace)
    with pytest.raises(ValidationError):
        ReplayDataset(description="duplicate", traces=[replay.traces[0], replay.traces[0]])
    with pytest.raises(ValueError, match="IDs"):
        Evaluator().run_all(dataset, traces=replay.traces[:-1])


class Client:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def claims(*verdicts):
    return json.dumps(dict(claims=[dict(claim=f"claim-{i}", verdict=verdict,
        evidence_indices=[0] if verdict == "supported" else [],
        quotes=["申请已提交"] if verdict == "supported" else [], reason="核对证据")
        for i, verdict in enumerate(verdicts)]), ensure_ascii=False)


def test_faithfulness_is_claim_support_and_not_boolean_string_coercion():
    client = Client(claims("supported", "contradicted"), '{"faithful":"false"}', claims())
    judge = ModelGrader(client, "fake-judge")
    result = judge.judge_faithfulness("进度？", "申请已提交，钱已到账。", ["申请已提交，等待审核。"])
    assert result["status"] == "scored" and result["score"] == .5
    assert judge.judge_faithfulness("问题", "回答", ["依据"])["status"] == "error"
    assert judge.judge_faithfulness("你好", "你好", [])["status"] == "not_applicable"
    assert client.requests[0]["messages"][0]["role"] == "system"
    assert judge.calls[0]["input_tokens"] == 10


@pytest.mark.parametrize("reply", [claims("supported"), "not json", '{"claims":[]}'])
def test_judge_never_defaults_missing_evidence_or_invalid_output_to_full_credit(reply):
    judge = ModelGrader(Client(reply), "fake")
    result = judge.judge_faithfulness("问题", "退款已到账", [])
    assert result["score"] is None
    assert result["status"] in {"error", "not_applicable"}


def test_judge_failures_and_budget_do_not_disappear_or_leak_sdk_errors():
    judge = ModelGrader(Client(RuntimeError("private token value")), "fake")
    result = judge.judge_faithfulness("问题", "回答", ["依据"])
    assert result == dict(status="error", score=None, reason="RuntimeError")
    assert judge.calls[0]["success"] is False
    client = Client()
    result = ModelGrader(client, "fake", max_input_chars=10).judge_faithfulness("问题", "回答", ["依据"])
    assert result["status"] == "error" and not client.requests
    judge = ModelGrader(Client('{"score": true, "reason":"bad"}'), "fake")
    assert judge.judge_answer_quality("q", "a", None, [])["status"] == "error"


def test_judge_receives_answer_and_actual_evidence_but_no_gold_as_evidence():
    dataset, replay = fixture()
    client = Client(claims("unsupported"), '{"score":2,"reason":"无依据"}')
    judge = ModelGrader(client, "fake")
    row = Evaluator(judge=judge).run_case(dataset.cases[0], trace=replay.traces[0])
    assert row["faithfulness"]["score"] == 0 and not row["checks_passed"]
    request = json.loads(client.requests[0]["messages"][1]["content"])
    assert request["answer"] == replay.traces[0].answer
    assert "reference_answer" not in request and "relevance" not in request
    assert dataset.cases[0].reference_answer not in json.dumps(request["evidence"], ensure_ascii=False)


def test_missing_answer_and_judge_errors_are_reported_with_coverage():
    dataset, replay = fixture()
    judge = ModelGrader(Client(), "fake")
    trace = replay.traces[0].model_copy(update={"answer": None})
    subset = EvalDataset(name="subset", description="test", cases=[dataset.cases[0]])
    report = Evaluator(judge=judge).run_all(subset, traces=[trace])
    assert report["summary"]["errors"] == 1
    assert report["summary"]["faithfulness"]["statuses"] == {"missing_answer": 1}
    assert report["summary"]["checks_passed"] == 0


def test_multiline_policy_quotes_preserve_original_text():
    _, replay = fixture()
    trace = replay.traces[0].model_copy(deep=True)
    quote = "申请已提交\n等待审核。"
    trace.retrieval.results[0].text = quote
    response = json.dumps(dict(claims=[dict(claim="还在等待审核", verdict="supported",
        evidence_indices=[0], quotes=[quote], reason="引用原文")]), ensure_ascii=False)
    result = ModelGrader(Client(response), "fake").judge_faithfulness(
        trace.query, "还在等待审核", evidence_for(trace))
    assert result["status"] == "scored" and result["score"] == 1


def test_real_policy_retriever_through_current_service(tmp_path, monkeypatch):
    from app.config.settings import settings
    from app.rag import service
    from app.rag.chunker import policy_chunks
    from app.rag.releases import PolicyIndex
    from app.rag.retriever import PolicyRetriever
    from app.rag.updater import KnowledgeBaseUpdater
    from tests.test_policy_retrieval import Embedder, document, context
    root = tmp_path / "knowledge"
    root.mkdir()
    document(root, "refund")
    embedder = Embedder()
    index = PolicyIndex(tmp_path / "index")
    KnowledgeBaseUpdater(root, tmp_path / "manifest.json", embedder, None).publish_policies(index)
    monkeypatch.setattr(settings, "retrieval_service_url", "")
    monkeypatch.setattr(service, "_policy_retriever", PolicyRetriever(embedder, index))
    chunk = next(c for c in policy_chunks(root) if c.section == "七天退货")
    positive = EvalCase(id="positive", query="七天退货", context=context(), relevance=[
        dict(source_id=chunk.policy["source_id"], version=chunk.policy["version"], section=chunk.section, grade=3)])
    negative = EvalCase(id="negative", query="星际航行燃料", context=context(), expected_error="policy_evidence_missing")
    dataset = EvalDataset(name="integration", description="real Chroma with deterministic fixture vectors", cases=[positive, negative])
    report = Evaluator().run_all(dataset, search=current_policy_search)
    assert report["summary"]["retrieval_passed"] == 2
    receipt = report["cases"][0]["trace"]["retrieval"]
    assert receipt["index"]["generation"] == index.head()["generation"]
    assert receipt["results"][0]["chunk_id"] == chunk.chunk_id
    assert receipt["context"] == positive.context.model_dump(mode="json")


def test_live_adapter_rejects_stale_evidence(monkeypatch):
    from app.rag import service
    monkeypatch.setattr(service, "search_policy", lambda *a: {"success": True, "results": []})
    monkeypatch.setattr(service, "policy_result_current", lambda *a: False)
    assert current_policy_search("q", {}, 3)["error_code"] == "policy_release_changed"


@pytest.mark.parametrize("arguments", [
    ["--judge"], ["--mode", "retrieve"], ["--mode", "retrieve", "--live"],
    ["--generate-answers", "--live"], ["--k", "6"], ["--dataset", "custom.json"],
    ["--output", str(DATA / "rag_demo.json")],
])
def test_cli_rejects_unsafe_or_ambiguous_modes_before_calls(arguments):
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_cli_offline_replay_does_not_initialize_live_components(tmp_path, monkeypatch):
    import app.evaluation.run as runner
    monkeypatch.setattr(runner, "current_policy_search", lambda *a: pytest.fail("live search invoked"))
    output = tmp_path / "report.json"
    assert main(["--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["model_calls"] == {"judge": [], "answer": []}
    assert report["summary"]["total"] == 6
    assert len(report["dataset_sha256"]) == 64


def test_generated_answers_are_graded_without_replacing_input_trace():
    dataset, replay = fixture()
    generator = ModelGrader(Client("申请已提交"), "generator")
    trace = replay.traces[2]
    judge = ModelGrader(Client(claims("supported"), '{"score":4,"reason":"基本准确"}'), "judge")
    # This actual evidence contains the literal quote required by the fake judge.
    receipt = trace.retrieval.model_dump(mode="json")
    receipt["results"][0]["text"] = "申请已提交，等待审核。"
    row = Evaluator(judge=judge, answer_generator=generator).run_case(
        dataset.cases[2], search=lambda *args: receipt)
    assert row["checks_passed"]
    assert row["trace"]["answer"] == "申请已提交"
    assert row["answer_origin"] == "rag_harness_generated"
    assert trace.answer != "申请已提交"
