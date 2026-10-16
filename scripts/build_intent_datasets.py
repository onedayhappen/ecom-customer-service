"""Build the source-attributed single-focus multi-turn seed corpus.

No random splitting or bulk paraphrase generation: each authored dialogue has an
explicit split. This is an adapted seed corpus, not original customer chat logs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.data_plan.taxonomy import TAXONOMY_VERSION, VALID_INTENTS
from scripts.train_intent_lora import normalized_dialogue, validate_training_row

DIRECTORY = ROOT / 'data/fine_tuning/multiturn_intent_sft'
DATASET_VERSION = 'single-focus-last-user-v1'
SPLITS = {'T': 'train', 'D': 'dev', 'E': 'test'}
# The public subset has no matching narrowly defined intent for these goals.
# Its related utterances motivate extensions; they are not translations/relabels.
EXTENSIONS = {'promotion_calculation', 'cart_optimization', 'price_history', 'greetings'}


def build_rows(directory: Path = DIRECTORY) -> dict[str, list[dict]]:
    sources = json.loads((directory / 'sources_v4.json').read_text(encoding='utf-8'))
    result = {split: [] for split in SPLITS.values()}
    counts = Counter()
    intent = None
    for line_no, raw in enumerate((directory / 'seed_dialogues_v4.txt').read_text(encoding='utf-8').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            intent = line[1:-1]
            if intent not in VALID_INTENTS:
                raise ValueError(f'line {line_no}: unknown intent {intent}')
            continue
        if intent is None or line[:2] not in {'T ', 'D ', 'E '}:
            raise ValueError(f'line {line_no}: invalid seed row')
        split = SPLITS[line[0]]
        utterances = line[2:].split(' | ')
        if len(utterances) < 3 or len(utterances) % 2 != 1 or any(not u.strip() for u in utterances):
            raise ValueError(f'line {line_no}: must end in user, with at least two user turns')
        messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': u}
                    for i, u in enumerate(utterances)]
        orders = set(re.findall(r'\bORD-[A-Za-z0-9-]+\b', ' '.join(utterances)))
        if len(orders) > 1:
            raise ValueError(f'line {line_no}: multiple explicit orders')
        anchor = sources['anchors'][intent][split]
        counts[split, intent] += 1
        row = {
            'id': f"v4_{split}_{intent}_{counts[split, intent]:03}",
            'taxonomy_version': TAXONOMY_VERSION,
            'dataset_version': DATASET_VERSION,
            'split': split,
            'family_id': f"jddc:{anchor['source_id']}",
            'source': {
                'repository': sources['repository'], 'revision': sources['revision'],
                'path': sources['files'][anchor['upstream_split']]['path'],
                'row_id': anchor['row_id'], 'line': anchor['line'],
                'sha256': anchor['sha256'], 'license': sources['license'],
            },
            'adaptation': 'scenario_extension' if intent in EXTENSIONS else 'scenario_rewrite',
            'synthetic_context': True,
            'annotation_method': 'agent_authored_not_human_reviewed',
            'focus_scope': 'one_order_or_one_consultation_goal',
            'messages': messages,
        }
        if split == 'test':
            row['expected_intent'] = intent
        else:
            row['messages'] = [*messages, {'role': 'assistant', 'content': json.dumps({'intent': intent}, separators=(',', ':'))}]
            validate_training_row(row)
        result[split].append(row)
    validate_splits(result)
    return result


def model_input(row: dict) -> list[dict]:
    return row['messages'] if row['split'] == 'test' else row['messages'][:-1]


def label_of(row: dict) -> str:
    return row.get('expected_intent') or json.loads(row['messages'][-1]['content'])['intent']


def validate_splits(splits: dict[str, list[dict]]) -> None:
    seen_ids, seen_texts, family_split = set(), {}, {}
    for split, rows in splits.items():
        if {label_of(r) for r in rows} != VALID_INTENTS:
            raise ValueError(f'{split}: missing canonical labels')
        for row in rows:
            if row['id'] in seen_ids:
                raise ValueError(f"duplicate id: {row['id']}")
            seen_ids.add(row['id'])
            family = row['family_id']
            if family in family_split and family_split[family] != split:
                raise ValueError(f'source family crosses splits: {family}')
            family_split[family] = split
            key = normalized_dialogue(model_input(row))
            if key in seen_texts:
                raise ValueError(f"duplicate input: {row['id']} and {seen_texts[key]}")
            seen_texts[key] = row['id']


def report_for(splits: dict[str, list[dict]]) -> dict:
    return {
        'dataset_version': DATASET_VERSION,
        'provenance': 'Public JDDC utterance anchors, newly authored multi-turn rewrites; not real chat histories.',
        'limitations': [
            'Small balanced seed corpus; not enough to establish production accuracy.',
            'No original conversation IDs in source select.txt; original conversation isolation is unverified.',
            'Source/family and normalized exact-input overlap checked; semantic near-duplicates still require review.',
            'Labels are agent-authored and require independent human review before final evaluation.',
        ],
        'splits': {split: {
            'total': len(rows),
            'labels': dict(sorted(Counter(label_of(row) for row in rows).items())),
            'input_message_lengths': dict(sorted(Counter(len(model_input(row)) for row in rows).items())),
            'source_families': len({row['family_id'] for row in rows}),
            'scenario_extensions': sum(row['adaptation'] == 'scenario_extension' for row in rows),
        } for split, rows in splits.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate that generated files match the authored source; no writes')
    args = parser.parse_args()
    splits = build_rows()
    artifacts = {DIRECTORY / f'{split}_v4.jsonl': ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
                 for split, rows in splits.items()}
    report = report_for(splits)
    report['sha256'] = {path.name: hashlib.sha256(body.encode('utf-8')).hexdigest() for path, body in artifacts.items()}
    artifacts[DIRECTORY / 'dataset_report_v4.json'] = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    for path, body in artifacts.items():
        if args.check:
            if not path.is_file() or path.read_text(encoding='utf-8') != body:
                raise SystemExit(f'Generated file differs: {path}; rerun without --check')
        else:
            path.write_text(body, encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
