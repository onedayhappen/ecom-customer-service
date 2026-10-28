"""Compare deterministic extraction/grounding with a Git baseline; no network.

Run: python scripts/benchmark_memory_validation.py --baseline-ref <commit> --output report.json
Both versions receive identical valid inputs and must return identical slots.
Timing excludes model inference, storage and network; it is not an online SLA.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from statistics import median, quantiles
import subprocess
import sys
from time import perf_counter_ns
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.memory import atomic, extraction, runtime
from app.session.context_budget import estimate_text_tokens


@contextmanager
def modules(overrides):
    prior = {name: sys.modules.get(name) for name in overrides}
    sys.modules.update(overrides)
    try:
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def baseline(ref):
    loaded = {}
    for path in ('app/memory/atomic.py', 'app/prompts/memory.py', 'app/memory/extraction.py', 'app/memory/runtime.py'):
        code = subprocess.check_output(['git', 'show', f'{ref}:{path}'], cwd=ROOT).decode('utf-8')
        name = path[:-3].replace('/', '.')
        module = ModuleType(name)
        with modules(loaded):
            exec(compile(code, f'{ref}:{path}', 'exec'), module.__dict__)
        loaded[name] = module
    return loaded


def signature(facts):
    return [(f.predicate, f.value, f.qualifiers, f.source_span) for f in facts]


def measure(fn, iterations):
    for _ in range(10):
        fn()
    samples = []
    for _ in range(21):
        start = perf_counter_ns()
        for _ in range(iterations):
            fn()
        samples.append((perf_counter_ns() - start) / iterations / 1e6)
    return dict(median_ms=round(median(samples), 4), p95_ms=round(quantiles(samples, n=20)[18], 4),
                samples=len(samples), iterations_per_sample=iterations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    old = baseline(args.baseline_ref)
    cases = json.loads((ROOT / 'tests/fixtures/memory_dialogue_cases.json').read_text(encoding='utf-8'))
    messages, candidates = [], []
    for case in cases[:2]:
        for item in case['response']['facts']:
            item = atomic.normalize_candidate(item, case['messages'])
            assert item is not None
            item.pop('content')
            item.pop('memory_key')
            item['source_index'] += len(messages)
            candidates.append(item)
        messages.extend(case['messages'])
    report = dict(baseline_ref=args.baseline_ref, scope='offline fixed valid candidates; no model/network/storage',
                  batches={}, real_cases={})
    for copies, iterations in ((1, 40), (16, 3)):
        data = dict(schema_version=2, facts=deepcopy(candidates * copies))
        new_fn = lambda: runtime._ground_facts(extraction._atomic_facts(data, messages), messages, 'benchmark')
        new_facts = new_fn()
        with modules(old):
            old_fn = lambda: old['app.memory.runtime']._ground_facts(
                old['app.memory.extraction']._atomic_facts(data, messages), messages, 'benchmark')
            assert signature(old_fn()) == signature(new_facts)
            before = measure(old_fn, iterations)
        after = measure(new_fn, iterations)
        report['batches'][len(data['facts'])] = dict(before=before, after=after,
            median_ratio=round(after['median_ms'] / before['median_ms'], 4))
    for case in cases:
        batch = extraction._atomic_facts(case['response'], case['messages'])
        report['real_cases'][case['session']] = dict(accepted=len(batch), deferred=len(batch.issues))
    from app.prompts.memory import ATOMIC_EXTRACTION_PROMPT
    report['instruction_estimated_tokens'] = dict(
        before=estimate_text_tokens(old['app.prompts.memory'].ATOMIC_EXTRACTION_PROMPT),
        after=estimate_text_tokens(ATOMIC_EXTRACTION_PROMPT))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
