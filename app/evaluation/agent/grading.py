# 文件说明：执行事实检查与 rubric 汇总，校验评分证据并区分失败和未评分。
"""Fail-closed scenario grading. No production claim renderer is used as an oracle."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .contracts import DIMENSIONS, Capture, Scenario, collections_for, digest


class EvidenceError(ValueError):
    pass


def lookup(row, path):
    for key in path:
        if type(key) is int and isinstance(row, list) and 0 <= key < len(row):
            row = row[key]
        elif isinstance(key, str) and isinstance(row, dict) and key in row:
            row = row[key]
        else:
            raise EvidenceError("Missing evidence field: " + ".".join(map(str, path)))
    return row


def equal(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(b, dict):
        return len(a) == len(b) and all(k in a and equal(a[k], v) for k, v in b.items())
    if isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    return a == b


def matches(row, predicate):
    actual, expected = lookup(row, predicate.path), predicate.value
    if predicate.op == "equals":
        return equal(actual, expected)
    if predicate.op == "one_of":
        return any(equal(actual, option) for option in expected)
    if predicate.op == "contains":
        if isinstance(expected, str):
            return isinstance(actual, str) and expected in actual
        return isinstance(actual, dict) and all(k in actual and equal(actual[k], v) for k, v in expected.items())
    if predicate.op == "decimal_equal":
        try:
            if type(actual) not in {str, int, float}:
                raise ValueError()
            a, b = Decimal(str(actual)), Decimal(expected)
            if not a.is_finite() or not b.is_finite():
                raise ValueError()
            return a == b
        except (ValueError, InvalidOperation):
            raise EvidenceError("Invalid numeric evidence") from None
    if predicate.op == "instant_equal":
        try:
            a, b = datetime.fromisoformat(actual), datetime.fromisoformat(expected)
            if a.tzinfo is None or b.tzinfo is None:
                raise ValueError()
            return a.astimezone(timezone.utc) == b.astimezone(timezone.utc)
        except (ValueError, TypeError):
            raise EvidenceError("Instants require explicit timezone") from None
    raise EvidenceError("Unsupported predicate")


def selected(row, predicates):
    # Ordered selectors permit checking event kind before kind-specific fields.
    return all(matches(row, predicate) for predicate in predicates)


def check_one(check, capture):
    try:
        if capture.complete.get(check.source) is not True or check.source not in capture.observations:
            raise EvidenceError("Collection incomplete: " + check.source)
        rows = list(enumerate(capture.observations[check.source]))
        if check.source in {"models", "tool_calls"} and any(r.get("finished") is not True for _, r in rows):
            raise EvidenceError("Collection contains unfinished calls")
        for step, after in ((check.after_step, True), (check.before_step, False)):
            if step is not None:
                if step not in capture.boundaries:
                    raise EvidenceError("User step was not committed: " + step)
                boundary = capture.boundaries[step]
                rows = [(i, r) for i, r in rows if (lookup(r, ["_seq"]) > boundary if after
                                                   else lookup(r, ["_seq"]) < boundary)]
        found = [(i, r) for i, r in rows if selected(r, check.where)]
        verdicts = [selected(r, check.assertions) for _, r in found]
        passed = (any(verdicts) if check.quantifier == "exists" else
                  bool(found) and all(verdicts) if check.quantifier == "all" else
                  not found if check.quantifier == "none" else len(found) == check.count)
        return dict(id=check.id, status="pass" if passed else "fail", matched=len(found),
                    reason=None if passed else "count_mismatch" if check.quantifier == "count" else
                    "forbidden_record" if check.quantifier == "none" else "no_matching_record" if not found else "assertion_mismatch",
                    evidence=[dict(source=check.source, index=i, sequence=r.get("_seq")) for i, r in found],
                    assertion_paths=[p.path for p in check.assertions])
    except EvidenceError as exc:
        return dict(id=check.id, status="error", reason=str(exc))


RUBRIC_SCORES = {"pass": 1.0, "partial": 0.5, "fail": 0.0}


def score_rubrics(scenario, capture, name, judge):
    rubrics = getattr(scenario.expected, name).rubrics
    if not rubrics:
        return []
    try:
        if judge is None:
            raise EvidenceError("Rubrics require a judge")
        if capture.schema_version == 4 and capture.simulator_validity != "valid":
            raise EvidenceError("Simulation is not valid")
        if not capture.started or any(capture.complete.get(s) is not True or s not in capture.observations for s in collections_for(capture)):
            raise EvidenceError("Rubric judging requires complete evidence")
        if any(r.get("finished") is not True for s in ("models", "tool_calls") for r in capture.observations[s]):
            raise EvidenceError("Unfinished calls")
        results = judge(scenario, capture, name)
        if len(results) != len(rubrics) or {r["id"] for r in results} != {r.id for r in rubrics}:
            raise EvidenceError("Missing, duplicate or unknown rubric results")
        if any(r["status"] not in {*RUBRIC_SCORES, "error", "insufficient_evidence"} for r in results):
            raise EvidenceError("Invalid rubric status")
        by_id = {r["id"]: r for r in results}
        return [dict(by_id[r.id], score=RUBRIC_SCORES.get(by_id[r.id]["status"])) for r in rubrics]
    except Exception as exc:
        return [dict(id=r.id, status="error", score=None, reason=str(exc) if isinstance(exc, EvidenceError)
                     else type(exc).__name__, citations=[]) for r in rubrics]


def grade(scenario: Scenario, capture: Capture, *, judge=None):
    accepted = {digest(scenario.inputs.model_dump())}
    if scenario.inputs.simulation is None:
        legacy = scenario.inputs.model_dump(exclude={"simulation"})
        legacy["budget"].pop("simulator_calls", None)
        legacy["budget"].pop("memory_calls", None)
        accepted.add(digest(legacy))
    if capture.scenario_id != scenario.id or capture.input_digest not in accepted:
        raise ValueError("Capture does not match scenario inputs")
    if bool(scenario.inputs.simulation) != (capture.schema_version == 4):
        raise ValueError("Capture driver version mismatch")
    if (not capture.started and capture.termination != "not_started") or (capture.started and capture.termination == "not_started"):
        raise ValueError("Inconsistent execution state")
    dimensions = {}
    for name in DIMENSIONS:
        spec = getattr(scenario.expected, name)
        if not spec.applicable:
            dimensions[name] = dict(status="not_applicable", reason=spec.reason, checks=[], rubrics=[], rubric_score=None)
            continue
        checks = [check_one(c, capture) for c in spec.checks]
        if capture.schema_version == 4 and (capture.simulator_validity != "valid" or capture.error
                or not all(capture.complete.get(s) for s in collections_for(capture))):
            checks.append(dict(id="simulation_evidence", status="error", reason=capture.error or "invalid_or_incomplete_simulation"))
        if not capture.started:
            checks.append(dict(id="execution", status="error", reason="not_started"))
        elif name == "task_completion" and capture.termination != "settled":
            checks.append(dict(id="execution", status="fail", reason=capture.termination))
        rubrics = score_rubrics(scenario, capture, name, judge)
        statuses = {c["status"] for c in [*checks, *rubrics]}
        status = ("fail" if "fail" in statuses else "error" if statuses & {"error", "insufficient_evidence"}
                  else "partial" if "partial" in statuses else "pass")
        score = sum(r["score"] for r in rubrics) / len(rubrics) if rubrics and all(r["score"] is not None for r in rubrics) else None
        dimensions[name] = dict(status=status, checks=checks, rubrics=rubrics, rubric_score=score)
    applicable = [d for d in dimensions.values() if d["status"] != "not_applicable"]
    overall = all(d["status"] == "pass" for d in applicable)
    return dict(run_id=capture.run_id, scenario_id=scenario.id, kind=scenario.kind,
                source=capture.source, repetition=capture.repetition, started=capture.started,
                termination=capture.termination, error=capture.error, dimensions=dimensions,
                branch=capture.branch, simulator_validity=capture.simulator_validity,
                error_category=capture.metadata.get("error_category"),
                passed=overall, all_dimensions_applicable=len(applicable) == 3)


def evaluate(suite, captures, repetitions, *, judge=None):
    if repetitions < 1:
        raise ValueError("Repetitions must be positive")
    expected = {(s.id, r, b) for s in suite.scenarios for r in range(1, repetitions + 1)
        for b in (("memory_off", "memory_on") if s.inputs.simulation and s.inputs.simulation.memory == "paired" else ("single",))}
    keys = [(c.scenario_id, c.repetition, c.branch) for c in captures]
    if len(set(keys)) != len(keys) or set(keys) != expected:
        raise ValueError("Missing, duplicate or extra scenario repetitions")
    if len({c.run_id for c in captures}) != len(captures):
        raise ValueError("Duplicate run IDs")
    for sid, rep, branch in expected:
        if branch == "memory_off":
            pair = [c for c in captures if c.scenario_id == sid and c.repetition == rep]
            if any(c.started and c.termination == "settled" and not c.error and all(c.complete.values())
                   and not (c.metadata.get("snapshot_digest") and c.metadata.get("pair_id")) for c in pair):
                raise ValueError("Missing paired snapshot identity")
            # Failed startup may have no snapshot, but must never become an eligible pair.
            if all(c.metadata.get("snapshot_digest") for c in pair) and (
                len({c.metadata["snapshot_digest"] for c in pair}) != 1 or
                len({c.metadata.get("pair_id") for c in pair}) != 1 or len({c.source for c in pair}) != 1):
                raise ValueError("Mismatched paired snapshots")
    scenarios = {s.id: s for s in suite.scenarios}
    results = [grade(scenarios[c.scenario_id], c, judge=judge) for c in captures]
    groups = {}
    def group_key(r):
        return r["source"] if r["branch"] == "single" else r["source"]+":"+r["branch"]
    for source in sorted({group_key(r) for r in results}):
        group = [r for r in results if group_key(r) == source]
        metrics = {}
        for kind in ("complete", "stage"):
            items = [r for r in group if r["kind"] == kind]
            if not items:
                continue
            summary = dict(planned=len(items), started=sum(r["started"] for r in items),
                simulator_validity={s: sum(r["simulator_validity"] == s for r in items)
                    for s in ("valid", "invalid", "error", "not_applicable")})
            for name in DIMENSIONS:
                values = [r["dimensions"][name] for r in items if r["started"]
                          and r["dimensions"][name]["status"] != "not_applicable"]
                counts = {state: sum(v["status"] == state for v in values) for state in ("pass", "partial", "fail", "error")}
                # An explicit failure can coexist with an evaluation error.
                errors = sum(any(c["status"] in {"error", "insufficient_evidence"} for c in [*v["checks"], *v["rubrics"]]) for v in values)
                rubric_values = [v for v in values if v["rubrics"]]
                scores = [v["rubric_score"] for v in rubric_values if v["rubric_score"] is not None]
                summary[name] = dict(counts, denominator=len(values), evaluation_errors=errors,
                    planned_denominator=sum(r["dimensions"][name]["status"] != "not_applicable" for r in items),
                    planned_pass_rate=counts["pass"] / sum(r["dimensions"][name]["status"] != "not_applicable" for r in items)
                        if any(r["dimensions"][name]["status"] != "not_applicable" for r in items) else None,
                    pass_rate=counts["pass"] / len(values) if values else None,
                    scoring_coverage=(len(values)-errors) / len(values) if values else None,
                    rubric_mean_score=sum(scores)/len(scores) if scores else None,
                    rubric_scored=len(scores), rubric_planned=len(rubric_values))
            overall = [r for r in items if r["all_dimensions_applicable"]]
            summary["overall"] = dict(passed=sum(r["passed"] for r in overall), denominator=len(overall),
                pass_rate=sum(r["passed"] for r in overall)/len(overall) if overall else None)
            metrics[kind] = summary
        groups[source] = metrics
    by_scenario = []
    for source in groups:
        for scenario in suite.scenarios:
            runs = [r for r in results if group_key(r) == source and r["scenario_id"] == scenario.id]
            if runs:
                by_scenario.append(dict(scenario_id=scenario.id, source=source, planned_repetitions=repetitions,
                    observed_repetitions=len(runs), passed=sum(r["passed"] for r in runs),
                    run_ids=[r["run_id"] for r in runs]))
    from .reporting import dynamic_report
    from .latency import resume_report
    return dict(schema_version=suite.schema_version, suite=suite.name, suite_digest=digest(suite.model_dump()),
                repetitions=repetitions, groups=groups, scenarios=by_scenario, results=results,
                experiments=dynamic_report(suite, captures, results),
                resume_summary=resume_report(suite, captures, results))
