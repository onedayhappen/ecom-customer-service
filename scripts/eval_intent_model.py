"""在独立评测集上评估外部 LoRA 意图分类器。"""

from __future__ import annotations

import argparse
import json
import sys
import re
from pathlib import Path
from typing import Any

_INTENT_RE = re.compile(r'"intent"\s*:\s*"([a-z_]+)"')
# Support both direct script execution and python -m scripts.*.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prompts.intent_system import SYSTEM_PROMPT, with_intent_system  # noqa: E402


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="基座模型目录或 Hugging Face 模型名")
    parser.add_argument("--adapter", required=True, help="LoRA adapter 目录；建议放在仓库外")
    parser.add_argument("--eval-file", type=Path, default=Path("data/evaluation/multiturn_intent_eval_v3.jsonl"))
    parser.add_argument("--max-new-tokens", type=int, default=32)
    args = parser.parse_args()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(args.model, trust_remote_code=True)
    model = PeftModel.from_pretrained(model, args.adapter).eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    correct = 0
    rows = _read_jsonl(args.eval_file)
    details: list[dict[str, Any]] = []
    for row in rows:
        messages = row["messages"]
        messages = with_intent_system(messages)
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(device)
        with torch.no_grad():
            output = model.generate(**inputs, max_new_tokens=args.max_new_tokens, do_sample=False)
        generated = tokenizer.decode(output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        match = _INTENT_RE.search(generated)
        predicted = match.group(1) if match else None
        expected = row.get("expected_intent") or messages[-1].get("content", "")
        if isinstance(expected, str) and expected.startswith("{"):
            try:
                expected = json.loads(expected).get("intent")
            except json.JSONDecodeError:
                expected = None
        ok = predicted == expected
        correct += int(ok)
        details.append({"id": row.get("id"), "expected": expected, "predicted": predicted, "ok": ok})
    accuracy = correct / len(rows) if rows else 0.0
    print(json.dumps({"total": len(rows), "correct": correct, "accuracy": accuracy, "details": details}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
