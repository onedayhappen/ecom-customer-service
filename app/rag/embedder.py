"""向量化封装：支持 OpenAI API 与本地 Sentence Transformers 模型。"""

from pathlib import Path
from typing import Iterable
import threading

from openai import OpenAI
from app.config.settings import settings


class Embedder:
    """统一 Embedding 接口，按配置选择远程或本地实现。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "text-embedding-3-small",
        batch_size: int = 64,
        provider: str = "openai",
        local_model_path: str | None = None,
    ):
        self._provider = provider.lower()
        if self._provider not in {"openai", "local"}:
            raise ValueError("未知 Embedding provider，可选: openai / local")
        self._client = (
            OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=settings.llm_timeout_seconds,
                max_retries=0,
            )
            if self._provider == "openai" else None
        )
        self._model = model
        self._batch_size = batch_size
        self._local_model_path = Path(local_model_path).resolve() if local_model_path else None
        self._local_model = None
        self._load_lock = threading.Lock()
        if self._provider == "local" and (
            self._local_model_path is None or not self._local_model_path.exists()
        ):
            raise FileNotFoundError(f"本地 Embedding 模型不存在: {local_model_path}")

    @property
    def model(self) -> str:
        if self._provider == "local":
            return f"local:{self._local_model_path.name}"
        return self._model

    def encode(self, texts: Iterable[str]) -> list[list[float]]:
        """批量编码，自动按 batch_size 分批请求。"""
        texts = list(texts)
        if not texts:
            return []
        if self._provider == "local":
            from app.runtime.budgets import embedding_budget
            with embedding_budget(self.model, texts, remote=False):
                return self._encode_local(texts)

        out: list[list[float]] = []
        # 批处理兼顾接口吞吐和单次请求大小，输出顺序与输入文本严格一致。
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            from app.runtime.budgets import embedding_budget
            with embedding_budget(self.model, batch):
                resp = self._client.embeddings.create(model=self._model, input=batch)
            out.extend(item.embedding for item in resp.data)
        return out

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]

    def _encode_local(self, texts: list[str]) -> list[list[float]]:
        with self._load_lock:
            if self._local_model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as error:
                    raise ImportError(
                        "本地 Embedding 需要 sentence-transformers。"
                        "请在当前环境安装 `sentence-transformers` 和兼容的 torch。"
                    ) from error
                self._local_model = SentenceTransformer(str(self._local_model_path))

        vectors = self._local_model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.tolist()
