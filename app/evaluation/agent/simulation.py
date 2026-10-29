"""Dynamic session orchestration and paired runs from one verified SQLite snapshot."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from .contracts import COLLECTIONS, DYNAMIC_COLLECTIONS, Capture, digest
from .memory import MemoryObserver, clone_store, memory_environment, snapshot_digest
from .runtime import BudgetExceeded, run_inputs
from .sandbox import isolated_store
from .simulator import Calls, UserSimulator


def run_simulation(inputs, *, client, user_client, model, user_model, run_id, scenario_id,
                   reviewer_model=None, memory_model=None, repetition=1,
                   source="scripted_regression", persist=None):
    """Only execution inputs enter this process; expected/rubric never enters simulation."""
    if inputs.simulation is None:
        raise ValueError("Dynamic inputs required")
    plan = inputs.simulation
    state = dict(objects=deepcopy(inputs.objects), receipts={})
    outputs = []
    paired = plan.memory == "paired"
    baseline = None

    def emit(current=None, warmup=None):
        if not persist:
            return
        present = {c.branch: c for c in [*outputs, *([current] if current else [])]}
        planned = []
        for branch in (("memory_off", "memory_on") if paired else ("single",)):
            item = present.get(branch)
            if item is None:
                item = Capture(schema_version=4, run_id=run_id+":"+branch, scenario_id=scenario_id,
                    input_digest=digest(inputs.model_dump()), repetition=repetition, source=source, branch=branch,
                    simulator_validity="error", metadata=dict(model=model, user_model=user_model,
                        **({"warmup": warmup.model_dump()} if warmup else {})))
            else:
                item = item.model_copy(deep=True)
            if baseline:
                item.metadata.update(snapshot_digest=baseline, pair_id=run_id)
            planned.append(item)
        persist(planned)

    def execute(phases, store, business, branch, *, warmup=None, memory_reads=True):
        used = (warmup.metadata.get("usage", {}) if warmup else {})
        elapsed = warmup.metadata.get("elapsed_seconds", 0) if warmup else 0
        started_at = time.monotonic()
        deadline = started_at + max(0, inputs.budget.seconds-elapsed)
        simulation_calls = Calls(user_client, user_model,
            max(0, inputs.budget.simulator_calls-used.get("simulator", 0)), deadline)
        memory_calls = Calls(client, memory_model or model,
            max(0, inputs.budget.memory_calls-used.get("memory", 0)), deadline)
        observer = MemoryObserver(memory_reads and plan.memory != "off", memory_calls)
        cap = Capture(schema_version=4, run_id=run_id+":"+branch, scenario_id=scenario_id,
            input_digest=digest(inputs.model_dump()), repetition=repetition, source=source,
            branch=branch if branch in {"memory_off", "memory_on"} else "single",
            simulator_validity="valid", observations={s: [] for s in DYNAMIC_COLLECTIONS},
            complete={s: False for s in DYNAMIC_COLLECTIONS},
            metadata=dict(model=model, user_model=user_model, reviewer_model=reviewer_model or user_model,
                          memory_model=memory_model or model, retrieval="lexical_only", usage={}))
        cap.metadata["latency"] = dict(schema_version=1, scope="service_entry_observed", requests=[])
        completed, model_count, tool_count, rounds = [], used.get("models", 0), used.get("tools", 0), used.get("rounds", 0)
        offset = 0

        def publish():
            cap.observations["memory"] = deepcopy(observer.records)
            cap.metadata.update(elapsed_seconds=elapsed+time.monotonic()-started_at,
                role_calls=dict(simulator=deepcopy(simulation_calls.records), memory=deepcopy(memory_calls.records)),
                usage=dict(models=model_count, tools=tool_count, rounds=rounds,
                    simulator=used.get("simulator", 0)+len(simulation_calls.records),
                    memory=used.get("memory", 0)+len(memory_calls.records)))
            emit(current=cap if branch != "warmup" else None, warmup=cap if branch == "warmup" else warmup)

        if warmup:
            cap.metadata["warmup"] = warmup.model_dump()
            # Authoritative, actually published source messages; not planned future disclosure.
            observer.add("warmup_sources", events=[e for e in warmup.observations["events"]
                if e.get("visibility") == "public" and e.get("kind") == "message"])
        try:
            with observer.installed():
                observer.snapshot(store, "start")
                if warmup and (warmup.termination != "settled" or warmup.simulator_validity != "valid"
                               or not all(warmup.complete.values())):
                    cap.simulator_validity = warmup.simulator_validity
                    raise RuntimeError("warmup_failed")
                for index, phase in enumerate(phases):
                    remaining = dict(seconds=deadline-time.monotonic(),
                        model_calls=inputs.budget.model_calls-model_count,
                        tool_calls=inputs.budget.tool_calls-tool_count, rounds=inputs.budget.rounds-rounds)
                    if remaining["seconds"] < .1 or any(remaining[k] < 1 for k in ("model_calls", "tool_calls", "rounds")):
                        raise BudgetExceeded("trajectory_budget")
                    phase = phase.model_copy(update={"max_turns": min(phase.max_turns, remaining["rounds"])})
                    phase_inputs = inputs.model_copy(deep=True)
                    phase_inputs.budget = inputs.budget.model_copy(update=remaining)
                    phase_inputs.seeds = inputs.seeds if not warmup and index == 0 else []
                    driver = UserSimulator(phase, inputs.sessions[phase.session], simulation_calls, reviewer_model)
                    observer.phase_id = phase.id
                    def collect(partial):
                        # Preserve running worker evidence if supervisor kills a hung API.
                        cap.metadata["inflight"] = partial.model_dump()
                        cap.started = cap.started or partial.started
                        cap.simulator_validity = driver.validity
                        publish()
                    item = run_inputs(phase_inputs, client=client, model=model, run_id=cap.run_id+":"+phase.id,
                        scenario_id=scenario_id, repetition=repetition, source=source, store=store, driver=driver,
                        business_state=business, memory_observer=observer, persist=collect)
                    cap.metadata.pop("inflight", None)
                    cap.metadata["latency"]["requests"].extend(
                        dict(row, phase_id=phase.id) for row in item.metadata.get("latency", {}).get("requests", []))
                    cap.started = cap.started or item.started
                    cap.simulator_validity = driver.validity
                    for name in COLLECTIONS:
                        rows = deepcopy(item.observations.get(name, []))
                        for row in rows:
                            row["phase_id"] = phase.id
                            if "_seq" in row:
                                row["_seq"] += offset
                        # Final state snapshots remain final, not summed across sessions.
                        if name in {"tasks", "objects", "operations"}:
                            cap.observations[name] = rows
                        else:
                            # Initial history is already retained once in previous phases / warmup.
                            if name == "events":
                                rows = [r for r in rows if not r.get("session_id") or r["session_id"] == phase.session]
                            cap.observations[name].extend(rows)
                    cap.boundaries.update({k: v+offset for k, v in item.boundaries.items()})
                    offset += max((r.get("_seq", 0) for rows in item.observations.values() for r in rows), default=0)+1
                    cap.observations["user_actions"].extend(deepcopy(driver.actions))
                    cap.observations["sessions"].append(dict(phase_id=phase.id, session_id=phase.session,
                        user_id=inputs.sessions[phase.session], termination=item.termination,
                        simulator_validity=driver.validity, turns=driver.turns, error=item.error,
                        objects=deepcopy(item.observations.get("objects", []))))
                    model_count += len(item.observations.get("models", []))
                    tool_count += len(item.observations.get("tool_calls", []))
                    rounds += driver.turns
                    completed.append(item)
                    cap.termination, cap.error = item.termination, item.error
                    if item.termination != "settled" or driver.validity != "valid":
                        cap.metadata["error_category"] = ("budget" if item.error == "BudgetExceeded" or item.termination == "budget"
                            else "simulator" if driver.validity != "valid" else "agent_runtime")
                        break
                    if plan.memory != "off":
                        cap.metadata["error_category"] = "memory_processing"
                        observer.archive(store, inputs.sessions[phase.session], phase.session)
                        cap.metadata.pop("error_category", None)
                    publish()
                if len(completed) == len(phases) and cap.termination == "settled":
                    cap.complete = {s: all(c.complete.get(s, False) for c in completed) for s in COLLECTIONS}
                    cap.complete.update(user_actions=cap.simulator_validity == "valid", sessions=True, memory=True)
        except Exception as exc:
            cap.error = str(exc) if str(exc) in {"warmup_failed", "trajectory_budget"} else type(exc).__name__
            cap.termination = ("budget" if isinstance(exc, BudgetExceeded) else "error") if cap.started else "not_started"
            cap.metadata.setdefault("error_category", "budget" if isinstance(exc, BudgetExceeded) else "environment")
        publish()
        return cap

    with isolated_store(sessions=inputs.sessions.items()) as store, TemporaryDirectory(prefix="agent-memory-eval-") as tmp:
        with memory_environment(Path(tmp)/"warmup", plan.memory != "off"):
            if not paired:
                return [execute(plan.phases, store, state, "single")]
            warmup = execute(plan.phases[:plan.warmup_sessions], store, state, "warmup")
            snapshot = snapshot_digest(store, state)
            baseline = snapshot
        for branch in ("memory_off", "memory_on"):
            branch_store = clone_store(store, Path(tmp)/(branch+".db"))
            branch_state = deepcopy(state)
            if snapshot_digest(branch_store, branch_state) != snapshot:
                raise ValueError("Snapshot clone mismatch")
            with memory_environment(Path(tmp)/branch, True):
                cap = execute(plan.phases[plan.warmup_sessions:], branch_store, branch_state, branch,
                    warmup=warmup, memory_reads=branch == "memory_on")
            cap.metadata.update(snapshot_digest=snapshot, pair_id=run_id,
                                comparison="same_warmup_read_ablation")
            outputs.append(cap)
            emit(warmup=warmup)
        return outputs
