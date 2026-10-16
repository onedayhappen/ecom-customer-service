"""使用 LoRA 微调 Qwen 意图分类器。

模型权重和训练输出应放在仓库外。必须显式提供按当前 31 类重新标注的
训练集；旧 data_v3.jsonl 仅保留为历史基线，不可直接训练本版模型。
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Support both direct script execution and python -m scripts.*.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prompts.intent_system import SYSTEM_PROMPT, with_intent_system  # noqa: E402
from app.data_plan.taxonomy import TAXONOMY_VERSION, VALID_INTENTS, write_manifest


def validate_training_row(row: dict) -> None:
    if not isinstance(row, dict):
        raise ValueError("训练样本必须为对象")
    if row.get("taxonomy_version", TAXONOMY_VERSION) != TAXONOMY_VERSION:
        raise ValueError("训练样本标签版本不匹配")
    messages = row.get("messages", [])
    if (not isinstance(messages, list) or len(messages) < 2
            or any(not isinstance(m, dict) or not isinstance(m.get('content'), str)
                   or not m['content'].strip() for m in messages)
            or messages[-1].get("role") != "assistant"
            or messages[-2].get("role") != "user"):
        raise ValueError("训练样本必须以当前 user 消息和 assistant 分类标签结尾")
    dialogue = messages[1:] if messages[0].get('role') == 'system' else messages
    if any(m.get('role') != ('user' if i % 2 == 0 else 'assistant') for i, m in enumerate(dialogue)):
        raise ValueError('用户和客服必须交替，不能把客服追问写成用户输入')
    if row.get('dataset_version') == 'single-focus-last-user-v1' and len(dialogue) < 4:
        raise ValueError('新版样本必须至少包含两条用户消息和一条历史客服回复')
    try:
        target = json.loads(messages[-1]["content"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("训练标签必须为 JSON") from exc
    if (not isinstance(target, dict) or set(target) != {'intent'}
            or not isinstance(target.get('intent'), str) or target['intent'] not in VALID_INTENTS):
        raise ValueError("训练标签不属于当前 31 类；请重新标注旧数据")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} 不是合法 JSONL") from exc
        if not isinstance(row, dict) or not isinstance(row.get("messages"), list) or len(row["messages"]) < 2:
            raise ValueError(f"{path}:{line_no} 缺少 messages")
        validate_training_row(row)
        rows.append(row)
    if not rows:
        raise ValueError("训练集不能为空")
    return rows


def normalized_dialogue(messages: list[dict]) -> str:
    """Normalize order IDs/numbers/punctuation for split-overlap validation."""
    text = '\n'.join(f"{m['role']}:{m['content']}" for m in messages if m['role'] != 'system')
    text = unicodedata.normalize('NFKC', text).lower()
    text = re.sub(r'ord-[a-z0-9-]+', '<order>', text)
    text = re.sub(r'\d+', '<number>', text)
    return re.sub(r'[\s，。！？、,.!?；;：:]+', '', text)


def validate_dataset_pair(train_rows: list[dict], eval_rows: list[dict] | None) -> None:
    """Keep held-out test data and source families out of training/selection."""
    for rows, expected_split in ((train_rows, 'train'), (eval_rows or [], 'dev')):
        for row in rows:
            if row.get('split') not in {None, expected_split}:
                raise ValueError(f"{expected_split} 输入不能使用 split={row.get('split')} 的数据")
    labels = {json.loads(row['messages'][-1]['content'])['intent'] for row in train_rows}
    if labels != VALID_INTENTS:
        raise ValueError(f"训练集缺少当前标签：{sorted(VALID_INTENTS - labels)}")
    train_keys = {normalized_dialogue(row['messages'][:-1]) for row in train_rows}
    train_families = {row['family_id'] for row in train_rows if row.get('family_id')}
    for row in eval_rows or []:
        if normalized_dialogue(row['messages'][:-1]) in train_keys:
            raise ValueError('训练与验证集存在重复对话')
        if row.get('family_id') in train_families:
            raise ValueError('训练与验证集存在相同来源场景，不能用于独立验证')


class IntentDataset:
    def __init__(self, rows: list[dict[str, Any]], tokenizer: Any, max_length: int) -> None:
        self.items: list[dict[str, list[int]]] = []
        for row in rows:
            validate_training_row(row)
            messages = row["messages"]
            if messages[-1].get("role") != "assistant":
                continue
            messages = with_intent_system(messages)
            prompt_messages = messages[:-1]
            prompt = tokenizer.apply_chat_template(
                prompt_messages, tokenize=False, add_generation_prompt=True
            )
            full = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False
            )
            prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
            encoded = tokenizer(
                full,
                add_special_tokens=False,
            )
            if len(encoded["input_ids"]) > max_length:
                raise ValueError(
                    f"样本含 system prompt 后为 {len(encoded['input_ids'])} tokens，"
                    f"超过 max_length={max_length}；请增大 --max-length 或缩短对话，"
                    "不能截掉末轮用户消息或训练标签。"
                )
            labels = list(encoded["input_ids"])
            if len(prompt_ids) >= len(labels):
                raise ValueError('分词后没有可监督的末尾分类标签')
            for index in range(min(len(prompt_ids), len(labels))):
                labels[index] = -100
            self.items.append({
                "input_ids": list(encoded["input_ids"]),
                "attention_mask": list(encoded["attention_mask"]),
                "labels": labels,
            })

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> dict[str, list[int]]:
        return self.items[index]


@dataclass
class Collator:
    tokenizer: Any

    def __call__(self, features: list[dict[str, list[int]]]) -> dict[str, Any]:
        import torch

        max_len = max(len(item["input_ids"]) for item in features)
        pad_id = self.tokenizer.pad_token_id
        result: dict[str, Any] = {}
        for key, fill in (("input_ids", pad_id), ("attention_mask", 0), ("labels", -100)):
            result[key] = torch.tensor(
                [item[key] + [fill] * (max_len - len(item[key])) for item in features],
                dtype=torch.long,
            )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--train-file", type=Path, required=True, help="按当前 31 类契约重新标注的训练集")
    parser.add_argument("--eval-file", type=Path, default=None, help="仅使用 dev 验证集，不能使用最终 test 集")
    parser.add_argument("--validate-only", action="store_true", help="只校验数据与划分，不加载模型或启动训练")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=Path("../intent-model/intent-lora"))
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=8)
    args = parser.parse_args()

    if not args.train_file.exists():
        raise SystemExit(f"训练集不存在：{args.train_file}")
    train_rows = _read_jsonl(args.train_file)
    eval_rows = _read_jsonl(args.eval_file) if args.eval_file else None
    validate_dataset_pair(train_rows, eval_rows)
    if args.validate_only:
        print(json.dumps({'train': len(train_rows), 'dev': len(eval_rows or []), 'labels': len(VALID_INTENTS)}, ensure_ascii=False))
        return
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import (AutoModelForCausalLM, AutoTokenizer, Trainer,
                              TrainingArguments)

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(args.model, trust_remote_code=True)
    model = get_peft_model(
        model,
        LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        ),
    )
    train_set = IntentDataset(train_rows, tokenizer, args.max_length)
    eval_set = None
    if args.eval_file:
        eval_set = IntentDataset(eval_rows, tokenizer, args.max_length)
    args.output_dir.parent.mkdir(parents=True, exist_ok=True)
    training_kwargs = {
        "output_dir": str(args.output_dir),
        "num_train_epochs": args.epochs,
        "per_device_train_batch_size": args.batch_size,
        "gradient_accumulation_steps": args.gradient_accumulation_steps,
        "learning_rate": args.learning_rate,
        "logging_steps": 10,
        "save_strategy": "epoch",
        "report_to": [],
        "fp16": False,
        "seed": args.seed,
        "data_seed": args.seed,
    }
    training_signature = inspect.signature(TrainingArguments).parameters
    training_kwargs["eval_strategy" if "eval_strategy" in training_signature else "evaluation_strategy"] = "epoch" if eval_set else "no"
    trainer_kwargs = {
        "model": model,
        "train_dataset": train_set,
        "eval_dataset": eval_set,
        "data_collator": Collator(tokenizer),
        "args": TrainingArguments(**training_kwargs),
    }
    trainer_signature = inspect.signature(Trainer).parameters
    trainer_kwargs["processing_class" if "processing_class" in trainer_signature else "tokenizer"] = tokenizer
    trainer = Trainer(**trainer_kwargs)
    trainer.train()
    trainer.save_model(str(args.output_dir))
    tokenizer.save_pretrained(str(args.output_dir))
    write_manifest(args.output_dir)
    print(f"LoRA adapter 已写入仓库外目录：{args.output_dir}")


if __name__ == "__main__":
    main()
