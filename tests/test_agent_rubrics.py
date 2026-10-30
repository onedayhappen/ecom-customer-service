# 文件说明：测试 rubric 契约、证据引用、事实优先级和评测示例可执行性。
"""Rubric protocol, scoring, evidence validation and executable dataset examples."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.evaluation.agent.contracts import Captures, DIMENSIONS, Dimension, Suite
from app.evaluation.agent.grading import evaluate, grade
from app.evaluation.agent.judge import SemanticJudge
from app.evaluation.agent.run import main
from app.evaluation.agent.runtime import run_inputs
from tests.test_agent_e2e import ContinueClient, ScriptedClient, response, rubric, scenario, synthetic

DATA = Path(__file__).resolve().parents[1] / "app/evaluation/agent/agent_demo.json"


def rubric_case():
    case = scenario()
    for name in DIMENSIONS:
        setattr(case.expected, name, Dimension(rubrics=[rubric()]))
    return case


def verdict(status="pass", id="facts"):
    return dict(id=id, status=status, reason="根据本次查询回执及公开回复判断。", citations=[
        dict(source="replies", index=0, path=["content"]),
        dict(source="tool_calls", index=0, path=["result", "success"])])


def model_judge(results, calls=None):
    def create(**kwargs):
        if calls is not None:
            calls.append(json.loads(kwargs["messages"][1]["content"]))
        return response(json.dumps(dict(results=results)))
    return SemanticJudge(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "test-judge")


def test_all_dimensions_accept_rubrics_and_semantically_different_paths():
    case, calls = rubric_case(), []
    cap = synthetic(case)
    cap.observations["replies"] = [dict(content="目前没有可供核实的物流轨迹，尚不能确定配送进度。")]
    cap.observations["tool_calls"].insert(1, dict(tool="find_orders", finished=True))
    result = grade(case, cap, judge=model_judge([verdict()], calls))
    assert result["passed"]
    assert [c["dimension"] for c in calls] == list(DIMENSIONS)
    assert all(d["rubric_score"] == 1 and d["checks"] == [] for d in result["dimensions"].values())
    assert all("expected" not in c and "rubrics" in c for c in calls)


def test_factual_failure_cannot_be_overridden_by_judge_pass():
    case = scenario()
    case.expected.context_consistency.rubrics = [rubric()]
    cap = synthetic(case)
    cap.observations["tasks"][0]["details"]["order_id"] = "WRONG"
    result = grade(case, cap, judge=model_judge([verdict()]))
    dimension = result["dimensions"]["context_consistency"]
    assert dimension["status"] == "fail" and dimension["rubric_score"] == 1
    assert dimension["checks"][0]["status"] == "fail" and not result["passed"]


@pytest.mark.parametrize("state, score, status", [("pass", 1, "pass"), ("partial", .5, "partial"),
    ("fail", 0, "fail"), ("insufficient_evidence", None, "error")])
def test_scores_coverage_and_pass_rates_are_distinct(state, score, status):
    case = rubric_case()
    suite = Suite(name="rubric", description="test", scenarios=[case])
    report = evaluate(suite, [synthetic(case)], 1, judge=model_judge([verdict(state)]))
    assert report["schema_version"] == 3
    for dim in report["results"][0]["dimensions"].values():
        assert dim["status"] == status and dim["rubric_score"] == score
    summary = report["groups"]["scripted_regression"]["complete"]["task_completion"]
    assert summary["pass_rate"] == (1 if state == "pass" else 0)
    assert summary["rubric_mean_score"] == score
    assert summary["rubric_scored"] == (0 if score is None else 1)
    assert summary["evaluation_errors"] == (1 if score is None else 0)


@pytest.mark.parametrize("rows", [[], [verdict(), verdict()], [verdict(id="invented")]])
def test_missing_duplicate_and_extra_rubric_results_never_pass(rows):
    case = rubric_case()
    result = grade(case, synthetic(case), judge=model_judge(rows))
    assert all(d["status"] == "error" for d in result["dimensions"].values())


def test_one_unscored_rubric_does_not_disappear_from_average():
    case = rubric_case()
    case.expected.task_completion.rubrics.append(rubric().model_copy(update=dict(id="second")))
    result = grade(case, synthetic(case), judge=model_judge([verdict(), verdict("insufficient_evidence", "second")]))
    assert result["dimensions"]["task_completion"]["rubric_score"] is None


@pytest.mark.parametrize("fault", ["missing_collection", "unfinished_call", "not_started"])
def test_incomplete_execution_is_not_sent_to_judge(fault):
    case, calls = rubric_case(), []
    cap = synthetic(case)
    if fault == "missing_collection":
        del cap.observations["events"]
    elif fault == "unfinished_call":
        cap.observations["tool_calls"][0]["finished"] = False
    else:
        cap.started, cap.termination = False, "not_started"
    result = grade(case, cap, judge=model_judge([verdict()], calls))
    assert not result["passed"] and calls == []


def test_absence_evidence_is_only_valid_for_a_complete_empty_collection():
    case = rubric_case()
    cap = synthetic(case)
    cap.observations["replies"] = []
    row = verdict("fail")
    row["citations"] = [dict(source="replies", index=None, path=[])]
    judge = model_judge([row])
    assert judge(case, cap, "reply_accuracy")[0]["status"] == "fail"
    cap.observations["replies"] = [dict(content="实际存在回复")]
    assert judge(case, cap, "reply_accuracy")[0]["status"] == "error"


def test_pass_requires_declared_evidence_and_a_real_reply():
    case, cap = rubric_case(), synthetic()
    row = verdict()
    row["citations"] = row["citations"][:1]
    assert model_judge([row])(case, cap, "task_completion")[0]["status"] == "error"
    row = verdict()
    row["citations"][0] = dict(source="replies", index=None, path=[])
    cap.observations["replies"] = []
    assert model_judge([row])(case, cap, "reply_accuracy")[0]["status"] == "error"


def test_schema_rejects_empty_dimensions_duplicate_ids_and_old_dataset_version():
    for value in ({}, dict(rubrics=[rubric().model_dump()] * 2),
                  dict(applicable=False, reason="excluded", rubrics=[rubric().model_dump()]),
                  dict(semantic_rubric="old format")):
        with pytest.raises(ValidationError):
            Dimension.model_validate(value)
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["schema_version"] = 2
    with pytest.raises(ValidationError):
        Suite.model_validate(data)


def test_cli_requires_judge_before_creating_output_or_calling_agent(tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["--dataset", str(DATA), "--live-model", "--output", str(out)]) == 2
    assert "--judge-model" in capsys.readouterr().out
    assert not out.exists()


def test_cli_rubric_replay_writes_separate_scores_and_judge_records(tmp_path, monkeypatch):
    from app.config.settings import settings
    case = rubric_case()
    dataset, traces, out = tmp_path / "suite.json", tmp_path / "captures.json", tmp_path / "out"
    dataset.write_text(Suite(name="replay", description="test", scenarios=[case]).model_dump_json(), encoding="utf-8")
    traces.write_text(Captures(captures=[synthetic(case)]).model_dump_json(), encoding="utf-8")
    calls, closed = [], []
    client = model_judge([verdict()], calls).client
    client.close = lambda: closed.append(True)
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    assert main(["--dataset", str(dataset), "--traces", str(traces), "--repetitions", "1",
                 "--judge-model", "fake-judge", "--output", str(out)]) == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    records = json.loads((out / "judge.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 3 and report["results"][0]["passed"]
    assert len(records) == len(calls) == 3 and closed == [True]
    assert {r["dimension"] for r in records} == set(DIMENSIONS)
    assert all(r["status"] == "scored" for r in records)
    assert json.loads((out / "captures.json").read_text(encoding="utf-8"))["schema_version"] == 2


@pytest.mark.parametrize("index", [0, 1, 2])
def test_demo_inputs_execute_and_factual_checks_match_real_evidence(index):
    suite = Suite.model_validate_json(DATA.read_text(encoding="utf-8"))
    case = suite.scenarios[index]
    if index == 2:
        client = ScriptedClient("prepare_order_cancel", case.inputs.writes[0].arguments)
    else:
        client = (ContinueClient if index == 1 else ScriptedClient)()
    cap = run_inputs(case.inputs, client=client, model="scripted", run_id=uuid4().hex, scenario_id=case.id)
    assert cap.termination == "settled", cap.error
    result = grade(case, cap)
    assert not result["passed"]  # No real judge verdict has been supplied.
    for dim in result["dimensions"].values():
        assert all(c["status"] == "pass" for c in dim["checks"]), dim
        assert all(r["status"] == "error" for r in dim["rubrics"])
