"""业务链监控日志查询 CLI：汇总线上观测 JSONL，输出可读报告。

用法：
    python -m app.observability.query                     # 今天的日志
    python -m app.observability.query --date 2026-08-20   # 指定日期
    python -m app.observability.query --last 100          # 最近 100 条请求
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from app.config.settings import settings


def load_traces(
    log_dir: str, date_str: str | None = None, last: int | None = None,
) -> list[dict]:
    root = Path(log_dir)
    files = sorted(root.glob("business-trace-*.jsonl"))
    if date_str:
        files = [f for f in files if date_str in f.name]
    traces: list[dict] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        traces.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # 半行写入可容忍，跳过
    if last:
        traces = traces[-last:]
    return traces


def _pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * p))
    return sorted_values[idx]


def summarize(traces: list[dict]) -> dict:
    n = len(traces)
    durations = sorted(t["duration_ms"] for t in traces)
    tokens = [
        sum(call.get("total_tokens", 0) for call in t.get("llm_calls", []))
        for t in traces
    ]
    llm_counts = [len(t.get("llm_calls", [])) for t in traces]
    intents = Counter(
        (t.get("response") or {}).get("intent") or "unknown" for t in traces
    )
    executions = Counter(
        (t.get("response") or {}).get("execution") or "none" for t in traces
    )
    tool_names: Counter[str] = Counter()
    tool_failures = 0
    tool_calls = 0
    for t in traces:
        for step in t.get("tool_steps", []):
            tool_calls += 1
            tool_names[step.get("name", "?")] += 1
            if step.get("blocked") or not (step.get("result") or {}).get("success"):
                tool_failures += 1
    errors = [
        {
            "request_id": t.get("request_id"),
            "intent": (t.get("response") or {}).get("intent"),
            "error": t.get("error"),
            "duration_ms": t.get("duration_ms"),
        }
        for t in traces if t.get("error")
    ]
    return {
        "n": n,
        "duration": {
            "avg_ms": round(statistics.fmean(durations), 1) if durations else 0.0,
            "p50_ms": round(_pct(durations, 0.5), 1),
            "p95_ms": round(_pct(durations, 0.95), 1),
        },
        "tokens": {
            "avg": round(statistics.fmean(tokens), 1) if tokens else 0.0,
            "total": sum(tokens),
        },
        "llm_calls_avg": round(statistics.fmean(llm_counts), 2) if llm_counts else 0.0,
        "intents": dict(intents.most_common()),
        "executions": dict(executions.most_common()),
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "top_tools": dict(tool_names.most_common(8)),
        "errors": errors,
    }


def _print_report(s: dict) -> None:
    bar = "=" * 72
    print(bar)
    print(f"  业务链观测汇总（{s['n']} 条请求）")
    print(bar)
    if s["n"] == 0:
        print("  无数据（检查日志目录或日期）")
        return
    print(f"  延迟      avg {s['duration']['avg_ms']}ms  p50 {s['duration']['p50_ms']}ms  p95 {s['duration']['p95_ms']}ms")
    print(f"  Token     avg {s['tokens']['avg']}  总 {s['tokens']['total']}  LLM 调用/请求 {s['llm_calls_avg']}")
    print(f"  工具调用  {s['tool_calls']} 次（失败 {s['tool_failures']}）")
    print(f"  意图分布  {s['intents']}")
    print(f"  执行路径  {s['executions']}")
    if s["top_tools"]:
        print(f"  Top 工具  {s['top_tools']}")
    if s["errors"]:
        print(f"  错误      {len(s['errors'])} 条")
        for e in s["errors"][:10]:
            print(f"    {e['request_id']} [{e['intent']}] {e['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="业务链监控日志查询")
    parser.add_argument("--log-dir", default=settings.observability_log_dir)
    parser.add_argument("--date", default=None, help="如 2026-08-20")
    parser.add_argument("--last", type=int, default=None, help="仅最近 N 条")
    args = parser.parse_args()

    traces = load_traces(args.log_dir, date_str=args.date, last=args.last)
    _print_report(summarize(traces))


if __name__ == "__main__":
    main()
