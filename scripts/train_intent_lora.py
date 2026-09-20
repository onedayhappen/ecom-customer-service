"""使用 LoRA 微调 Qwen 意图分类器。

模型权重和训练输出应放在仓库外。训练集只包含合成客服对话，默认使用
data/fine_tuning/multiturn_intent_sft/data_v3.jsonl。
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Support both direct script execution and python -m scripts.*.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prompts.intent_system import SYSTEM_PROMPT, with_intent_system  # noqa: E402


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} 不是合法 JSONL") from exc
        if not isinstance(row.get("messages"), list) or len(row["messages"]) < 2:
            raise ValueError(f"{path}:{line_no} 缺少 messages")
        rows.append(row)
    return rows


class IntentDataset:
    def __init__(self, rows: list[dict[str, Any]], tokenizer: Any, max_length: int) -> None:
        self.items: list[dict[str, list[int]]] = []
        for row in rows:
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
    parser.add_argument("--train-file", type=Path, default=Path("data/fine_tuning/multiturn_intent_sft/data_v3.jsonl"))
    parser.add_argument("--eval-file", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("../intent-model/intent-lora"))
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=8)
    args = parser.parse_args()

    if not args.train_file.exists():
        raise SystemExit(f"训练集不存在：{args.train_file}")
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
    train_set = IntentDataset(_read_jsonl(args.train_file), tokenizer, args.max_length)
    eval_set = None
    if args.eval_file:
        eval_set = IntentDataset(_read_jsonl(args.eval_file), tokenizer, args.max_length)
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
    print(f"LoRA adapter 已写入仓库外目录：{args.output_dir}")


if __name__ == "__main__":
    main()
