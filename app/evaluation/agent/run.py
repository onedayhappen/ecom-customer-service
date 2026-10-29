"""Dynamic user simulation and paired memory evaluation; static v3 replay supported."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from .contracts import DIMENSIONS, Capture, Captures, Inputs, Suite, digest
from .grading import evaluate

ROOT = Path(__file__).resolve().parents[3]


def provenance():
    files = {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p.read_text(encoding="utf-8-sig"))
             for p in sorted((ROOT / "app").rglob("*.py"))}
    files["requirements.txt"] = digest((ROOT / "requirements.txt").read_text(encoding="utf-8-sig"))
    return dict(source_digest=digest(files), python=sys.version.split()[0])


def isolated_env():
    # Clear dependencies before settings/import side effects in the child. Model credentials stay local.
    env = dict(os.environ)
    env.update(COMMERCE_PROVIDER_CONFIG="{}", ORDER_DB_DSN="", ORDER_CACHE_REDIS_URL="",
        API_SESSION_POSTGRES_DSN="", API_CONCURRENCY_REDIS_URL="", MEMORY_POSTGRES_DSN="",
        MEMORY_REDIS_URL="", MEMORY_ENABLED="false", SHORT_TERM_MEMORY_ENABLED="false",
        RETRIEVAL_SERVICE_URL="", MCP_ENABLED="false", OBSERVABILITY_ENABLED="false", AUDIT_ENABLED="false")
    return env


def run_isolated(scenario, repetition, model, path, *, popen=subprocess.Popen, startup_grace=10):
    run_id = uuid4().hex
    initial = Capture(run_id=run_id, scenario_id=scenario.id, input_digest=digest(scenario.inputs.model_dump()),
        repetition=repetition, source="live_model", metadata=dict(model=model))
    # Deliberately omit expected labels; subprocess input is the entire information boundary.
    payload = dict(inputs=scenario.inputs.model_dump(), model=model, run_id=run_id,
                   scenario_id=scenario.id, repetition=repetition)
    version = provenance()
    path = path.resolve()
    process = popen([sys.executable, "-m", "app.evaluation.agent.run", "--child", str(path)],
        cwd=ROOT, env=isolated_env(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    timed_out = False
    try:
        process.communicate(json.dumps(payload, ensure_ascii=False), timeout=scenario.inputs.budget.seconds + startup_grace)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    try:
        capture = Capture.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else initial
    except (OSError, ValueError):
        capture = initial
        capture.error = "invalid_child_capture"
    if capture.run_id != run_id or capture.input_digest != initial.input_digest:
        raise ValueError("Child capture identity mismatch")
    if timed_out or process.returncode != 0:
        capture.complete = {k: False for k in capture.complete}
        capture.error = "supervisor_timeout" if timed_out else "child_failed"
        capture.termination = "timeout" if capture.started and timed_out else "error" if capture.started else "not_started"
    if not path.exists() and not capture.error:
        capture.error = "child_did_not_capture"
    after = provenance()
    capture.metadata.update(version, source_changed_during_run=version != after)
    if version != after:
        capture.complete = {k: False for k in capture.complete}
        capture.error = "source_changed_during_run"
    path.write_text(capture.model_dump_json(indent=2), encoding="utf-8")
    return capture


def run_dynamic_isolated(scenario, repetition, model, path, *, user_model, reviewer_model=None,
                         memory_model=None, popen=subprocess.Popen, startup_grace=10):
    from .runtime import save_capture
    run_id = uuid4().hex
    branches = ("memory_off", "memory_on") if scenario.inputs.simulation.memory == "paired" else ("single",)
    initial = [Capture(schema_version=4, run_id=run_id+":"+b, scenario_id=scenario.id,
        input_digest=digest(scenario.inputs.model_dump()), repetition=repetition,
        source="live_model", branch=b, simulator_validity="error", metadata=dict(model=model, user_model=user_model)) for b in branches]
    payload = dict(inputs=scenario.inputs.model_dump(), model=model, user_model=user_model,
        reviewer_model=reviewer_model, memory_model=memory_model, run_id=run_id,
        scenario_id=scenario.id, repetition=repetition)
    version = provenance()
    path = path.resolve()
    save_capture(path, Captures(schema_version=4, captures=initial))
    process = popen([sys.executable, "-m", "app.evaluation.agent.run", "--child", str(path)],
        cwd=ROOT, env=isolated_env(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    timed_out = False
    try:
        # Warmup plus two arms are sequential, each trajectory has the same fixed budget.
        process.communicate(json.dumps(payload, ensure_ascii=False),
            timeout=scenario.inputs.budget.seconds * (3 if len(branches) == 2 else 1) + startup_grace)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    invalid = False
    try:
        found = Captures.model_validate_json(path.read_text(encoding="utf-8")).captures
        if len({c.branch for c in found}) != len(found):
            raise ValueError("Duplicate child branches")
        for c in found:
            if (c.branch not in branches or c.run_id != run_id+":"+c.branch or c.scenario_id != scenario.id
                or c.input_digest != initial[0].input_digest or c.repetition != repetition or c.source != "live_model"):
                raise ValueError("Child capture identity mismatch")
    except (OSError, ValueError):
        found, invalid = [], True
    captures = []
    after = provenance()
    for original in initial:
        c = next((c for c in found if c.branch == original.branch), original)
        missing = not any(c.branch == x.branch for x in found) or not c.started
        if invalid or timed_out or process.returncode != 0 or missing or version != after:
            c.complete = {k: False for k in c.complete}
            c.error = ("invalid_child_capture" if invalid else "supervisor_timeout" if timed_out else
                "source_changed_during_run" if version != after else c.error or "child_failed_or_missing")
            c.termination = ("timeout" if timed_out else "error") if c.started else "not_started"
            c.metadata["error_category"] = "supervisor"
        c.metadata.update(version, source_changed_during_run=version != after)
        captures.append(c)
    save_capture(path, Captures(schema_version=4, captures=captures))
    return captures

def child(path):
    from .runtime import run_inputs, save_capture
    from app.config.settings import settings
    from openai import OpenAI
    payload = json.load(sys.stdin)
    inputs = Inputs.model_validate(payload.pop("inputs"))
    if inputs.simulation:
        from .simulation import run_simulation
        if not settings.openai_api_key:
            return 2
        with OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                    max_retries=0, timeout=min(inputs.budget.seconds, 30)) as client, OpenAI(
                    api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                    max_retries=0, timeout=min(inputs.budget.seconds, 30)) as user_client:
            captures = run_simulation(inputs, client=client, user_client=user_client, source="live_model", **payload,
                persist=lambda cs: save_capture(path, Captures(schema_version=4, captures=cs)))
            save_capture(path, Captures(schema_version=4, captures=captures))
        return 0
    initial = Capture(run_id=payload["run_id"], scenario_id=payload["scenario_id"],
        input_digest=digest(inputs.model_dump()), repetition=payload["repetition"], source="live_model")
    save_capture(path, initial)
    if not settings.openai_api_key:
        initial.error = "missing_model_credentials"
        save_capture(path, initial)
        return 2
    with OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                max_retries=0, timeout=min(inputs.budget.seconds, 30)) as client:
        capture = run_inputs(inputs, client=client, source="live_model", **payload,
                             persist=lambda c: save_capture(path, c))
        save_capture(path, capture)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--print-schema", action="store_true")
    parser.add_argument("--traces", type=Path)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--user-model", help="Required for live dynamic scenarios")
    parser.add_argument("--reviewer-model", help="Simulator adherence reviewer; defaults to user model")
    parser.add_argument("--memory-model", help="Memory extractor; defaults to Agent model")
    parser.add_argument("--judge-model", help="Required for datasets with rubrics; uses the configured model endpoint")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, help="New output directory; never overwrites prior results")
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child(args.child)
    if args.print_schema:
        print(json.dumps(Suite.model_json_schema(), ensure_ascii=False, indent=2))
        return 0
    if not args.dataset or bool(args.traces) == args.live_model or args.repetitions < 1:
        parser.error("Specify --dataset and exactly one of --live-model / --traces; repetitions must be positive")
    client = None
    try:
        suite = Suite.model_validate_json(args.dataset.read_text(encoding="utf-8-sig"))
        if args.live_model and any(s.inputs.simulation for s in suite.scenarios) and not args.user_model:
            print("Dynamic execution requires --user-model; no model calls were made.")
            return 2
        if not args.judge_model and any(getattr(s.expected, d).rubrics for s in suite.scenarios for d in DIMENSIONS):
            print("Dataset rubrics require --judge-model; no Agent or judge calls were made.")
            return 2
        captures = Captures.model_validate_json(args.traces.read_text(encoding="utf-8-sig")).captures if args.traces else None
        # Validate the complete replay matrix before creating files or invoking a paid judge.
        if captures is not None:
            evaluate(suite, captures, args.repetitions)
        model = args.model
        if args.live_model or args.judge_model:
            from app.config.settings import settings
            if not settings.openai_api_key:
                raise ValueError("Missing model credentials")
            model = model or settings.deepseek_model_name
        directory = args.output or Path("output/agent-e2e") / (datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=False)
        if captures is None:
            captures = []
            for i, s in enumerate(suite.scenarios):
                for r in range(1, args.repetitions+1):
                    path = directory / f"capture-{i}-{r}.json"
                    if s.inputs.simulation:
                        captures.extend(run_dynamic_isolated(s, r, model, path, user_model=args.user_model,
                            reviewer_model=args.reviewer_model, memory_model=args.memory_model))
                    else:
                        captures.append(run_isolated(s, r, model, path))
        judge = None
        if args.judge_model:
            from openai import OpenAI
            from .judge import SemanticJudge
            client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, max_retries=0, timeout=30)
            judge = SemanticJudge(client, args.judge_model)
        report = evaluate(suite, captures, args.repetitions, judge=judge)
        report.update(evaluated_at=datetime.now(timezone.utc).isoformat(), evaluator=provenance(),
            mode="replay" if args.traces else "service_entry", capture_digest=digest([c.model_dump() for c in captures]),
            execution_metadata={c.run_id: {k: c.metadata.get(k) for k in ("model", "source_digest", "python", "calls")} for c in captures},
            scope="Isolated service-entry evaluation with simulated external business services; not production success rate")
        (directory / "captures.json").write_text(Captures(schema_version=4 if suite.schema_version == 4 else 2,
            captures=captures).model_dump_json(indent=2), encoding="utf-8")
        if judge:
            (directory / "judge.json").write_text(json.dumps(judge.records, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps(dict(resume_summary=report["resume_summary"], output=str(directory.resolve())), ensure_ascii=False, indent=2))
        return 0 if all(r["passed"] for r in report["results"]) else 1
    except (ValueError, OSError) as exc:
        print("Evaluation configuration/artifact error: " + type(exc).__name__)
        return 2
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
