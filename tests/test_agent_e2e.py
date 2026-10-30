# 文件说明：测试真实服务入口驱动、业务确认与故障情况下的评测判定。
"""Technical fixtures, not a benchmark dataset or a model-quality measurement."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.evaluation.agent.contracts import COLLECTIONS, Capture, Inputs, Rubric, Scenario, Suite, digest
from app.evaluation.agent.grading import evaluate, grade
from app.evaluation.agent.runtime import run_inputs
from app.services.commerce.common import ref


def predicate(path, value, op="equals"):
    return dict(path=path.split("."), value=value, op=op)


def check(id, source, where=(), assertions=(), **kwargs):
    return dict(id=id, source=source, where=list(where), assertions=list(assertions), **kwargs)


def rubric():
    return Rubric(id="facts", criterion="业务结论是否有依据", pass_when="事实和限制均准确",
        partial_when="事实准确但遗漏非关键说明", fail_when="编造业务状态",
        evidence_sources=["replies", "tool_calls"])


def scenario():
    target = ref("order", "pdd", "ORD-A")
    return Scenario.model_validate(dict(id="query", description="Technical fixture", inputs=dict(
        sessions={"s": "alice"}, steps=[dict(id="first", session="s", trigger=dict(kind="initial"),
            text="查 pdd 订单 ORD-A 的物流")],
        reads=[dict(tool="get_logistics", user="alice", arguments=dict(target_ref=target),
                    data=dict(items=[], total=0))], budget=dict(seconds=12.0)), expected=dict(
        task_completion=dict(checks=[check("query", "tool_calls", [predicate("tool", "get_logistics")],
            [predicate("result.success", True), predicate("arguments.target_ref", target)]),
            check("notice", "replies", assertions=[predicate("content", "暂未查到", "contains")])]),
        context_consistency=dict(checks=[check("object", "tasks", assertions=[predicate("details.order_id", "ORD-A")],
            quantifier="all")]),
        reply_accuracy=dict(checks=[check("uncertainty", "replies", assertions=[predicate("content", "仍需核实", "contains")]),
            check("no-fabrication", "replies", [predicate("content", "已经签收", "contains")], quantifier="none")]))))


def response(content=None, calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
        content=content, tool_calls=[SimpleNamespace(id=uuid4().hex,
            type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a))) for n, a in calls]))])


class ScriptedClient:
    """Substitute only SDK outputs; real association/execution/publication code runs."""
    def __init__(self, tool="get_logistics", args=None, scene="execution", owner=None):
        self.tool, self.args, self.scene = tool, args or dict(target_ref=ref("order", "pdd", "ORD-A")), scene
        self.owner = owner or self
        if owner is None:
            self.used = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def bind_scene(self, scene):
        return type(self)(self.tool, self.args, scene, self.owner)

    def create(self, **kwargs):
        if self.scene == "query_rewrite":
            data = json.loads(kwargs["messages"][-1]["content"])
            return response(data["current_messages"][-1]["content"])
        if self.scene == "reply_composition":
            data = json.loads(kwargs["messages"][-1]["content"])
            return response(json.dumps(dict(claim_ids=[c["claim_id"] for m in data["matters"] for c in m.get("claims", [])])))
        if "AssociationProposal" in kwargs["messages"][0]["content"]:
            data = json.loads(kwargs["messages"][-1]["content"])
            message = data["current_messages"][-1]
            quote = dict(session_id=data["session_id"], sequence=message["sequence"], quote=message["content"])
            return response(json.dumps(dict(decisions=[dict(sequences=[message["sequence"]], action="create",
                effect="new", goal=message["content"], evidence=[quote], details=[
                    dict(field="order_id", value="ORD-A", evidence=quote),
                    dict(field="platform_code", value="pdd", evidence=quote)])])))
        self.owner.used += 1
        if self.owner.used == 1:
            return response(calls=[(self.tool, self.args)])
        return response("查询已结束。")


@pytest.fixture
def query_capture():
    case = scenario()
    capture = run_inputs(case.inputs, client=ScriptedClient(), model="scripted-test",
                         run_id=uuid4().hex, scenario_id=case.id)
    return case, capture


def test_actual_service_entry_query(query_capture):
    case, capture = query_capture
    assert capture.termination == "settled", capture.model_dump()
    assert all(capture.complete.values()), capture.model_dump()
    result = grade(case, capture)
    assert result["passed"], (result, capture.model_dump())
    assert {"rewrite_context", "associate_context", "compose_reply_context"} <= {e["kind"] for e in capture.observations["events"]}
    assert capture.source == "scripted_regression"
    timing = capture.metadata["latency"]["requests"]
    assert len(timing) == 1 and timing[0]["status"] == "settled"
    assert 0 <= timing[0]["first_reply_seconds"] <= timing[0]["settled_seconds"]


@pytest.mark.parametrize("mutate", ["wrong_object", "lost_result", "false_reply", "missing_collection"])
def test_faults_never_pass(query_capture, mutate):
    case, capture = query_capture
    assert grade(case, capture)["passed"], (capture.termination, grade(case, capture))
    if mutate == "wrong_object":
        capture.observations["tasks"][0]["details"]["order_id"] = "ORD-B"
    elif mutate == "lost_result":
        capture.observations["tool_calls"] = []
    elif mutate == "false_reply":
        capture.observations["replies"].append(dict(content="已经签收", _seq=999))
    else:
        capture.complete["replies"] = False
    assert not grade(case, capture)["passed"]


def test_real_confirmation_changes_independent_state():
    inputs = Inputs.model_validate(dict(sessions={"s": "alice"}, objects={"order_a": dict(
        user_id="alice", order_id="ORD-A", platform_code="pdd", status="pending_payment")},
        writes=[dict(tool="prepare_order_cancel", user="alice", arguments=dict(order_ref=ref("order", "pdd", "ORD-A")),
            state_key="order_a", changes=dict(status="cancelled"), result_status="cancelled",
            review=dict(order_id="ORD-A", action="prepare_order_cancel"))],
        steps=[dict(id="request", session="s", trigger=dict(kind="initial"), text="取消 pdd 订单 ORD-A"),
            dict(id="confirm", session="s", action="confirm", trigger=dict(kind="confirmation",
                where=[predicate("can_confirm", True)]), card=[predicate("review.order_id", "ORD-A"),
                predicate("review.action", "prepare_order_cancel")])], budget=dict(seconds=15.0)))
    capture = run_inputs(inputs, client=ScriptedClient("prepare_order_cancel", dict(order_ref=ref("order", "pdd", "ORD-A"))),
        model="scripted-test", run_id=uuid4().hex, scenario_id="cancel")
    assert capture.termination == "settled", capture.model_dump()
    assert len(capture.observations["writes"]) == 1, capture.model_dump()
    assert capture.observations["objects"][0]["status"] == "cancelled"
    assert capture.observations["operations"][0]["status"] == "succeeded"
    assert any("订单已取消" in r["content"] for r in capture.observations["replies"])


def synthetic(case=None):
    case = case or scenario()
    return Capture(run_id="r1", scenario_id=case.id, input_digest=digest(case.inputs.model_dump()), repetition=1,
        source="scripted_regression", started=True, termination="settled",
        complete={s: True for s in COLLECTIONS},
        observations=dict(tasks=[dict(details=dict(order_id="ORD-A"))], writes=[],
            tool_calls=[dict(tool="get_logistics", arguments=dict(target_ref=ref("order", "pdd", "ORD-A")),
                result=dict(success=True), finished=True, _seq=2)],
            replies=[dict(content="暂未查到，仍需核实", _seq=3)], events=[], models=[], operations=[], objects=[]), boundaries=dict(first=1))


def test_exact_replay_and_fixed_denominators(tmp_path):
    from app.evaluation.agent.contracts import Captures
    from app.evaluation.agent.run import main
    case, cap = scenario(), synthetic()
    suite = Suite(name="technical", description="not a benchmark", scenarios=[case])
    for captures in ([], [cap, cap], [cap.model_copy(update=dict(scenario_id="unknown"))]):
        with pytest.raises(ValueError):
            evaluate(suite, captures, 1)
    with pytest.raises(ValueError):
        evaluate(suite, [cap.model_copy(update=dict(input_digest="wrong"))], 1)
    failed = cap.model_copy(deep=True, update=dict(run_id="r2", repetition=2, termination="timeout"))
    not_started = cap.model_copy(deep=True, update=dict(run_id="r3", repetition=3, started=False, termination="not_started"))
    report = evaluate(suite, [cap, failed, not_started], 3)
    summary = report["groups"]["scripted_regression"]["complete"]
    assert summary["task_completion"]["denominator"] == 2
    assert summary["task_completion"]["pass_rate"] == .5
    assert summary["overall"]["denominator"] == 3
    assert summary["overall"]["pass_rate"] == 1/3
    dataset, traces, output = tmp_path / "suite.json", tmp_path / "traces.json", tmp_path / "result"
    dataset.write_text(suite.model_dump_json(), encoding="utf-8")
    traces.write_text(Captures(captures=[cap]).model_dump_json(), encoding="utf-8")
    assert main(["--dataset", str(dataset), "--traces", str(traces), "--repetitions", "1", "--output", str(output)]) == 0
    replayed = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert replayed["results"] == evaluate(suite, [cap], 1)["results"]
    assert main(["--dataset", str(dataset), "--traces", str(traces), "--repetitions", "1", "--output", str(output)]) == 2


def test_unscored_semantics_and_missing_evidence_are_not_success():
    case, cap = scenario(), synthetic()
    case.expected.reply_accuracy.rubrics = [rubric()]
    result = grade(case, cap)
    assert result["dimensions"]["reply_accuracy"]["status"] == "error"
    assert not result["passed"]
    case.expected.reply_accuracy.rubrics = []
    cap.observations["replies"][0].pop("content")
    assert grade(case, cap)["dimensions"]["reply_accuracy"]["status"] == "error"


def test_all_is_not_vacuously_true_and_scalar_types_are_strict():
    from app.evaluation.agent.contracts import Predicate
    from app.evaluation.agent.grading import matches
    cap = synthetic()
    cap.observations["tasks"] = []
    assert grade(scenario(), cap)["dimensions"]["context_consistency"]["status"] == "fail"
    assert not matches(dict(n=False), Predicate(path=["n"], value=0))
    assert matches(dict(n="88.00"), Predicate(path=["n"], value="88", op="decimal_equal"))
    assert matches(dict(t="2026-10-28T08:00:00+08:00"), Predicate(path=["t"], value="2026-10-28T00:00:00Z", op="instant_equal"))
    for actual in ("NaN", "Infinity", False):
        with pytest.raises(ValueError):
            matches(dict(n=actual), Predicate(path=["n"], value="88", op="decimal_equal"))


def test_split_replies_candidate_queries_and_committed_windows():
    from app.evaluation.agent.contracts import Check
    case, cap = scenario(), synthetic()
    cap.observations["replies"] = [dict(content="暂未查到", _seq=3), dict(content="仍需核实", _seq=4)]
    cap.observations["tool_calls"].append(dict(tool="find_orders", arguments={}, finished=True, _seq=1))
    assert grade(case, cap)["passed"]
    case.expected.context_consistency.checks.append(Check.model_validate(check("no_old_query", "tool_calls",
        [predicate("tool", "find_orders")], quantifier="none", after_step="first")))
    assert grade(case, cap)["passed"]
    cap.boundaries = {}
    assert grade(case, cap)["dimensions"]["context_consistency"]["status"] == "error"


def test_duplicate_business_write_is_detected():
    from app.evaluation.agent.contracts import Check
    case, cap = scenario(), synthetic()
    case.expected.task_completion.checks.append(Check.model_validate(check("single_write", "writes", quantifier="count", count=1)))
    cap.observations["writes"] = [dict(operation_id="one", _seq=1)]
    assert grade(case, cap)["passed"]
    cap.observations["writes"].append(dict(operation_id="two", _seq=2))
    assert not grade(case, cap)["passed"]


def test_failed_task_may_have_an_accurate_reply():
    case, cap = scenario(), synthetic()
    cap.observations["tool_calls"][0]["result"]["success"] = False
    result = grade(case, cap)
    assert result["dimensions"]["task_completion"]["status"] == "fail"
    assert result["dimensions"]["reply_accuracy"]["status"] == "pass"


def test_global_budget_and_settings_restored():
    from app.config.settings import settings
    before = settings.model_dump()
    case = scenario()
    case.inputs.budget.model_calls = 1
    cap = run_inputs(case.inputs, client=ScriptedClient(), model="test", run_id=uuid4().hex, scenario_id=case.id)
    assert cap.termination == "budget", cap.model_dump()
    assert not grade(case, cap)["passed"]
    assert settings.model_dump() == before


class CancelClient(ScriptedClient):
    def create(self, **kwargs):
        if self.scene == "association":
            data = json.loads(kwargs["messages"][-1]["content"])
            message = data["current_messages"][-1]
            if "停止" in message["content"]:
                candidate = data["candidates"][0]
                return response(json.dumps(dict(decisions=[dict(sequences=[message["sequence"]], action="update",
                    effect="cancel", task_id=candidate["task_id"], task_version=candidate["version"], evidence=[
                        dict(session_id=data["session_id"], sequence=message["sequence"], quote=message["content"])])])) )
        return super().create(**kwargs)


def test_intervention_during_model_call_uses_input_barrier():
    from app.evaluation.agent.contracts import Step
    case = scenario()
    case.inputs.steps.append(Step.model_validate(dict(id="stop", session="s", text="停止查询 ORD-A",
        trigger=dict(kind="model_call", where=[predicate("scene", "task_execution")]))))
    cap = run_inputs(case.inputs, client=CancelClient(), model="test", run_id=uuid4().hex, scenario_id=case.id)
    assert cap.termination == "settled", cap.model_dump()
    assert "stop" in cap.boundaries, cap.model_dump()
    assert cap.observations["tasks"][0]["status"] == "cancelled", cap.model_dump()
    assert not cap.observations["tool_calls"]


class ClarifyClient(ScriptedClient):
    def create(self, **kwargs):
        if self.scene == "association":
            data = json.loads(kwargs["messages"][-1]["content"])
            message = data["current_messages"][-1]
            if "是的" in message["content"]:
                candidate = data["candidates"][0]
                return response(json.dumps(dict(decisions=[dict(sequences=[message["sequence"]], action="update",
                    effect="answer", task_id=candidate["task_id"], task_version=candidate["version"],
                    question_id=candidate["question"]["question_id"], evidence=[
                        dict(session_id=data["session_id"], sequence=message["sequence"], quote=message["content"])])])))
        if self.scene == "task_execution" and self.owner.used == 0:
            self.owner.used += 1
            return response(calls=[("ask_user", dict(question="您要查询物流吗？"))])
        if self.scene == "task_execution" and self.owner.used == 1:
            self.owner.used += 1
            return response(calls=[(self.tool, self.args)])
        return super().create(**kwargs)


def test_multi_turn_continues_after_published_question():
    from app.evaluation.agent.contracts import Step
    case = scenario()
    case.inputs.steps.append(Step.model_validate(dict(id="answer", session="s", text="是的，查物流",
        trigger=dict(kind="reply", where=[predicate("content", "您要查询物流吗", "contains")]))))
    cap = run_inputs(case.inputs, client=ClarifyClient(), model="test", run_id=uuid4().hex, scenario_id=case.id)
    assert cap.termination == "settled", (cap.termination, cap.error)
    assert "answer" in cap.boundaries
    assert len(cap.observations["tasks"]) == 1
    assert grade(case, cap)["passed"], grade(case, cap)


def test_confirmation_wrong_card_does_not_submit():
    from app.evaluation.agent.contracts import Step
    case = scenario()
    case.inputs.steps.append(Step.model_validate(dict(id="confirm", session="s", action="confirm",
        trigger=dict(kind="confirmation", where=[predicate("can_confirm", True)]),
        card=[predicate("review.order_id", "ORD-B"), predicate("review.action", "prepare_order_cancel")])) )
    cap = run_inputs(case.inputs, client=ScriptedClient(), model="test", run_id=uuid4().hex, scenario_id=case.id)
    assert cap.termination == "driver_mismatch"
    assert cap.observations["writes"] == []


def test_process_supervisor_kills_hung_child_without_false_pass(tmp_path):
    import subprocess
    import sys
    import time
    from app.evaluation.agent.run import run_isolated
    case = scenario()
    case.inputs.budget.seconds = .1
    def popen(command, **kwargs):
        # A real separate process that stalls; no model credentials or business connections.
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
    start = time.monotonic()
    cap = run_isolated(case, 1, "test", tmp_path / "capture.json", popen=popen, startup_grace=.2)
    assert time.monotonic()-start < 5
    assert cap.error == "supervisor_timeout"
    assert not cap.started and not grade(case, cap)["passed"]


def test_child_gets_inputs_not_labels(tmp_path):
    from app.evaluation.agent.run import run_isolated
    class Process:
        returncode = 0
        def communicate(self, data=None, **kwargs):
            payload = json.loads(data)
            assert "expected" not in payload and "inputs" in payload
            cap = Capture(run_id=payload["run_id"], scenario_id=payload["scenario_id"],
                input_digest=digest(payload["inputs"]), repetition=payload["repetition"], source="live_model")
            path.write_text(cap.model_dump_json(), encoding="utf-8")
        def poll(self):
            return 0
    path = tmp_path / "capture.json"
    cap = run_isolated(scenario(), 1, "test", path, popen=lambda *a, **k: Process())
    assert not cap.started


@pytest.mark.parametrize("value", ["not json", '{"results":[{"id":"facts","status":"pass","reason":"ok","citations":[]}]}',
    '{"results":[{"id":"facts","status":"pass","reason":"ok","citations":[{"source":"replies","index":99,"path":["content"]}]}]}'])
def test_semantic_judge_rejects_invalid_or_unreferenced_verdicts(value):
    from app.evaluation.agent.judge import SemanticJudge
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: response(value))))
    case = scenario()
    case.expected.reply_accuracy.rubrics = [rubric()]
    judge = SemanticJudge(client, "judge-test")
    assert judge(case, synthetic(), "reply_accuracy")[0]["status"] == "error"


def test_semantic_judge_requires_real_reply_and_fact_references():
    from app.evaluation.agent.judge import SemanticJudge
    value = json.dumps(dict(results=[dict(id="facts", status="pass", reason="test fixture", citations=[
        dict(source="replies", index=0, path=["content"]),
        dict(source="tool_calls", index=0, path=["result", "success"])])]))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: response(value))))
    case = scenario()
    case.expected.reply_accuracy.rubrics = [rubric()]
    assert SemanticJudge(client, "judge-test")(case, synthetic(), "reply_accuracy")[0]["status"] == "pass"


def test_full_driver_in_real_child_process(tmp_path):
    import subprocess
    import sys
    from app.evaluation.agent.run import run_isolated
    script = """
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
import sys
from app.config.settings import settings
from app.evaluation.agent.run import child
from app.evaluation.agent.runtime import run_inputs
from tests.test_agent_e2e import ScriptedClient
def scripted(*args, **kwargs):
    kwargs['source'] = 'scripted_regression'
    return run_inputs(*args, **kwargs)
with patch.object(settings, 'openai_api_key', 'test-only'), patch('openai.OpenAI', lambda **k: nullcontext(ScriptedClient())), patch('app.evaluation.agent.runtime.run_inputs', scripted):
    raise SystemExit(child(Path(sys.argv[1])))
"""
    def popen(command, **kwargs):
        return subprocess.Popen([sys.executable, "-c", script, command[-1]], **kwargs)
    case = scenario()
    cap = run_isolated(case, 1, "test", tmp_path / "capture.json", popen=popen)
    assert cap.source == "scripted_regression"
    assert grade(case, cap)["passed"], (cap.termination, cap.error, grade(case, cap))


def test_stateful_backend_does_not_accept_other_user_stale_state_or_double_write():
    from app.evaluation.agent.business import Business
    from app.services.commerce.common import BusinessError
    case = scenario()
    raw = case.inputs.model_dump()
    raw.update(objects=dict(a=dict(user_id="alice", order_id="ORD-A", platform_code="pdd", status="pending")),
        writes=[dict(tool="prepare_order_cancel", user="alice", arguments=dict(order_ref=ref("order", "pdd", "ORD-A")),
            state_key="a", changes=dict(status="cancelled"), review=dict(order_id="ORD-A", action="prepare_order_cancel"),
            result_status="cancelled")])
    records = []
    backend = Business(Inputs.model_validate(raw), SimpleNamespace(add=lambda source, row: records.append((source, row))))
    svc = backend.service("alice")
    args = dict(order_ref=ref("order", "pdd", "ORD-A"))
    prepared = svc.preview("prepare_order_cancel", args)
    with pytest.raises(BusinessError):
        backend.service("bob").preview("prepare_order_cancel", args)
    backend.objects["a"]["status"] = "changed"
    with pytest.raises(BusinessError):
        svc.submit(prepared["action"], prepared["arguments"], "op1", prepared["business_version"])
    prepared = svc.preview("prepare_order_cancel", args)
    one = svc.submit(prepared["action"], prepared["arguments"], "op1", prepared["business_version"])
    assert svc.submit(prepared["action"], prepared["arguments"], "op1", prepared["business_version"]) == one
    assert len([r for s, r in records if s == "writes"]) == 1
    assert backend.objects["a"]["status"] == "cancelled"


def test_stage_scenarios_and_execution_sources_have_separate_denominators():
    case, cap = scenario(), synthetic()
    stage = case.model_copy(deep=True, update=dict(id="stage", kind="stage"))
    stage_cap = cap.model_copy(deep=True, update=dict(run_id="stage", scenario_id="stage", source="live_model"))
    report = evaluate(Suite(name="separated", description="test", scenarios=[case, stage]), [cap, stage_cap], 1)
    assert set(report["groups"]["scripted_regression"]) == {"complete"}
    assert set(report["groups"]["live_model"]) == {"stage"}


class ContinueClient(ScriptedClient):
    def create(self, **kwargs):
        if self.scene == "association":
            data = json.loads(kwargs["messages"][-1]["content"])
            message, candidate = data["current_messages"][-1], data["candidates"][0]
            return response(json.dumps(dict(decisions=[dict(sequences=[message["sequence"]], action="update",
                effect="supplement", task_id=candidate["task_id"], task_version=candidate["version"], evidence=[
                    dict(session_id=data["session_id"], sequence=message["sequence"], quote=message["content"])])])))
        return super().create(**kwargs)


def test_cross_session_continuation_keeps_seed_identity():
    from app.evaluation.agent.contracts import Seed
    case = scenario()
    case.inputs.sessions["old"] = "alice"
    case.inputs.seeds.append(Seed(alias="original", session="old", goal="查 pdd 订单 ORD-A 的物流",
                                 details=dict(order_id="ORD-A", platform_code="pdd")))
    cap = run_inputs(case.inputs, client=ContinueClient(), model="test", run_id=uuid4().hex, scenario_id=case.id)
    assert cap.termination == "settled", cap.model_dump()
    assert len(cap.observations["tasks"]) == 1
    assert cap.observations["tasks"][0]["alias"] == "original"
    assert grade(case, cap)["passed"], grade(case, cap)
