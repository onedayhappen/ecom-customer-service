"""Offline replay and local overhead measurement; no model or business DB calls.

Run from the repository root:
    python -m scripts.benchmark_reply_grounding --output data/evaluation/reply-grounding-regression.json
"""
import argparse
import json
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from types import SimpleNamespace

from app.agents.reply_composition import compose, validate_reply
from app.prompts.reply_composition import REPLY_COMPOSITION_PROMPT
from app.session.context_budget import estimate_request_tokens
from app.tasks.reply_facts import facts
from app.tasks.reply_grounding import choices, project, response


def run(iterations):
    reports = []
    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "tests/fixtures/reply_grounding").glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        record = SimpleNamespace(details={})
        def context():
            return dict(current_messages=[dict(role="user", content=case["user_message"])], recent_messages=[],
                        matters=[dict(task_id="case", verified_facts="", grounding=project(record, case["receipts"]))],
                        required_notices=[], other_matters=[])
        evidence = context()
        old_rejected = False
        try:
            validate_reply(case["original_reply"], context=evidence)
        except ValueError as exc:
            old_rejected = str(exc) == "ungrounded_composed_reply"
        assert old_rejected
        requests = []
        if case["source"]["reply_sequence"] == 20:
            relevant = [b["id"] for b in choices(evidence) if "HLA海澜之家" in b["text"] and ("88.00" in b["text"] or "暂未查到包裹记录" in b["text"])]
        else:
            relevant = [b["id"] for b in choices(evidence) if "ROSHE RUN" in b["text"] and "最新轨迹时间" in b["text"]]
        def create(**params):
            requests.append(params)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=json.dumps({"blocks": relevant}), tool_calls=None))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        answer = compose(client, "offline", evidence)
        assert len(requests) == 1
        old_context = dict(evidence, matters=[dict(task_id="case", verified_facts=facts(record, case["receipts"]))])
        baseline = dict(requests[0], messages=[dict(role="system", content=REPLY_COMPOSITION_PROMPT),
            dict(role="user", content=json.dumps(old_context, ensure_ascii=False))])
        samples = []
        for _ in range(iterations):
            start = perf_counter_ns()
            ctx = context()
            validate_reply(response(ctx), context=ctx)
            samples.append((perf_counter_ns() - start) / 1_000_000)
        samples.sort()
        reports.append(dict(case=path.stem, source=case["source"], original_rejected=old_rejected,
            corrected_reply=answer, fallback_reply=response(evidence),
            composition_calls=len(requests), added_verification_model_calls=0,
            input_estimate_original_fact_format=estimate_request_tokens(baseline),
            input_estimate_grounded_blocks=estimate_request_tokens(requests[0]),
            local_projection_and_validation_ms=dict(iterations=iterations, median=median(samples), p95=samples[int(.95 * (len(samples)-1))])))
    return dict(method="Recorded receipts and bad replies; deterministic model-selection stub, no live LLM or business writes. Token counts are local estimates. Timing excludes imports and fixture I/O.", cases=reports)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    report = run(args.iterations)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
