# 文件说明：提供 RAG 评测命令行，读取题集并选择回放或实时采集模式。
"""python -m app.evaluation.rag.run --help (offline replay by default)."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from app.evaluation.rag.dataset import load_dataset, load_traces
from app.evaluation.rag.evaluator import Evaluator, current_policy_search

DATA = Path(__file__).resolve().parent / "datasets"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("replay", "retrieve"), default="replay")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--traces", type=Path)
    parser.add_argument("--k", type=int, nargs="+", default=[1, 3, 5])
    parser.add_argument("--judge", action="store_true", help="Grade supplied/generated answers with an LLM")
    parser.add_argument("--generate-answers", action="store_true", help="Generate RAG-only answers in retrieve mode")
    parser.add_argument("--live", action="store_true", help="Allow configured model/embedding/retrieval calls")
    parser.add_argument("--model", help="Judge model; defaults to current configured chat model")
    parser.add_argument("--answer-model", help="Optional separate model for RAG answer generation")
    parser.add_argument("--output", type=Path, default=Path("data/evaluation/rag_report.json"))
    args = parser.parse_args(argv)
    if any(not 1 <= k <= 5 for k in args.k):
        parser.error("--k must be between 1 and 5 (current policy service limit)")
    if (args.mode == "retrieve" or args.judge or args.generate_answers) and not args.live:
        parser.error("--live is required for configured retrieval/model calls")
    if args.generate_answers and args.mode != "retrieve":
        parser.error("--generate-answers requires --mode retrieve; replay preserves captured answers")
    if args.mode == "retrieve" and (args.traces or not args.dataset):
        parser.error("retrieve mode requires --dataset and does not accept --traces")
    if args.mode == "retrieve" and args.judge and not args.generate_answers:
        parser.error("retrieve mode needs --generate-answers for --judge; use replay to judge captured answers")
    if args.mode == "replay" and args.dataset and not args.traces:
        parser.error("A custom replay dataset requires --traces")
    if args.answer_model and not args.generate_answers:
        parser.error("--answer-model requires --generate-answers")
    if args.model and not (args.judge or args.generate_answers):
        parser.error("--model requires --judge or --generate-answers")
    dataset_path = args.dataset or DATA / "rag_demo.json"
    trace_path = args.traces or DATA / "rag_demo_traces.json"
    protected = [dataset_path] + ([trace_path] if args.mode == "replay" else [])
    if args.output.resolve() in {path.resolve() for path in protected}:
        parser.error("Report output must not overwrite a dataset or trace file")
    client = None
    try:
        dataset = load_dataset(dataset_path)
        replay = load_traces(trace_path) if args.mode == "replay" else None
        judge = generator = None
        if args.judge or args.generate_answers:
            from openai import OpenAI
            from app.config.settings import settings
            from app.evaluation.rag.judges import ModelGrader
            if not settings.openai_api_key:
                raise ValueError("Model credentials are missing")
            client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                            timeout=settings.llm_timeout_seconds, max_retries=0)
            model = args.model or settings.deepseek_model_name
            if args.judge:
                judge = ModelGrader(client, model, options=settings.chat_completion_options)
            if args.generate_answers:
                generator = ModelGrader(client, args.answer_model or model, options=settings.chat_completion_options)
        evaluator = Evaluator(ks=args.k, judge=judge, answer_generator=generator)
        report = evaluator.run_all(dataset, traces=replay.traces if replay else None,
                                  search=current_policy_search if args.mode == "retrieve" else None)
        report["evaluated_at"] = datetime.now(timezone.utc).isoformat()
        report["dataset_sha256"] = sha256(dataset_path.read_bytes()).hexdigest()
        if replay:
            report["trace_sha256"] = sha256(trace_path.read_bytes()).hexdigest()
            report["trace_description"] = replay.description
        report["model_calls"] = {"judge": judge.calls if judge else [], "answer": generator.calls if generator else []}
        report["cost"] = None
        report["cost_note"] = "No pricing supplied. Judge/generation usage is separate from captured agent tokens."
        try:
            root = Path(__file__).resolve().parents[3]
            report["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
            report["git_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())
        except (OSError, subprocess.CalledProcessError):
            report["git_commit"] = None
        if args.mode == "retrieve":
            from app.config.settings import settings
            report["retrieval_configuration"] = dict(provider=settings.embedding_provider,
                embedding_model=settings.embedding_model if settings.embedding_provider != "local" else Path(settings.local_embedding_model_path).name,
                remote=bool(settings.retrieval_service_url))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps(dict(mode=report["mode"], scope=report["scope"], summary=report["summary"], output=str(args.output)), ensure_ascii=False, indent=2))
        return 0 if report["summary"]["checks_passed"] == report["summary"]["total"] else 1
    except (ValueError, OSError) as error:
        print(f"Evaluation configuration/input error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
