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
        self._endpoint = str(base_url or '')
        self._identity_files = None
        self._identity = None
        self._batch_size = batch_size
        self._local_model_path = Path(local_model_path).resolve() if local_model_path else None
        self._local_model = None
        self._local_model_identity = None
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

    @property
    def index_identity(self) -> str:
        """Bind policy releases to provider/model and actual local model files.

        A local directory basename is not a model version. File stat changes
        invalidate the digest; hashing occurs only after such a change.
        """
        import hashlib
        files = []
        if self._provider == 'local':
            files = sorted(p for p in self._local_model_path.rglob('*') if p.is_file()
                           and p.suffix in {'.json', '.bin', '.safetensors', '.model', '.txt'})
            if not files:
                raise ValueError('Local embedding model has no identifiable artifacts')
        signature = [(str(p.relative_to(self._local_model_path)), p.stat().st_size, p.stat().st_mtime_ns) for p in files]
        if self._identity is None or signature != self._identity_files:
            value = hashlib.sha256((self._provider + '\n' + self._model + '\n'
                                    + (self._endpoint if not files else '')).encode())
            for path in files:
                value.update(str(path.relative_to(self._local_model_path)).encode())
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        value.update(block)
            self._identity = 'embedding-v1:' + value.hexdigest()
            self._identity_files = signature
        return self._identity

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
            identity = self.index_identity
            if self._local_model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as error:
                    raise ImportError(
                        "本地 Embedding 需要 sentence-transformers。"
                        "请在当前环境安装 `sentence-transformers` 和兼容的 torch。"
                    ) from error
                self._local_model = SentenceTransformer(str(self._local_model_path))
                self._local_model_identity = identity
            if self._local_model_identity != identity or self.index_identity != identity:
                raise ValueError('Local embedding files changed; restart the model process and rebuild the index')

        vectors = self._local_model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        if self.index_identity != identity:
            raise ValueError('Local embedding files changed during encoding')
        return vectors.tolist()
