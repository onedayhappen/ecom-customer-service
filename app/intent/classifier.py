"""可选本地意图分类器：Qwen2.5-0.5B-Instruct + LoRA adapter。

替换 app/intent/ 的规则识别作为第一优先级：判断对话中用户最后一条消息的
意图，输出 31 类业务任务级标签。

设计要点：
- **延迟加载 + 进程内单例**：torch/transformers/peft 在首次调用时才导入，
  避免污染 API 启动路径；模型只加载一次，后续复用。
- **prompt 与训练分布对齐**：消息为「历史对话 + 当前 user 轮」，
  模型续写 assistant 输出 {"intent": "标签"}。
- **fail-safe**：依赖缺失、模型加载失败、输出解析失败、推理异常，一律返回
  None，由调用方回退规则识别，保证客服链路永远可用。
- **线程安全**：推理加锁串行化，避免 CPU 多线程并发争用 torch。
"""

from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from app.config.settings import settings
from app.data_plan.enums import Intent
from app.data_plan.taxonomy import VALID_INTENTS, validate_manifest
from app.prompts.intent_system import SYSTEM_PROMPT, with_intent_system

# torch 为可选依赖：缺失时分类器整体不可用，
# 由 recognize 回退规则；模块导入不受影响。
try:  # pragma: no cover - 环境依赖
    import torch  # noqa: F401
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]

# 与分类器契约一致的 31 类标签


# 项目根目录：app/intent/classifier.py → 上溯 3 级
ROOT = Path(__file__).resolve().parent.parent.parent


def parse_intent(text: str) -> str | None:
    """从模型输出中解析 intent 标签；格式不合法或标签不在 31 类内返回 None。"""
    text = text or ""
    start = text.find('{')
    if start < 0:
        return None
    try:
        value = json.loads(text[start:]).get('intent')
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value in VALID_INTENTS else None


def _resolve_path(path: str) -> Path:
    """相对路径优先按项目根解析，否则按当前工作目录解析。"""
    p = Path(path)
    if p.is_absolute():
        return p
    rooted = ROOT / p
    return rooted if rooted.exists() else p.resolve()


class IntentClassifier:
    """微调意图分类器（lazy load，进程内单例由 :func:`get_classifier` 管理）。"""

    def __init__(self) -> None:
        self._tokenizer: Any = None
        self._model: Any = None
        self._device: str = "cpu"
        self._lock = threading.Lock()
        self._load_error: str | None = None

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """模型是否已成功加载。"""
        return self._model is not None

    def _ensure_loaded(self) -> bool:
        """首次调用时导入依赖并加载模型；失败记录原因并返回 False。"""
        if self._model is not None:
            return True
        if self._load_error is not None:
            return False
        with self._lock:
            if self._model is not None:
                return True
            try:
                self._load()
                return True
            except Exception as e:  # noqa: BLE001 —— 任何加载失败都降级回规则
                self._load_error = f"{type(e).__name__}: {e}"
                return False

    def _load(self) -> None:
        validate_manifest(_resolve_path(settings.intent_adapter_path))
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        base = _resolve_path(settings.intent_base_model_path)
        adapter = _resolve_path(settings.intent_adapter_path)
        if not base.exists():
            raise FileNotFoundError(f"基座模型不存在: {base}")
        if not (adapter / "adapter_config.json").exists():
            raise FileNotFoundError(f"LoRA adapter 不存在: {adapter}")

        device = settings.intent_model_device or (
            "cuda" if torch.cuda.is_available() else "cpu"
        )
        # transformers>=5 弃用 torch_dtype，改用 dtype；4.x 只认 torch_dtype
        load_kwargs = {}
        try:
            import transformers
            if int(transformers.__version__.split(".")[0]) >= 5:
                load_kwargs["dtype"] = torch.float32
            else:  # pragma: no cover - 低版本兼容分支
                load_kwargs["torch_dtype"] = torch.float32
        except ImportError:  # pragma: no cover
            load_kwargs["torch_dtype"] = torch.float32

        self._tokenizer = AutoTokenizer.from_pretrained(
            str(base), trust_remote_code=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            str(base), trust_remote_code=True, **load_kwargs,
        )
        model = PeftModel.from_pretrained(model, str(adapter))
        model.to(device)
        model.eval()
        self._model = model
        self._device = device

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------
    def classify(self, messages: list[dict]) -> Intent | None:
        """对多轮对话判断末轮用户意图；任何异常返回 None（调用方回退规则）。

        messages 为 OpenAI 格式（不含 system）：[{"role": "user"/"assistant", ...}, ...]，
        最后一条必须是 user 的当前消息。
        """
        if not settings.intent_model_enabled:
            return None
        if not self._ensure_loaded():
            return None
        try:
            return self._classify(messages)
        except Exception:  # noqa: BLE001 —— 推理异常不影响客服链路
            return None

    def _classify(self, messages: list[dict]) -> Intent | None:
        history = self._trim_context(messages)
        prompt_messages = with_intent_system(history)
        inputs = self._tokenizer.apply_chat_template(
            prompt_messages, tokenize=True, add_generation_prompt=True,
            return_tensors="pt", return_dict=True,
        )["input_ids"].to(self._device)

        no_grad = torch.no_grad() if torch is not None else nullcontext()
        with self._lock, no_grad:
            outputs = self._model.generate(
                inputs,
                max_new_tokens=settings.intent_max_new_tokens,
                do_sample=False,
                pad_token_id=self._tokenizer.pad_token_id or self._tokenizer.eos_token_id,
            )
        generated = self._tokenizer.decode(
            outputs[0][inputs.shape[1]:], skip_special_tokens=True,
        )
        parsed = parse_intent(generated)
        return Intent(parsed) if parsed else None

    @staticmethod
    def _trim_context(messages: list[dict]) -> list[dict]:
        """只保留最近 N 轮上下文；若末轮不是 user 则丢弃其后的消息。"""
        msgs = [m for m in (messages or []) if isinstance(m, dict)
                and m.get("role") in {"user", "assistant"}
                and isinstance(m.get("content"), str) and m["content"]]
        if not msgs:
            return [{"role": "user", "content": ""}]
        # 末轮必须是 user（当前消息）；否则截到最近一条 user
        if msgs[-1].get("role") != "user":
            idx = len(msgs) - 1
            while idx >= 0 and msgs[idx].get("role") != "user":
                idx -= 1
            msgs = msgs[: idx + 1]
        limit = max(1, settings.intent_max_context_turns)
        return msgs[-limit:] or [{"role": "user", "content": ""}]


# 进程内单例：API 多线程共享，模型只加载一次
_classifier: IntentClassifier | None = None


def get_classifier() -> IntentClassifier:
    global _classifier
    if _classifier is None:
        _classifier = IntentClassifier()
    return _classifier


def reset_classifier() -> None:
    """重置单例（测试用）。"""
    global _classifier
    _classifier = None


__all__ = [
    "IntentClassifier",
    "SYSTEM_PROMPT",
    "VALID_INTENTS",
    "get_classifier",
    "parse_intent",
    "reset_classifier",
]
