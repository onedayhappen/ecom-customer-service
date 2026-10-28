"""Offline replay and local cost probe; never calls a model or commerce database.

python scripts/verify_reply_evidence.py --baseline 7b65437 \
    --output data/evaluation/reply-evidence-verification.json

Timing covers projection/validation only, not provider latency or production load.
Token counts use the application's estimator, not provider billing measurements.
"""
import argparse
import ast
from copy import deepcopy
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.reply_composition import selection_context
from app.config.settings import settings
from app.memory.source_policy import safe_context
from app.prompts.reply_composition import REPLY_COMPOSITION_PROMPT
from app.session.context_budget import estimate_request_tokens
from app.support.store import SupportStore, Tx
from app.tasks.replies import ReplyCoordinator
from app.tasks.reply_claims import build_claims, render_plan
from app.tasks.reply_facts import facts
from tests.test_reply_evidence import CASES, assert_safe_case, case_claim, install_case, plan


def measure(fn, iterations):
    for _ in range(20):
        fn()
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return dict(median_ms=round(statistics.median(samples), 4),
                p95_ms=round(sorted(samples)[int(len(samples) * .95)], 4))


def tokens(context, prompt):
    return estimate_request_tokens(dict(messages=[dict(role="system", content=prompt),
        dict(role="user", content=json.dumps(safe_context(context), ensure_ascii=False))]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Git revision before the evidence fix")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=500)
    args = parser.parse_args()
    if not 20 <= args.iterations <= 10000:
        parser.error("iterations must be between 20 and 10000")
    source = subprocess.check_output(["git", "show", args.baseline + ":app/prompts/reply_composition.py"], cwd=ROOT)
    old_prompt = ast.literal_eval(ast.parse(source.decode("utf-8")).body[0].value)
    settings.memory_enabled = settings.short_term_memory_enabled = settings.audit_enabled = False
    settings.observability_enabled = False
    settings.api_concurrency_redis_url = ""
    report = dict(method="Offline replay of original recorded receipts through TaskRuns and ReplyCoordinator; "
                  "deterministic composers, disposable SQLite, no live model/business IO. "
                  "Local timing excludes model/DB latency; estimated tokens are not billed tokens.",
                  baseline=args.baseline, iterations=args.iterations, cases=[])
    for case in CASES:
        item = dict(session=case["session"], round=case["round"])
        for historical in (False, True):
            with tempfile.TemporaryDirectory(prefix="reply-evidence-") as temp:
                store = SupportStore("sqlite:///" + (Path(temp) / "support.db").as_posix())
                store.ensure("s", "alice")
                record, receipts = install_case(store, case)
                contexts, sql = [], []

                def composer(context):
                    contexts.append(context)
                    return case["historical_reply"] if historical else plan(case_claim(context, case))

                execute = Tx.execute

                def counted(tx, statement, params=()):
                    sql.append(statement)
                    return execute(tx, statement, params)

                with patch.object(Tx, "execute", counted):
                    published = ReplyCoordinator(store, "alice", composer=composer).publish("s")
                assert_safe_case(published["content"], case)
                assert len(contexts) == 1
                if historical:
                    item["historical_reply_rejected"] = published["content"] != case["historical_reply"]
                    item["fallback"] = published["content"]
                    continue
                context = contexts[0]
                item.update(reply=published["content"], composition_calls=len(contexts),
                            publication_sql_statements=len(sql))
                baseline = deepcopy(context)
                baseline["matters"][0].pop("claims")
                baseline["matters"][0]["verified_facts"] = facts(record, receipts)
                item["estimated_input_tokens"] = dict(before=tokens(baseline, old_prompt),
                    after=tokens(selection_context(context), REPLY_COMPOSITION_PROMPT))

                def validated_projection():
                    fresh = dict(matters=[dict(task_id=record.task_id,
                        evidence_ids=[r["event_id"] for r in receipts], claims=build_claims(record, receipts))])
                    return render_plan(plan(case_claim(fresh, case)), fresh)

                item["local_projection"] = dict(before=measure(lambda: facts(record, receipts), args.iterations),
                    after_including_validation=measure(validated_projection, args.iterations))
        report["cases"].append(item)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
