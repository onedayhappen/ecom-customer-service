"""Service-entry observed latency, independent of simulator and judge wall time."""
from copy import deepcopy
from math import ceil, isfinite
from threading import RLock
import time

from .contracts import DIMENSIONS


class ResponseTimer:
    def __init__(self, clock=time.monotonic):
        self.clock, self.rows, self.starts = clock, [], {}
        self.lock = RLock()

    def begin(self, step):
        with self.lock:
            active = [r for r in self.rows if r["status"] == "pending"]
            for r in active:
                r["overlapping"] = True
            row = dict(step_id=step.id, session_id=step.session, action=step.action,
                status="pending", overlapping=bool(active), first_reply_seconds=None,
                last_reply_seconds=None, settled_seconds=None, reply_events=[])
            self.rows.append(row)
            self.starts[step.id] = self.clock()

    def reply(self, session_id, sequence):
        with self.lock:
            for row in self.rows:
                if row["status"] != "pending" or row["session_id"] != session_id:
                    continue
                if sequence in row["reply_events"]:
                    continue
                row["reply_events"].append(sequence)
                seconds = max(0.0, self.clock()-self.starts[row["step_id"]])
                if row["first_reply_seconds"] is None:
                    row["first_reply_seconds"] = seconds
                row["last_reply_seconds"] = seconds

    def settle(self):
        with self.lock:
            for row in self.rows:
                if row["status"] == "pending":
                    row.update(status="settled", settled_seconds=max(0.0, self.clock()-self.starts[row["step_id"]]))

    def stop(self, reason):
        with self.lock:
            for row in self.rows:
                if row["status"] == "pending":
                    row.update(status=reason, observed_seconds=max(0.0, self.clock()-self.starts[row["step_id"]]))

    def snapshot(self):
        with self.lock:
            rows = deepcopy(self.rows)
            for row in rows:
                if row["status"] == "pending":
                    row["observed_seconds"] = max(0.0, self.clock()-self.starts[row["step_id"]])
            return dict(schema_version=1, scope="service_entry_observed", requests=rows)


def statistics(values):
    values = sorted(v for v in values if type(v) in {int, float} and isfinite(v) and v >= 0)
    return dict(n=len(values), mean_seconds=sum(values)/len(values) if values else None,
        p50_seconds=values[ceil(.50*len(values))-1] if values else None,
        p95_seconds=values[ceil(.95*len(values))-1] if values else None)


def timing_rows(capture):
    timing = capture.metadata.get("latency")
    if not isinstance(timing, dict) or timing.get("schema_version") != 1:
        return None
    rows = list(timing.get("requests", []))
    # Partial dynamic phase is persisted separately until runtime returns.
    partial = capture.metadata.get("inflight", {}).get("metadata", {}).get("latency", {})
    rows.extend(partial.get("requests", []))
    return rows


def resume_report(suite, captures, results):
    """Only three business dimensions plus latency; diagnostic reports remain separate."""
    scenarios = {s.id: s for s in suite.scenarios}
    scored = {r["run_id"]: r for r in results}
    grouped = {}
    for cap in captures:
        scenario = scenarios[cap.scenario_id]
        mode = "dynamic" if scenario.inputs.simulation else "static_steps"
        key = "|".join((cap.source, mode, cap.branch, scenario.kind, str(cap.metadata.get("model", "unknown"))))
        grouped.setdefault(key, []).append(cap)
    groups = {}
    for key, caps in grouped.items():
        business = {}
        for name in DIMENSIONS:
            values = [scored[c.run_id]["dimensions"][name] for c in caps
                      if scored[c.run_id]["dimensions"][name]["status"] != "not_applicable"]
            counts = {status: sum(v["status"] == status for v in values) for status in ("pass", "partial", "fail", "error")}
            errors = sum(any(x["status"] in {"error", "insufficient_evidence"}
                             for x in [*v["checks"], *v["rubrics"]]) for v in values)
            business[name] = dict(counts, planned=len(values), evaluation_errors=errors,
                pass_rate=counts["pass"]/len(values) if values else None,
                scoring_coverage=(len(values)-errors)/len(values) if values else None)
        entries = [(c, timing_rows(c)) for c in caps]
        rows = [(c, r) for c, rs in entries if rs is not None for r in rs]
        eligible = [(c, r) for c, r in rows if not r.get("overlapping")
                    and c.simulator_validity in {"valid", "not_applicable"}]
        # Include observed first replies even on a later timeout; never substitute timeout with zero.
        first = [r.get("first_reply_seconds") for _, r in eligible]
        complete = [r.get("settled_seconds") for _, r in eligible if r.get("status") == "settled"]
        groups[key] = dict(planned_runs=len(caps), started_runs=sum(c.started for c in caps),
            quality=business, latency=dict(scope="service_entry_observed", unit="seconds",
                runs_with_timing=sum(rs is not None for _, rs in entries),
                runs_missing_timing=sum(rs is None for _, rs in entries),
                observed_requests=len(rows),
                overlapping_requests=sum(bool(r.get("overlapping")) for _, r in rows),
                simulator_excluded_requests=sum(c.simulator_validity not in {"valid", "not_applicable"} for c, _ in rows),
                unfinished_requests=sum(r.get("status") != "settled" for _, r in rows),
                requests_without_reply=sum(r.get("first_reply_seconds") is None for _, r in rows),
                timeout_runs=sum(c.termination == "timeout" or c.error == "supervisor_timeout" for c in caps),
                budget_runs=sum(c.termination == "budget" or c.metadata.get("error_category") == "budget" for c in caps),
                first_reply=statistics(first), service_settled=statistics(complete)))
    return dict(dimensions=list(DIMENSIONS), groups=groups,
        note="Quality uses all planned applicable runs. Latency excludes overlapping inputs and invalid simulation; "
             "first replies may precede failure, settled does not imply business success. Missing timings remain unscored.")
