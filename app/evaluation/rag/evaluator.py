# 文件说明：采集当前政策检索结果或回放轨迹，分别执行检索与回答评测。
"""Collect current policy results or replay captured traces, then grade separately."""
import time

from app.evaluation.rag.dataset import PolicyReceipt, ReplayTrace
from app.evaluation.rag.judges import unscored
from app.evaluation.rag.metrics import aggregate, retrieval_metrics


def current_policy_search(query, context, top_k):
    """Use the same local/remote scoped-policy service as current commerce tools."""
    from app.rag.service import search_policy, policy_result_current
    from app.rag.policy import failure
    receipt = search_policy(query, top_k, context)
    if receipt.get("success") is True and not policy_result_current(receipt, context):
        return failure("policy_release_changed")
    return receipt


def evidence_for(trace):
    # Labels/reference answers never enter the faithfulness evidence.
    import json
    evidence = []
    for hit in trace.retrieval.results:
        metadata = hit.model_dump(mode="json")
        body = metadata.pop("text")
        # Preserve literal line breaks so a multiline quotation can be verified.
        evidence.append(json.dumps(metadata, ensure_ascii=False) + "\n正文：\n" + body)
    evidence.extend(json.dumps(observation.model_dump(), ensure_ascii=False) for observation in trace.observations)
    if not trace.retrieval.success:
        evidence.append(json.dumps(trace.retrieval.model_dump(mode="json"), ensure_ascii=False))
    return evidence


class Evaluator:
    def __init__(self, *, ks=(1, 3, 5), judge=None, answer_generator=None):
        if not ks or any(type(k) is not int or not 1 <= k <= 5 for k in ks):
            raise ValueError("Current policy retrieval supports k from 1 to 5")
        self.ks = sorted(set(ks))
        self.judge, self.answer_generator = judge, answer_generator

    def run_case(self, case, *, trace=None, search=None):
        start = time.monotonic()
        row = dict(case_id=case.id, tags=case.tags, status="ok", errors=[], metrics={},
                   retrieval_passed=False, checks_passed=False, trace=None,
                   faithfulness=unscored("disabled", "Model judge not requested"),
                   answer_quality=unscored("disabled", "Model judge not requested"))
        try:
            if search is not None:
                result = PolicyReceipt.model_validate(search(case.query, case.context, max(self.ks)))
                trace = ReplayTrace(case_id=case.id, query=case.query, context=case.context,
                                    requested_k=max(self.ks), retrieval=result)
            if trace is None:
                raise ValueError("Missing replay trace")
            trace = trace.model_copy(deep=True)
            if trace.case_id != case.id or trace.query != case.query or trace.context != case.context:
                raise ValueError("Trace does not match case ID, query and context")
            if trace.requested_k < max(self.ks):
                raise ValueError("Replay was captured at a smaller k than requested")
            if len(trace.retrieval.results) > trace.requested_k:
                raise ValueError("Trace contains more results than requested")
            receipt = trace.retrieval.model_dump(mode="json")
            if "context" in receipt and receipt["context"] != trace.context.model_dump(mode="json"):
                raise ValueError("Receipt context does not match the trace")
            if "query" in receipt and receipt["query"] != trace.query:
                raise ValueError("Receipt query does not match the trace")
            labels = {label.key: label.grade for label in case.relevance}
            ranked = [hit.key for hit in trace.retrieval.results]
            for k in self.ks:
                row["metrics"].update({f"{name}@{k}": value for name, value in retrieval_metrics(labels, ranked, k).items()})
            row["duplicate_hits"] = len(ranked) - len(set(ranked))
            if case.expected_error:
                row["abstention_correct"] = not trace.retrieval.success and trace.retrieval.error_code == case.expected_error
                row["retrieval_passed"] = row["abstention_correct"]
            else:
                row["abstention_correct"] = None
                row["retrieval_passed"] = trace.retrieval.success and row["metrics"][f"recall@{max(self.ks)}"] == 1.0
            if not trace.retrieval.success and trace.retrieval.error_code != case.expected_error:
                row["status"] = "retrieval_error"
                row["errors"].append(trace.retrieval.error_code)
            row["answer_origin"] = "captured" if trace.answer is not None else "missing"
            if self.answer_generator is not None:
                trace.answer = self.answer_generator.generate_answer(case.query, evidence_for(trace))
                row["answer_origin"] = "rag_harness_generated"
            row["trace"] = trace.model_dump(mode="json")
            if self.judge is not None:
                evidence = evidence_for(trace)
                row["faithfulness"] = self.judge.judge_faithfulness(case.query, trace.answer, evidence)
                row["answer_quality"] = self.judge.judge_answer_quality(case.query, trace.answer, case.reference_answer, evidence)
                for name in ("faithfulness", "answer_quality"):
                    if row[name]["status"] in {"error", "missing_answer"}:
                        row["errors"].append(f"{name}:{row[name]['status']}")
                        row["status"] = "judge_error"
            # A partial/unscored judge result cannot silently pass answer checks.
            answer_passed = (self.judge is None or (
                row["faithfulness"]["status"] == "scored" and row["faithfulness"]["score"] == 1.0
                and row["answer_quality"]["status"] == "scored" and row["answer_quality"]["score"] >= 0.6))
            row["checks_passed"] = row["retrieval_passed"] and answer_passed and not row["errors"]
        except Exception as error:
            row["status"] = "evaluation_error"
            row["errors"].append(type(error).__name__)
            if trace is not None:
                row["trace"] = trace.model_dump(mode="json")
        row["elapsed_seconds"] = round(time.monotonic() - start, 4)
        return row

    def run_all(self, dataset, *, traces=None, search=None):
        if (traces is None) == (search is None):
            raise ValueError("Choose captured traces OR a retrieval adapter")
        by_id = {trace.case_id: trace for trace in traces or []}
        if traces is not None and (len(by_id) != len(traces) or set(by_id) != {c.id for c in dataset.cases}):
            raise ValueError("Dataset and replay trace IDs must match exactly")
        results = [self.run_case(case, trace=by_id.get(case.id), search=search) for case in dataset.cases]
        names = [f"{metric}@{k}" for k in self.ks for metric in ("recall", "precision", "mrr", "ndcg")]
        summary = dict(
            total=len(results), retrieval_passed=sum(row["retrieval_passed"] for row in results),
            checks_passed=sum(row["checks_passed"] for row in results),
            errors=sum(bool(row["errors"]) for row in results),
            retrieval={name: aggregate([row["metrics"].get(name) for row in results]) for name in names},
            abstention=aggregate([row.get("abstention_correct") for row in results]),
        )
        for name in ("faithfulness", "answer_quality"):
            counts = {}
            for row in results:
                status = row[name]["status"]
                counts[status] = counts.get(status, 0) + 1
            summary[name] = {**aggregate([r[name]["score"] for r in results]), "statuses": counts}
        return dict(schema_version=1, dataset=dataset.name, description=dataset.description,
                    mode="current_policy_retrieval" if search else "captured_trace_replay", ks=self.ks,
                    scope="RAG component evaluation; not end-to-end agent/business success rate",
                    thresholds=dict(recall_at_max_k=1.0, faithfulness=1.0, answer_quality=0.6),
                    summary=summary, cases=results)
