"""Export the existing labeled v4 splits to ShareGPT; never generate new dialogues.

Run from the repository root. --check verifies reproducibility without writing.
LLaMA-Factory must use mask_history=true and train_on_prompt=false so historical
customer-service replies are context, not classification training targets.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.data_plan.taxonomy import TAXONOMY_VERSION
from app.prompts.intent_system import SYSTEM_PROMPT, with_intent_system
from scripts.intent_dataset_validation import validate_splits
from scripts.eval_intent_model import prepare_example
from scripts.train_intent_lora import validate_training_row

DIRECTORY = ROOT / 'data/fine_tuning/multiturn_intent_sft'
ROLES = {'system': 'system', 'user': 'human', 'assistant': 'gpt'}


def to_sharegpt(row: dict) -> dict:
    context, label = prepare_example(row)
    answer = json.dumps({'intent': label}, ensure_ascii=False, separators=(',', ':'))
    messages = [*with_intent_system(context), {'role': 'assistant', 'content': answer}]
    validate_training_row({**row, 'messages': messages})
    # Provenance remains available for auditing but is not mapped into model input.
    return {
        **{k: v for k, v in row.items() if k not in {'messages', 'expected_intent'}},
        'conversations': [{'from': ROLES[m['role']], 'value': m['content']} for m in messages],
    }


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def export(input_dir: Path, output_dir: Path, check: bool = False) -> dict:
    splits, input_hashes = {}, {}
    for split in ('train', 'dev', 'test'):
        path = input_dir / f'{split}_v4.jsonl'
        raw = path.read_bytes()
        input_hashes[path.name] = sha256(raw)
        rows = []
        for line_number, line in enumerate(raw.decode('utf-8-sig').splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if row.get('split') != split:
                    raise ValueError('sample split does not match input file')
                prepare_example(row)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f'{path}:{line_number}: {exc}') from exc
            rows.append(row)
        splits[split] = rows
    validate_splits(splits)

    info_path = output_dir / 'dataset_info.json'
    info = json.loads(info_path.read_text(encoding='utf-8')) if info_path.exists() else {}
    artifacts = {}
    report = {
        'taxonomy_version': TAXONOMY_VERSION,
        'system_prompt_sha256': sha256(SYSTEM_PROMPT.encode('utf-8')),
        'input_sha256': input_hashes,
        'conversion': 'Existing v4 samples only; no new dialogues, relabeling or split changes.',
        'provenance_note': 'The v4 corpus is the existing adapted seed dataset, not the unannotated CSDS extraction.',
        'supervision': {'mask_history': True, 'train_on_prompt': False, 'target': 'last assistant intent JSON only'},
        'splits': {},
    }
    for split, rows in splits.items():
        converted = [to_sharegpt(row) for row in rows]
        # Round-trip validation protects original role/content, target and metadata.
        for original, result in zip(rows, converted, strict=True):
            restored = [{'role': next(k for k, v in ROLES.items() if v == m['from']),
                         'content': m['value']} for m in result['conversations']]
            context, label = prepare_example(original)
            if restored[:-1] != with_intent_system(context) or json.loads(restored[-1]['content']) != {'intent': label}:
                raise ValueError(f'Conversion changed dialogue or label: {original["id"]}')
        name = f'{split}_v4_sharegpt.jsonl'
        body = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in converted)
        artifacts[output_dir / name] = body
        info[f'ecom_intent_v4_{split}'] = {
            'file_name': name, 'formatting': 'sharegpt',
            'columns': {'messages': 'conversations'},
            'tags': {'role_tag': 'from', 'content_tag': 'value',
                     'user_tag': 'human', 'assistant_tag': 'gpt', 'system_tag': 'system'},
        }
        report['splits'][split] = {
            'file': name, 'rows': len(rows),
            'labels': dict(sorted(Counter(prepare_example(row)[1] for row in rows).items())),
            'sha256': sha256(body.encode('utf-8')),
        }
    artifacts[info_path] = json_text(info)
    artifacts[output_dir / 'sharegpt_export_report_v4.json'] = json_text(report)
    # Validate every split before creating any output. Original files are read only.
    if not check:
        output_dir.mkdir(parents=True, exist_ok=True)
    for path, body in artifacts.items():
        encoded = body.encode('utf-8')
        if check:
            if not path.exists() or path.read_bytes() != encoded:
                raise ValueError(f'Export missing or outdated: {path}')
        else:
            path.write_bytes(encoded)
    for filename, expected in input_hashes.items():
        if sha256((input_dir / filename).read_bytes()) != expected:
            raise ValueError(f'Source changed during export: {filename}')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, default=DIRECTORY)
    parser.add_argument('--output-dir', type=Path, default=DIRECTORY)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    report = export(args.input_dir, args.output_dir, args.check)
    print(json.dumps({split: {'rows': stats['rows'], 'labels': len(stats['labels']), 'file': stats['file']}
                      for split, stats in report['splits'].items()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
