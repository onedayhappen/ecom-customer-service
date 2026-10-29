"""Latency boundaries and reporting with deterministic clocks and real Agent execution."""
from copy import deepcopy
from types import SimpleNamespace

from app.evaluation.agent.contracts import DIMENSIONS, Suite
from app.evaluation.agent.grading import evaluate
from app.evaluation.agent.latency import ResponseTimer, statistics
from tests.test_agent_e2e import scenario, synthetic
from tests.test_agent_simulation import UserClient, dynamic_case, run


def test_response_boundaries_ignore_user_wait_and_duplicate_events():
    now = [0.0]
    timer = ResponseTimer(lambda: now[0])
    timer.reply("s", 1)  # Pre-existing history is never a new response.
    timer.begin(SimpleNamespace(id="q1", session="s", action="message"))
    now[0] = 2.0
    timer.reply("other", 2)
    timer.reply("s", 3)
    now[0] = 3.0
    timer.reply("s", 3)
    timer.reply("s", 4)
    now[0] = 4.0
    timer.settle()
    now[0] = 100.0  # User generation / think time.
    timer.begin(SimpleNamespace(id="q2", session="s", action="message"))
    now[0] = 105.0
    timer.stop("timeout")
    first, second = timer.snapshot()["requests"]
    assert first["first_reply_seconds"] == 2.0
    assert first["last_reply_seconds"] == 3.0
    assert first["settled_seconds"] == 4.0
    assert first["reply_events"] == [3, 4]
    assert second["first_reply_seconds"] is None
    assert second["settled_seconds"] is None and second["observed_seconds"] == 5.0


def test_summary_keeps_timeouts_missing_history_and_excludes_overlap():
    case, base = scenario(), synthetic()
    suite = Suite(name="latency", description="", scenarios=[case])
    timer = ResponseTimer(lambda: 0.0)
    timer.begin(SimpleNamespace(id="q1", session="s", action="message"))
    timer.begin(SimpleNamespace(id="q2", session="s", action="message"))
    timer.reply("s", 1)
    timer.settle()
    overlap = deepcopy(base)
    overlap.run_id, overlap.repetition = "overlap", 2
    overlap.metadata["latency"] = timer.snapshot()
    timed = deepcopy(base)
    timed.run_id, timed.repetition, timed.termination = "timeout", 3, "timeout"
    timed.metadata["latency"] = dict(schema_version=1, requests=[dict(
        first_reply_seconds=2.0, settled_seconds=None, status="timeout", overlapping=False)])
    report = evaluate(suite, [base, overlap, timed], 3)
    summary = next(iter(report["resume_summary"]["groups"].values()))
    assert set(summary["quality"]) == set(DIMENSIONS)
    assert summary["quality"]["task_completion"]["planned"] == 3
    assert summary["quality"]["task_completion"]["pass_rate"] == 2/3
    latency = summary["latency"]
    assert latency["runs_missing_timing"] == 1
    assert latency["overlapping_requests"] == 2
    assert latency["timeout_runs"] == 1 and latency["unfinished_requests"] == 1
    assert latency["first_reply"]["n"] == 1 and latency["first_reply"]["mean_seconds"] == 2.0
    assert latency["service_settled"]["mean_seconds"] is None
    assert statistics([])["p95_seconds"] is None
    assert statistics(list(range(1, 101)))["p95_seconds"] == 95


def test_dynamic_runtime_excludes_simulator_and_review_time(monkeypatch):
    from app.evaluation.agent import runtime
    clock = [0.0]
    monkeypatch.setattr(runtime, "ResponseTimer", lambda: ResponseTimer(lambda: clock[0]))
    original = runtime.MeteredClient.create
    def agent_call(self, **kwargs):
        clock[0] += .25
        return original(self, **kwargs)
    monkeypatch.setattr(runtime.MeteredClient, "create", agent_call)
    class SlowUser(UserClient):
        def create(self, **kwargs):
            clock[0] += 100.0
            return super().create(**kwargs)
    cap = run(dynamic_case(), SlowUser())[0]
    assert cap.termination == "settled"
    rows = cap.metadata["latency"]["requests"]
    assert len(rows) == 1 and rows[0]["phase_id"] == "first"
    assert clock[0] >= 400.0  # Generate/review both the request and end action.
    assert 0 < rows[0]["first_reply_seconds"] <= rows[0]["settled_seconds"] < 100
    assert rows[0]["settled_seconds"] == len(cap.observations["models"])*.25


def test_legacy_capture_has_unscored_latency_and_replay_is_stable():
    case, cap = scenario(), synthetic()
    suite = Suite(name="legacy", description="", scenarios=[case])
    summary = evaluate(suite, [cap], 1)["resume_summary"]
    assert summary == evaluate(suite, [cap], 1)["resume_summary"]
    latency = next(iter(summary["groups"].values()))["latency"]
    assert latency["runs_missing_timing"] == 1
    assert latency["first_reply"]["n"] == 0 and latency["first_reply"]["mean_seconds"] is None
