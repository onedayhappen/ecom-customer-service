# 文件说明：在一次性 SQLite 中回放已记录提案，验证真实关联重试逻辑。
"""Replay recorded proposals through real association code, with disposable SQLite.

Loads only the pre-fix retry and order-read methods from --baseline for comparison.
No live model, commerce database, or original evaluation database is modified.
"""
import argparse
import ast
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.settings import settings
from app.services.commerce.service import service_factory
from app.support.store import SupportStore, Tx
from app.tasks import association, candidates
from tests.test_association_retry import CASES, install_case


def old_method(revision, module, cls, name):
    relative = Path(module.__file__).relative_to(ROOT).as_posix()
    source = subprocess.check_output(["git", "show", revision + ":" + relative], cwd=ROOT).decode("utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    method = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == name)
    method.decorator_list = []
    namespace = vars(module).copy()
    exec(compile(ast.Module(body=[method], type_ignores=[]), relative, "exec"), namespace)
    return namespace[name]


def replay(case, old_process=None, old_orders=None):
    with tempfile.TemporaryDirectory(prefix="association-retry-") as directory:
        store = SupportStore("sqlite:///" + (Path(directory) / "support.db").as_posix())
        store.ensure("s", "alice")
        batch, _, attempts = install_case(store, case)
        value = association.AssociationService(store, "alice")
        if old_process:
            value.candidates.order_lookup = None  # Original production constructor.
        contexts, sql, order_reads = [], [], []

        def model(context):
            item = attempts[len(contexts)]
            contexts.append(deepcopy(context.data()))
            if item["proposal"] is None:
                raise TimeoutError("recorded first-attempt timeout")
            return item["proposal"]

        execute = Tx.execute
        def counted(tx, statement, params=()):
            sql.append(statement)
            return execute(tx, statement, params)

        read = old_orders or candidates.CandidateService._read_orders
        def counted_orders(self, *args):
            order_reads.append(True)
            return read(self, *args)

        def no_commerce(*_):
            raise AssertionError("Unexpected commerce IO during retry")

        with ExitStack() as stack:
            stack.enter_context(patch.object(Tx, "execute", counted))
            stack.enter_context(patch.object(candidates.CandidateService, "_read_orders", counted_orders))
            stack.enter_context(service_factory(no_commerce))
            if old_process:
                stack.enter_context(patch.object(association.AssociationService, "process", old_process))
            result = value.process(batch, SimpleNamespace(associate=model))
        return dict(actions=[d["action"] for d in result["decisions"]],
                    rejections=result["rejections"], gaps=[c["gaps"] for c in contexts],
                    clarification=bool(result["clarification"]),
                    model_calls=len(contexts), order_enrichment_attempts=len(order_reads),
                    sql_statements=len(sql),
                    task_goals=[t.goal for t in value.repo.list()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings.memory_enabled = settings.short_term_memory_enabled = settings.audit_enabled = False
    settings.observability_enabled = False
    settings.api_concurrency_redis_url = ""
    old_process = old_method(args.baseline, association, "AssociationService", "process")
    old_orders = old_method(args.baseline, candidates, "CandidateService", "_read_orders")
    report = dict(method="Offline recorded-input replay with original proposal/error, IDs remapped; "
                  "minimal prerequisite task state in disposable SQLite. Baseline methods are loaded from Git. "
                  "This verifies deterministic retry handling, not live model quality or production latency.",
                  baseline=args.baseline, cases=[])
    for case in CASES:
        before, after = replay(case, old_process, old_orders), replay(case)
        assert before["clarification"] and "incomplete_candidate_context" in before["rejections"]
        assert not after["clarification"] and after["model_calls"] == before["model_calls"] == 2
        assert after["order_enrichment_attempts"] == 0
        report["cases"].append(dict(session=case["session"], round=case["round"], user=case["user"],
                                    before=before, after=after))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
