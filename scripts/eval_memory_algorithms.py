"""固定候选的长期记忆离线回归，不调用 LLM、embedding 服务或生产存储。

Usage: python scripts/eval_memory_algorithms.py [--fixture PATH] [--output PATH]
退出码 0 表示预期全部满足，1 表示回归失败，2 表示 fixture 格式错误。
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.memory.atomic import AtomicValidationError, ground_time, normalize_candidate
from app.memory.long_term import MemoryFact
from app.memory.reconciliation import reconcile
from app.memory.retrieval import MemoryRetriever
from app.session.context_budget import estimate_text_tokens


def _signature(value):
    if isinstance(value, MemoryFact):
        value = {key: getattr(value, key) for key in ('predicate', 'value', 'qualifiers', 'polarity')}
    return json.dumps({"predicate": value['predicate'], "value": value['value'],
                       "qualifiers": value.get('qualifiers', {}), "polarity": value.get('polarity', 'positive')},
                      sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _fact(normalized, sources):
    """固定事件元数据与精确引文按线上相同字段组成领域候选。"""
    source = sources[normalized['source_index']]
    quote = normalized['source_quote']
    event_id, observed_at = source['event_id'], source['created_at']
    start = source['content'].index(quote)
    temporal = ground_time(normalized.get('valid_time_quote', ''), observed_at)
    return MemoryFact(
        content=normalized['content'], category='preference', created_at=observed_at,
        updated_at=observed_at, observed_at=observed_at, schema_version=2,
        predicate=normalized['predicate'], value=normalized['value'], qualifiers=normalized['qualifiers'],
        polarity=normalized['polarity'], operation=normalized['operation'], memory_key=normalized['memory_key'],
        source_type='user_explicit', source_event_id=event_id, source_session=event_id.rpartition(':')[0],
        source_span=quote, confidence=normalized['confidence'], **temporal,
        evidence=[{'source_event_id': event_id, 'source_session': event_id.rpartition(':')[0],
                   'source_span': quote, 'observed_at': observed_at,
                   'start_offset': start, 'end_offset': start + len(quote)}],
    )


def _ratio(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def evaluate(fixture):
    if fixture.get('schema_version') != 1 or not fixture.get('cases'):
        raise ValueError('fixture 需要 schema_version=1 和非空 cases')
    now = datetime.fromisoformat(fixture['evaluate_at'])
    if now.tzinfo is None:
        raise ValueError('evaluate_at 必须带时区')
    retriever = MemoryRetriever(model_path=None, enabled=False)
    counts, results, failures = Counter(), [], []
    for case in fixture['cases']:
        sources, ledger, candidates = case['sources'], [], {}
        detail = {'id': case['id'], 'admissions': [], 'queries': []}
        for example in case['candidates']:
            candidate = deepcopy(candidates[example['repeat_of']] if 'repeat_of' in example else example['candidate'])
            candidates[example['id']] = candidate
            normalized, error = None, ''
            try:
                normalized = normalize_candidate(candidate, sources)
                actual = 'accepted' if normalized is not None else 'ignored'
            except AtomicValidationError as exception:
                actual, error = 'invalid', str(exception)
            counts['candidates'] += 1
            counts['admission_correct'] += actual == example['expected_admission']
            counts['admission_' + actual] += 1
            admission = {'id': example['id'], 'expected': example['expected_admission'], 'actual': actual}
            if error:
                admission['reason'] = error
            if actual != example['expected_admission']:
                failures.append(f"{case['id']}/{example['id']}: admission {actual}")
            if normalized is not None:
                fact = _fact(normalized, sources)
                source = sources[normalized['source_index']]
                evidence = fact.evidence[0]
                grounded = (source['role'] == 'user' and source['content'][
                    evidence['start_offset']:evidence['end_offset']] == evidence['source_span'])
                counts['evidence_checked'] += 1
                counts['evidence_grounded'] += grounded
                if not grounded:
                    failures.append(f"{case['id']}/{example['id']}: ungrounded evidence")
                ledger, operations = reconcile(ledger, [fact], user_id='offline:' + case['id'],
                                                now=now, hot_limit=case.get('hot_limit', 20))
                admission['operation'] = operations[-1]['operation']
                if 'expected_operation' in example:
                    counts['operations'] += 1
                    correct = admission['operation'] == example['expected_operation']
                    counts['operation_correct'] += correct
                    if not correct:
                        failures.append(f"{case['id']}/{example['id']}: operation {admission['operation']}")
            detail['admissions'].append(admission)
        active = [fact for fact in ledger if fact.status == 'active']
        active_signatures = [_signature(fact) for fact in active]
        expected_active = [_signature(value) for value in case['expected_active']]
        snapshot_correct = Counter(active_signatures) == Counter(expected_active)
        counts['snapshots'] += 1
        counts['snapshot_correct'] += snapshot_correct
        if not snapshot_correct:
            failures.append(f"{case['id']}: current fact set mismatch")
        counts['active_facts'] += len(active)
        counts['duplicate_active'] += len(active_signatures) - len(set(active_signatures))
        detail['active'] = [json.loads(value) for value in sorted(active_signatures)]
        detail['ledger_versions'] = len(ledger)
        detail['active_cold'] = sum(fact.metadata.get('tier') == 'cold' for fact in active)
        if 'expected_active_cold' in case and detail['active_cold'] != case['expected_active_cold']:
            failures.append(f"{case['id']}: cold active count mismatch")
        for query in case.get('queries', []):
            mode = query['mode']
            if mode not in {'current', 'historical'}:
                raise ValueError('query.mode 仅支持 current/historical')
            as_of = query['as_of'] if mode == 'historical' else fixture['evaluate_at']
            returned = retriever.retrieve(query['query'], ledger, query.get('top_k', 4), as_of=as_of)
            actual = {_signature(fact) for fact in returned}
            expected = {_signature(value) for value in query['expected']}
            hits = len(actual & expected)
            counts[mode + '_hits'] += hits
            counts[mode + '_expected'] += len(expected)
            counts[mode + '_returned'] += len(actual)
            counts['retrieved_rows'] += len(returned)
            counts['duplicate_returned'] += len(returned) - len({(f.memory_id, f.version) for f in returned})
            if not expected:
                counts['empty_queries'] += 1
                counts['empty_correct'] += not returned
            if actual != expected:
                failures.append(f"{case['id']}/{query['id']}: retrieval set mismatch")
            detail['queries'].append({'id': query['id'], 'mode': mode, 'as_of': as_of,
                                       'hits': hits, 'expected_count': len(expected),
                                       'returned': [json.loads(value) for value in sorted(actual)]})
        raw = '\n'.join(source['content'] for source in sources if source['role'] == 'user')
        rendered = '\n'.join(fact.content for fact in sorted(active, key=lambda fact: fact.memory_key))
        raw_tokens, rendered_tokens = estimate_text_tokens(raw), estimate_text_tokens(rendered)
        counts['raw_tokens'] += raw_tokens
        counts['rendered_tokens'] += rendered_tokens
        detail['representation'] = {'raw_estimated_tokens': raw_tokens, 'rendered_estimated_tokens': rendered_tokens,
                                     'rendered': rendered}
        results.append(detail)
    return {
        'evaluation': 'fixed_candidate_deterministic_offline_regression',
        'scope': '固定中文候选；准入、证据、归一、更新、词法/槽位检索。未调用 LLM 或 embedding，不能代表真实抽取准确率或线上效果。',
        'evaluate_at': fixture['evaluate_at'], 'passed': not failures, 'case_count': len(results),
        'metrics': {
            'schema_evidence_admission_accuracy': _ratio(counts['admission_correct'], counts['candidates']),
            'admission_counts': {key: counts['admission_' + key] for key in ('accepted', 'ignored', 'invalid')},
            'accepted_evidence_grounding_rate': _ratio(counts['evidence_grounded'], counts['evidence_checked']),
            'update_operation_accuracy': _ratio(counts['operation_correct'], counts['operations']),
            'current_snapshot_accuracy': _ratio(counts['snapshot_correct'], counts['snapshots']),
            'current_recall': _ratio(counts['current_hits'], counts['current_expected']),
            'current_precision': _ratio(counts['current_hits'], counts['current_returned']),
            'historical_recall': _ratio(counts['historical_hits'], counts['historical_expected']),
            'historical_precision': _ratio(counts['historical_hits'], counts['historical_returned']),
            'empty_result_accuracy': _ratio(counts['empty_correct'], counts['empty_queries']),
            'active_duplicate_rate': _ratio(counts['duplicate_active'], counts['active_facts']),
            'retrieval_duplicate_rate': _ratio(counts['duplicate_returned'], counts['retrieved_rows']),
            'token_compression': {
                'raw_user_text_estimated_tokens': counts['raw_tokens'],
                'rendered_current_facts_estimated_tokens': counts['rendered_tokens'],
                'reduction_fraction': _ratio(counts['raw_tokens'] - counts['rendered_tokens'], counts['raw_tokens']),
                'method': '项目 estimate_text_tokens 启发式；fixture 用户原文 → 当前事实文本。不含版本账本、证据、系统包装或工具开销，不是账单 token 或线上压缩率。',
            },
        },
        'counts': dict(counts), 'failures': failures, 'cases': results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=ROOT / 'data/evaluation/long_term_memory_v2.json')
    parser.add_argument('--output', type=Path, help='可选 JSON 报告路径；默认只输出到终端')
    args = parser.parse_args(argv)
    try:
        report = evaluate(json.loads(args.fixture.read_text(encoding='utf-8')))
    except (KeyError, TypeError, ValueError, OSError) as error:
        parser.error(f'无法运行离线 fixture：{error}')
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + '\n', encoding='utf-8')
    print(serialized)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
