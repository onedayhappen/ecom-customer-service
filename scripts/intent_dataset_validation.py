"""Validate labeled dataset splits without generating or rewriting dialogues."""
from __future__ import annotations

import json

from app.data_plan.taxonomy import VALID_INTENTS
from scripts.train_intent_lora import normalized_dialogue


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
