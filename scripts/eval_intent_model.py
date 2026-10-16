"""在独立评测集上评估外部 LoRA 意图分类器。"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from pathlib import Path
from typing import Any

# Support both direct script execution and python -m scripts.*.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prompts.intent_system import with_intent_system
from app.data_plan.taxonomy import TAXONOMY_VERSION, VALID_INTENTS, validate_manifest
from app.intent.classifier import parse_intent
from app.intent.rules import recognize


def prepare_example(row):
    if not isinstance(row, dict) or not isinstance(row.get('messages'), list):
        raise ValueError('评测样本必须包含 messages 数组')
    if row.get('taxonomy_version', TAXONOMY_VERSION) != TAXONOMY_VERSION:
        raise ValueError('评测样本标签版本不匹配')
    if any(not isinstance(m, dict) or not isinstance(m.get('content'), str)
           or not m['content'].strip() for m in row['messages']):
        raise ValueError('评测消息必须是非空文本')
    messages = [dict(m) for m in row['messages']]
    expected = row.get('expected_intent')
    if messages and messages[-1].get('role') == 'assistant':
        if not strict_output_valid(messages[-1]['content']):
            raise ValueError('末尾分类答案只能包含一个 intent 标签')
        target = parse_intent(messages[-1].get('content', ''))
        if target is None or expected is not None and target != expected:
            raise ValueError('末尾 assistant 必须是与 expected_intent 一致的分类标签')
        expected = target
        messages.pop()  # Never leak the gold answer into the inference prompt.
    if not isinstance(expected, str) or expected not in VALID_INTENTS:
        raise ValueError('评测标签不属于当前 31 类')
    if not messages or messages[-1].get('role') != 'user':
        raise ValueError('评测输入必须结束于当前用户消息')
    dialogue = messages[1:] if messages[0].get('role') == 'system' else messages
    if any(m.get('role') != ('user' if i % 2 == 0 else 'assistant') for i, m in enumerate(dialogue)):
        raise ValueError('历史用户和客服角色必须交替')
    if row.get('dataset_version') == 'single-focus-last-user-v1' and len(dialogue) < 3:
        raise ValueError('新版评测输入必须包含多轮上下文')
    return messages, expected


def classification_metrics(details: list[dict]) -> dict:
    """Macro metrics expose failures in rare/easily confused business labels."""
    per_class = {}
    for label in sorted(VALID_INTENTS):
        tp = sum(d['expected'] == label and d['predicted'] == label for d in details)
        support = sum(d['expected'] == label for d in details)
        predicted = sum(d['predicted'] == label for d in details)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = dict(support=support, precision=precision, recall=recall, f1=f1)
    supported = [v for v in per_class.values() if v['support']]
    confusions = Counter((d['expected'], d['predicted']) for d in details if d['expected'] != d['predicted'])
    total = len(details)
    correct = sum(d['expected'] == d['predicted'] for d in details)
    slices = {}
    for kind in sorted({d.get('adaptation', 'unspecified') for d in details}):
        subset = [d for d in details if d.get('adaptation', 'unspecified') == kind]
        slices[kind] = {'total': len(subset), 'accuracy': sum(d['ok'] for d in subset) / len(subset)}
    return {
        'total': total, 'correct': correct, 'accuracy': correct / total if total else 0.0,
        'macro_f1': sum(v['f1'] for v in supported) / len(supported) if supported else 0.0,
        'macro_f1_scope': 'labels_with_test_support',
        'invalid_output_count': sum(not d.get('format_valid', d['predicted'] in VALID_INTENTS) for d in details),
        'per_class': per_class,
        'confusions': [{'expected': a, 'predicted': b, 'count': n} for (a, b), n in confusions.most_common()],
        'slices': slices,
    }


def strict_output_valid(text: str) -> bool:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    return (isinstance(value, dict) and set(value) == {'intent'}
            and isinstance(value['intent'], str) and value['intent'] in VALID_INTENTS)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="基座模型目录或 Hugging Face 模型名")
    parser.add_argument("--adapter", help="LoRA adapter 目录；建议放在仓库外")
    parser.add_argument("--eval-file", type=Path, required=True, help="显式指定评测文件；旧种子集已弃用，不再默认加载")
    parser.add_argument("--base-only", action="store_true", help="不加载 adapter，评估基座模型作为对照")
    parser.add_argument("--validate-only", action="store_true", help="只验证数据，不预测测试标签")
    parser.add_argument("--output-file", type=Path, help="评测报告路径；建议使用被忽略的 logs 目录")
    parser.add_argument("--max-new-tokens", type=int, default=32)
    parser.add_argument("--rules", action="store_true", help="只评测规则兜底，不加载模型")
    args = parser.parse_args()
    rows = _read_jsonl(args.eval_file)
    examples = [prepare_example(row) for row in rows]
    if not examples:
        parser.error("评测集不能为空")
    if args.output_file and args.output_file.resolve() == args.eval_file.resolve():
        parser.error('报告不能覆盖评测数据集')
    if args.validate_only:
        print(json.dumps({'total': len(examples), 'labels': dict(sorted(Counter(e for _, e in examples).items()))}, ensure_ascii=False))
        return
    if args.rules and args.base_only:
        parser.error('--rules 与 --base-only 不能同时使用')

    if not args.rules:
        if not args.model or not args.base_only and not args.adapter:
            parser.error('模型评测需要 --model，以及 --adapter 或 --base-only')
        if args.base_only and args.adapter:
            parser.error('--base-only 不能同时指定 --adapter')
        if args.adapter:
            validate_manifest(Path(args.adapter))
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(args.model, trust_remote_code=True)
        if args.adapter:
            model = PeftModel.from_pretrained(model, args.adapter)
        model.eval()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(device)
    details = []
    for row, (messages, expected) in zip(rows, examples):
        if args.rules:
            predicted = recognize(messages[-1]['content'], messages=messages).value
            format_valid = True
        else:
            prompt = tokenizer.apply_chat_template(with_intent_system(messages), tokenize=False, add_generation_prompt=True)
            inputs = tokenizer(prompt, return_tensors="pt").to(device)
            with torch.no_grad():
                output = model.generate(**inputs, max_new_tokens=args.max_new_tokens, do_sample=False)
            generated = tokenizer.decode(output[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True)
            predicted = parse_intent(generated)
            format_valid = strict_output_valid(generated)
        ok = predicted == expected
        details.append({'id': row.get('id'), 'expected': expected, 'predicted': predicted, 'ok': ok,
                        'format_valid': format_valid, 'adaptation': row.get('adaptation', 'unspecified')})
    result = {'mode': 'rules' if args.rules else ('base' if args.base_only else 'adapter'),
              'eval_file': str(args.eval_file), **classification_metrics(details), 'details': details}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_file:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        args.output_file.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)


if __name__ == "__main__":
    main()
