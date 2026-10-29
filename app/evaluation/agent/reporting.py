"""Explicit denominators, paired deltas and independently annotated memory facts."""
from .contracts import DIMENSIONS, collections_for


def memory_metrics(scenario, capture):
    observations = capture.observations
    warm = capture.metadata.get("warmup", {}).get("observations", {})
    actions = [*warm.get("user_actions", []), *observations.get("user_actions", [])]
    events = [*warm.get("events", []), *observations.get("events", [])]
    records = observations.get("memory", [])
    reliable = capture.simulator_validity == "valid" and capture.complete.get("memory") is True
    phases = {p.id: p for p in scenario.inputs.simulation.phases}
    rows = []
    for target in scenario.memory_targets:
        origin = phases[target.disclosed_phase]
        user = scenario.inputs.sessions[phases[target.phase_id].session]
        declared = [d["quote"] for a in actions if a.get("phase_id") == target.disclosed_phase and a.get("status") == "valid"
            for d in a.get("action", {}).get("disclosures", []) if d["fact_id"] == target.fact_id]
        published = any(q in e.get("payload", {}).get("content", "") for q in declared
            for e in events if e.get("session_id") == origin.session and e.get("kind") == "message"
            and e.get("payload", {}).get("role") == "user")
        def matches(f):
            return (f.get("predicate") == target.predicate and f.get("value") == target.value
                and all(f.get("qualifiers", {}).get(k) == v for k, v in target.qualifiers.items()))
        snapshots = [r for r in [*warm.get("memory", []), *records] if r.get("phase_id") == target.phase_id and r.get("kind") == "snapshot"
                     and r.get("stage") == "after_archive"]
        reads = [r for r in records if r.get("phase_id") == target.phase_id and r.get("kind") == "read"
                 and r.get("method") == "retrieve_relevant_long_term_memory" and r.get("user_id") == user
                 and not r.get("blocked") and "error" not in r]
        stored = (any(f["user_id"] == user and f["status"] == "active" and matches(f["payload"])
                      for f in snapshots[-1]["facts"]) if snapshots and published and reliable else None)
        recalled = (any(matches(f) for r in reads for f in r.get("facts", [])) if reads and published and reliable else None)
        rows.append(dict(id=target.id, disclosed=published, expected_state=target.expected_state,
            storage_pass=(stored if target.expected_state == "active" else not stored) if stored is not None else None,
            recall_pass=(recalled if target.expected_state == "active" else not recalled) if recalled is not None else None))
    def rate(field):
        available = [r[field] for r in rows if r[field] is not None]
        return dict(scored=len(available), annotated=len(rows), passed=sum(available),
                    rate=sum(available)/len(available) if available else None)
    return dict(targets=rows, storage=rate("storage_pass"), recall=rate("recall_pass"),
        observed_retrieval_calls=sum(r.get("method") == "retrieve_relevant_long_term_memory" for r in records),
        observed_history_reads=sum(r.get("kind") == "task_history" for r in records),
        usage_accuracy=None, unnecessary_questions=None,
        note="Usage, misuse and unnecessary questions require calibrated rubrics; no score inferred from answer similarity.")


def dynamic_report(suite, captures, results):
    caps = {c.run_id: c for c in captures}
    scenarios = {s.id: s for s in suite.scenarios}
    dynamic = [r for r in results if caps[r["run_id"]].schema_version == 4]
    def eligible(r):
        c = caps[r["run_id"]]
        return (r["simulator_validity"] == "valid" and r["started"] and not c.error
                and c.termination == "settled" and all(c.complete.get(k) is True and k in c.observations for k in collections_for(c)))
    valid_groups = {}
    for branch in sorted({r["branch"] for r in dynamic}):
        for source in sorted({r["source"] for r in dynamic if r["branch"] == branch}):
            runs = [r for r in dynamic if r["branch"] == branch and r["source"] == source]
            valid = [r for r in runs if eligible(r)]
            valid_groups[source+":"+branch] = dict(planned=len(runs), valid=len(valid),
                excluded=[dict(run_id=r["run_id"], reason=r["error"] or r["simulator_validity"] if r["simulator_validity"] != "valid"
                    else r["error"] or r["termination"]) for r in runs if not eligible(r)],
                passed=sum(r["passed"] for r in valid),
                pass_rate=sum(r["passed"] for r in valid)/len(valid) if valid else None)
    pairs = []
    for r in dynamic:
        if r["branch"] != "memory_off":
            continue
        other = next(x for x in dynamic if x["scenario_id"] == r["scenario_id"]
                     and x["repetition"] == r["repetition"] and x["branch"] == "memory_on")
        a, b = caps[r["run_id"]], caps[other["run_id"]]
        same = bool(a.metadata.get("snapshot_digest") and a.metadata.get("pair_id")
            and a.metadata.get("snapshot_digest") == b.metadata.get("snapshot_digest")
            and a.metadata.get("pair_id") == b.metadata.get("pair_id") and a.source == b.source)
        valid = eligible(r) and eligible(other) and same
        deltas = {}
        if valid:
            for d in DIMENSIONS:
                x, y = r["dimensions"][d], other["dimensions"][d]
                def scored(v):
                    return v["status"] not in {"not_applicable", "error"} and not any(
                        item["status"] in {"error", "insufficient_evidence"} for item in [*v["checks"], *v["rubrics"]])
                deltas[d] = (int(y["status"] == "pass")-int(x["status"] == "pass")
                             if scored(x) and scored(y) else None)
            for key in ("rounds", "models", "tools", "simulator", "memory"):
                x, y = a.metadata.get("usage", {}).get(key), b.metadata.get("usage", {}).get(key)
                deltas[key] = y-x if type(x) is int and type(y) is int else None
        pairs.append(dict(scenario_id=r["scenario_id"], repetition=r["repetition"], source=r["source"],
            off_run=a.run_id, on_run=b.run_id, valid=valid, same_snapshot=same, deltas_on_minus_off=deltas))
    paired_groups = {}
    for source in sorted({p["source"] for p in pairs}):
        group = [p for p in pairs if p["source"] == source]
        valid = [p for p in group if p["valid"]]
        means = {}
        for key in (*DIMENSIONS, "rounds", "models", "tools", "simulator", "memory"):
            values = [p["deltas_on_minus_off"].get(key) for p in valid]
            values = [v for v in values if v is not None]
            means[key] = dict(scored_pairs=len(values), mean=sum(values)/len(values) if values else None)
        paired_groups[source] = dict(planned_pairs=len(group), valid_pairs=len(valid), means=means)
    return dict(valid_runs=valid_groups, pairs=pairs, paired_summary=paired_groups,
        memory={r["run_id"]: memory_metrics(scenarios[r["scenario_id"]], caps[r["run_id"]]) for r in dynamic})
