# 文件说明：测试动态用户、多会话记忆、行为复核和真实确认的隔离评测流程。
"""Offline dynamic trajectories through actual intake, worker, memory and confirmation APIs."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest

from app.evaluation.agent.contracts import Inputs, Suite, digest
from app.evaluation.agent.grading import evaluate, grade
from app.evaluation.agent.memory import MemoryObserver, clone_store, memory_environment, snapshot_digest
from app.evaluation.agent.simulation import run_simulation
from app.evaluation.agent.simulator import Calls, UserSimulator, SimulatorInvalid
from app.evaluation.agent.sandbox import isolated_store, message
from tests.test_agent_e2e import ScriptedClient, response, scenario, synthetic
from tests.test_memory_v3 import change


class UserClient:
    def __init__(self, invalid=False, confirm=False):
        self.materials, self.invalid, self.confirm = [], invalid, confirm
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        data = json.loads(kwargs["messages"][-1]["content"])
        self.materials.append(deepcopy(data))
        if "proposed_action" in data:
            return response(json.dumps(dict(status="invalid" if self.invalid else "valid", reason="fixture")))
        public = data["public"]
        if not public["messages"]:
            facts = data["facts"]
            text = data["goal"]
            disclosures = []
            for key, fact in facts.items():
                if fact["disclosure"] == "anytime":
                    text += "。" + fact["value"]
                    disclosures.append(dict(fact_id=key, quote=fact["value"]))
            action = dict(action="message", text=text, disclosures=disclosures)
        elif self.confirm and any(c.get("can_confirm") for c in public["visible_cards"]):
            card = next(c for c in public["visible_cards"] if c.get("can_confirm"))
            action = dict(action="confirm", operation_id=card["operation_id"])
        else:
            action = dict(action="end")
        return response(json.dumps(dict(action, reason="fixture")))


class MemoryClient:
    def __init__(self):
        self.agent = ScriptedClient()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def bind_scene(self, scene):
        return self.agent.bind_scene(scene)

    def create(self, **kwargs):
        data = json.loads(kwargs["messages"][-1]["content"])
        new = [m for m in data["messages"] if m["is_new"]]
        changes = []
        for m in new:
            if "以后衣服喜欢蓝色" in m["content"]:
                changes.append(change(m["message_id"], "以后衣服喜欢蓝色", "blue"))
            if "忘掉衣服蓝色偏好" in m["content"]:
                changes.append(change(m["message_id"], "忘掉衣服蓝色偏好", "blue", action="FORGET"))
        return response(json.dumps(dict(schema_version=3, changes=changes)))


def dynamic_case(paired=False):
    case = scenario()
    raw = case.inputs.model_dump()
    raw["steps"] = []
    raw["sessions"] = {"s": "alice", "later": "alice"}
    raw["simulation"] = dict(memory="paired" if paired else "off", warmup_sessions=1 if paired else 0,
        phases=[dict(id="first", session="s", goal="查 pdd 订单 ORD-A 的物流", behavior="按事实回答",
                     facts={"color": dict(value="以后衣服喜欢蓝色", disclosure="anytime")} if paired else {})])
    if paired:
        raw["simulation"]["phases"].append(dict(id="later", session="later", goal="查 pdd 订单 ORD-A 的物流", behavior="只回答本轮问题"))
    raw["budget"].update(seconds=30.0, model_calls=60)
    case.inputs = Inputs.model_validate(raw)
    return case


def run(case, user=None, client=None, persist=None):
    return run_simulation(case.inputs, client=client or ScriptedClient(), user_client=user or UserClient(),
        model="agent-fixture", user_model="user-fixture", run_id="test-run", scenario_id=case.id, persist=persist)


def test_dynamic_real_worker_and_hidden_information(tmp_path):
    case, user = dynamic_case(), UserClient()
    cap = run(case, user)[0]
    assert cap.termination == "settled", cap.model_dump()
    assert all(cap.complete.values())
    assert grade(case, cap)["passed"]
    assert len(cap.observations["user_actions"]) == 2
    assert cap.observations["sessions"][0]["turns"] == 1
    for material in user.materials:
        assert not ({"expected", "rubrics", "objects", "reads", "writes", "observations", "phases"} & material.keys())
        assert all(m["role"] in {"user", "assistant"} for m in material["public"]["messages"])
    report = evaluate(Suite(schema_version=4, name="dynamic", description="fixture", scenarios=[case]), [cap], 1)
    assert report["experiments"]["valid_runs"]["scripted_regression:single"]["valid"] == 1
    from app.evaluation.agent.contracts import Captures
    from app.evaluation.agent.run import main
    suite = Suite(schema_version=4, name="replay", description="", scenarios=[case])
    (tmp_path/"suite.json").write_text(suite.model_dump_json(), encoding="utf-8")
    (tmp_path/"traces.json").write_text(Captures(schema_version=4, captures=[cap]).model_dump_json(), encoding="utf-8")
    assert main(["--dataset", str(tmp_path/"suite.json"), "--traces", str(tmp_path/"traces.json"),
        "--repetitions", "1", "--output", str(tmp_path/"result")]) == 0


def test_simulator_rejection_is_retained_and_never_passes():
    case = dynamic_case()
    cap = run(case, UserClient(invalid=True))[0]
    assert cap.simulator_validity == "invalid"
    assert cap.termination == "not_started"
    assert cap.observations["user_actions"][0]["review"]["status"] == "invalid"
    assert not grade(case, cap)["passed"]


def test_real_confirmation_via_dynamic_visible_card():
    case = dynamic_case()
    from app.services.commerce.common import ref
    raw = case.inputs.model_dump()
    raw["objects"] = {"a": dict(user_id="alice", order_id="ORD-A", platform_code="pdd", status="pending_payment")}
    raw["reads"] = []
    raw["writes"] = [dict(tool="prepare_order_cancel", user="alice",
        arguments=dict(order_ref=ref("order", "pdd", "ORD-A")), state_key="a",
        changes=dict(status="cancelled"), review=dict(order_id="ORD-A", action="prepare_order_cancel"), result_status="cancelled")]
    raw["simulation"]["phases"][0].update(goal="取消 pdd 订单 ORD-A",
        confirmations=[dict(review=dict(order_id="ORD-A", action="prepare_order_cancel"))])
    case.inputs = Inputs.model_validate(raw)
    user = UserClient(confirm=True)
    cap = run(case, user, ScriptedClient("prepare_order_cancel", dict(order_ref=ref("order", "pdd", "ORD-A"))))[0]
    assert cap.termination == "settled", cap.model_dump()
    assert len(cap.observations["writes"]) == 1
    assert cap.observations["objects"][0]["status"] == "cancelled"
    assert any(a["action"]["action"] == "confirm" for a in cap.observations["user_actions"])
    assert "confirmation_token" not in json.dumps(user.materials)


def test_unknown_card_and_first_action_are_rejected():
    case = dynamic_case()
    calls = SimpleNamespace(model="test", json=lambda *args: __import__(
        "app.evaluation.agent.simulator", fromlist=["UserAction"]).UserAction(
            action="confirm", operation_id="invented", reason="fixture"))
    driver = UserSimulator(case.inputs.simulation.phases[0], "alice", calls)
    with isolated_store(sessions=[("s", "alice")]) as store:
        with pytest.raises(SimulatorInvalid):
            driver.next_step(None, store)
    assert driver.actions[-1]["status"] == "invalid"


def test_paired_snapshot_actual_memory_and_replay():
    case = dynamic_case(paired=True)
    from app.evaluation.agent.contracts import MemoryTarget
    case.memory_targets = [MemoryTarget(id="color", phase_id="later", disclosed_phase="first", fact_id="color",
        predicate="prefers_color", value="blue", qualifiers={"product_category": "clothing"})]
    partial = []
    def persist(cs):
        assert len(cs) == 2
        partial.append(any(c.metadata.get("warmup", {}).get("metadata", {}).get("inflight", {}).get("started") for c in cs))
    caps = run(case, client=MemoryClient(), persist=persist)
    assert any(partial), "Warmup must persist partial worker evidence before either arm starts"
    assert [c.branch for c in caps] == ["memory_off", "memory_on"]
    assert all(c.termination == "settled" and all(c.complete.values()) for c in caps), [c.model_dump() for c in caps]
    assert caps[0].metadata["snapshot_digest"] == caps[1].metadata["snapshot_digest"]
    for cap in caps:
        assert cap.metadata["latency"]["requests"]
        assert {r["phase_id"] for r in cap.metadata["latency"]["requests"]} == {"later"}
        snapshots = [r for r in cap.observations["memory"] if r["kind"] == "snapshot" and r["stage"] == "start"]
        assert any(f["payload"]["value"] == "blue" for f in snapshots[0]["facts"])
        # No transcript copied into the new simulator session.
        assert cap.observations["user_actions"][0]["public"]["messages"] == []
        assert cap.metadata["warmup"]["observations"]["user_actions"][0]["action"]["disclosures"]
    suite = Suite(schema_version=4, name="pair", description="fixture", scenarios=[case])
    report = evaluate(suite, caps, 1)
    assert report["experiments"]["paired_summary"]["scripted_regression"]["valid_pairs"] == 1
    assert all(m["observed_retrieval_calls"] == 0 for m in report["experiments"]["memory"].values())
    assert all(m["storage"]["rate"] == 1.0 and m["recall"]["rate"] is None for m in report["experiments"]["memory"].values())
    undisclosed = deepcopy(caps)
    for cap in undisclosed:
        cap.metadata["warmup"]["observations"]["user_actions"][0]["action"]["disclosures"] = []
    report_without_disclosure = evaluate(suite, undisclosed, 1)
    assert all(m["storage"]["scored"] == 0 for m in report_without_disclosure["experiments"]["memory"].values())
    with pytest.raises(ValueError, match="Missing"):
        evaluate(suite, caps[:1], 1)
    wrong = deepcopy(caps)
    wrong[1].metadata["snapshot_digest"] = "another"
    with pytest.raises(ValueError, match="snapshot"):
        evaluate(suite, wrong, 1)
    failed = deepcopy(caps)
    failed[1].simulator_validity = "invalid"
    report = evaluate(suite, failed, 1)
    assert report["experiments"]["paired_summary"]["scripted_regression"]["valid_pairs"] == 0
    assert report["groups"]["scripted_regression:memory_on"]["complete"]["planned"] == 1
    missing = deepcopy(caps)
    missing[1].complete = {}
    report = evaluate(suite, missing, 1)
    assert report["experiments"]["paired_summary"]["scripted_regression"]["valid_pairs"] == 0


def test_dynamic_supervisor_keeps_both_planned_arms(tmp_path):
    import subprocess
    import sys
    from app.evaluation.agent.run import run_dynamic_isolated
    case = dynamic_case(paired=True)
    case.inputs.budget.seconds = .1
    def popen(command, **kwargs):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
    caps = run_dynamic_isolated(case, 1, "agent", tmp_path/"capture.json", user_model="user",
        popen=popen, startup_grace=.2)
    assert len(caps) == 2
    assert all(not c.started and c.error == "supervisor_timeout" for c in caps)
    report = evaluate(Suite(schema_version=4, name="timeout", description="", scenarios=[case]), caps, 1)
    assert report["experiments"]["paired_summary"]["live_model"]["planned_pairs"] == 1
    assert report["experiments"]["paired_summary"]["live_model"]["valid_pairs"] == 0


def test_memory_archive_read_gate_forget_and_branch_isolation(tmp_path):
    deadline = time.monotonic()+20
    calls = Calls(MemoryClient(), "memory-fixture", 10, deadline)
    with isolated_store(sessions=[("s", "alice")]) as store, memory_environment(tmp_path, True):
        message(store, "以后衣服喜欢蓝色")
        observer = MemoryObserver(True, calls)
        observer.phase_id = "warmup"
        observer.archive(store, "alice", "s")
        signature = snapshot_digest(store, {})
        other = clone_store(store, tmp_path/"clone.db")
        assert snapshot_digest(other, {}) == signature
        from app.memory.v3.manager import build_manager
        blocked = MemoryObserver(False, calls)
        with blocked.installed():
            assert build_manager("alice", store).retrieve_relevant_long_term_memory("我的衣服颜色偏好") == []
        assert blocked.records[-1]["blocked"]
        with observer.installed():
            facts = build_manager("alice", store).retrieve_relevant_long_term_memory("我的偏好")
            assert any(f.value == "blue" for f in facts)
        message(other, "忘掉衣服蓝色偏好")
        observer.archive(other, "alice", "s")
        assert not build_manager("alice", other).ltm.facts
        assert any(f.value == "blue" for f in build_manager("alice", store).ltm.facts)
        assert snapshot_digest(store, {}) == signature


def test_memory_failure_keeps_receipt_and_stops_next_session(tmp_path):
    bad = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response("bad-json"))))
    with isolated_store(sessions=[("s", "alice")]) as store, memory_environment(tmp_path, True):
        message(store, "以后衣服喜欢蓝色")
        obs = MemoryObserver(True, Calls(bad, "bad", 5, time.monotonic()+10))
        with pytest.raises(RuntimeError, match="archive"):
            obs.archive(store, "alice", "s")
        archive = next(r for r in obs.records if r["kind"] == "archive")
        assert archive["barrier"]["status"] == "retrying"
        assert archive["counts"]["failed"] == 1


def test_simulator_budget_is_shared_with_adherence_review():
    case = dynamic_case()
    case.inputs.budget.simulator_calls = 1
    cap = run(case)[0]
    assert cap.simulator_validity == "error"
    assert not cap.started
    assert cap.metadata["usage"]["simulator"] == 1
    assert not grade(case, cap)["passed"]


def test_legacy_input_hash_remains_replayable():
    case, cap = scenario(), synthetic()
    legacy = case.inputs.model_dump(exclude={"simulation"})
    for key in ("simulator_calls", "memory_calls"):
        legacy["budget"].pop(key)
    cap.input_digest = digest(legacy)
    assert grade(case, cap)["passed"]


def test_dynamic_cli_requires_user_model_before_output(tmp_path):
    from app.evaluation.agent.run import main
    dataset = tmp_path/"suite.json"
    dataset.write_text(Suite(schema_version=4, name="fixture", description="", scenarios=[dynamic_case()]).model_dump_json(), encoding="utf-8")
    out = tmp_path/"result"
    assert main(["--dataset", str(dataset), "--live-model", "--output", str(out)]) == 2
    assert not out.exists()
