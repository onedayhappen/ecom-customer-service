"""User-filtered fact retrieval: structured slots, Chinese BM25 and cached vectors.

Embeddings are generated only during writes. Reads encode the query once and
never download a model or re-encode stored facts.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from math import isfinite, log, sqrt
from pathlib import Path
import re
from typing import TYPE_CHECKING
import unicodedata

if TYPE_CHECKING:
    from app.memory.long_term import MemoryFact

logger = logging.getLogger(__name__)
_MODELS: dict[str, object] = {}
_STOP = set("我的你您他她它们了是在有要给帮请把将和与及一个这那到吗呢啊呀说")
_STOP_WORDS = {"用户", "偏好", "喜欢", "请问", "帮我", "记得", "以前", "之前", "什么", "怎么", "现在", "一下", "推荐"}


def _normalized(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def tokenize(text: str) -> list[str]:
    """Latin terms + Chinese characters/bigrams, without NLP downloads."""
    tokens = []
    for part in re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+", _normalized(text)):
        if not re.search(r"[\u3400-\u9fff]", part):
            tokens.append(part)
            continue
        for word in _STOP_WORDS:
            part = part.replace(word, " ")
        for segment in part.split():
            tokens.extend(char for char in segment if char not in _STOP)
            tokens.extend(segment[i:i + 2] for i in range(len(segment) - 1)
                          if any(c not in _STOP for c in segment[i:i + 2]))
    return tokens


def lexical_scores(query: str, texts: list[str]) -> dict[int, float]:
    """Share the Chinese/Latin lexical ranking with task candidate retrieval."""
    return MemoryRetriever._bm25(query, texts)


def slot_hints(query: str) -> tuple[set[str], dict]:
    from app.memory.atomic import query_slots
    return query_slots(query)


def fact_text(fact) -> str:
    return json.dumps({"content": fact.content, "predicate": getattr(fact, "predicate", ""),
                       "value": getattr(fact, "value", None), "qualifiers": getattr(fact, "qualifiers", {}),
                       "polarity": getattr(fact, "polarity", "positive")},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(fact) -> str:
    return sha256(fact_text(fact).encode("utf-8")).hexdigest()


def parse_time(value) -> datetime | None:
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def time_eligible(fact, *, as_of=None, history=False) -> bool:
    """Current and historical facts follow explicit validity windows."""
    status = getattr(fact, "status", "active")
    if (status not in {"active", "superseded", "retracted", "expired", "scheduled", "historical"}
            or getattr(fact, "operation", "") == "RETRACT"
            or getattr(fact, "metadata", {}).get("reset")
            or getattr(fact, "metadata", {}).get("archived_legacy")):
        return False
    try:
        at = parse_time(as_of) if as_of is not None else datetime.now(timezone.utc)
        if at is None:
            return False
        start = parse_time(getattr(fact, "valid_from", "") or getattr(fact, "observed_at", "")
                           or (fact.created_at if as_of is not None or history else ""))
        # Transaction time is not real-world validity: an assertion recorded
        # today may announce a change next month.
        ends = [parse_time(value) for value in (getattr(fact, "valid_to", ""), fact.expires_at) if value]
    except (TypeError, ValueError):
        return False
    # A scheduled assertion may be withdrawn before it ever becomes valid.
    # It is ledger evidence, not a historical user preference.
    if start is not None and ends and min(ends) <= start:
        return False
    if history and as_of is None:
        return status != "scheduled"
    if as_of is None and status != "active":
        return False
    if as_of is not None and status not in {"active", "scheduled"} and not ends:
        return False
    return (start is None or start <= at) and (not ends or at < min(ends))


class MemoryRetriever:
    def __init__(self, model_path: str | None, enabled: bool = True, min_score: float = 0.35):
        self.enabled, self.min_score = enabled, min_score
        self.model_path = Path(model_path).resolve() if model_path else None

    def _model_key(self) -> str:
        try:
            if not self.enabled or self.model_path is None or not self.model_path.is_dir():
                return ""
            stamp = [(str(path.relative_to(self.model_path)), path.stat().st_size, path.stat().st_mtime_ns)
                     for path in sorted(self.model_path.rglob("*"))
                     if path.is_file() and path.suffix in {".json", ".safetensors", ".bin"}]
        except OSError:
            return ""
        return sha256(json.dumps([str(self.model_path), stamp]).encode()).hexdigest()

    @staticmethod
    def _embedding(fact) -> dict:
        value = getattr(fact, "metadata", {}).get("embedding", {})
        return value if isinstance(value, dict) else {}

    def _get_model(self):
        key = self._model_key()
        if not key:
            return None
        if key not in _MODELS:
            try:
                from sentence_transformers import SentenceTransformer
                _MODELS[key] = SentenceTransformer(str(self.model_path), local_files_only=True)
            except (ImportError, OSError, ValueError, TypeError, RuntimeError):
                logger.warning("Local memory embedding unavailable; lexical retrieval remains active")
                return None
        return _MODELS[key]

    @staticmethod
    def _vector(value) -> list[float] | None:
        try:
            values = [float(v) for v in value]
            norm = sqrt(sum(v * v for v in values))
            if not values or not isfinite(norm) or norm <= 0:
                return None
            return [v / norm for v in values]
        except (TypeError, ValueError, OverflowError):
            return None

    def prepare_embeddings(self, facts: list[MemoryFact]) -> list[MemoryFact]:
        """Populate JSON-serializable metadata outside the storage transaction."""
        key = self._model_key()
        if not key:
            return facts
        pending = [fact for fact in facts if
                   (self._embedding(fact).get("model") != key or
                    self._embedding(fact).get("content_hash") != content_hash(fact) or
                    self._vector(self._embedding(fact).get("vector")) is None)]
        if not pending:
            return facts
        model = self._get_model()
        if model is None:
            return facts
        try:
            vectors = model.encode([fact_text(fact) for fact in pending], normalize_embeddings=True, show_progress_bar=False)
            if len(vectors) != len(pending):
                return facts
            for fact, raw in zip(pending, vectors):
                vector = self._vector(raw)
                if vector is not None:
                    fact.metadata = dict(getattr(fact, "metadata", {}))
                    fact.metadata["embedding"] = {"model": key, "content_hash": content_hash(fact), "vector": vector}
        except (OSError, ValueError, TypeError, RuntimeError):
            logger.warning("Memory embedding preparation failed; facts remain available lexically")
        return facts

    def _vector_scores(self, query, facts):
        key = self._model_key()
        cached = []
        for i, fact in enumerate(facts):
            embedding = self._embedding(fact)
            if key and embedding.get("model") == key and embedding.get("content_hash") == content_hash(fact):
                vector = self._vector(embedding.get("vector"))
                if vector:
                    cached.append((i, vector))
        if not cached:
            return {}
        model = self._get_model()
        if model is None:
            return {}
        try:
            vector = self._vector(model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0])
            if vector is None:
                return {}
            return {i: sum(a * b for a, b in zip(vector, stored))
                    for i, stored in cached if len(stored) == len(vector)}
        except (OSError, ValueError, TypeError, IndexError, RuntimeError):
            return {}

    @staticmethod
    def _bm25(query, texts):
        terms = set(tokenize(query))
        documents = [Counter(tokenize(text)) for text in texts]
        if not terms or not documents:
            return {}
        average = sum(sum(doc.values()) for doc in documents) / len(documents) or 1
        df = {term: sum(term in doc for doc in documents) for term in terms}
        scores = {}
        for i, document in enumerate(documents):
            length, score = sum(document.values()), 0.0
            for term in terms:
                count = document.get(term, 0)
                if count:
                    inverse = log(1 + (len(documents) - df[term] + 0.5) / (df[term] + 0.5))
                    score += inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
            if score > 0:
                scores[i] = score
        return scores

    def retrieve(self, query: str, facts: list[MemoryFact], top_k: int, *, as_of=None, history=False) -> list[MemoryFact]:
        if not query.strip() or not facts or top_k <= 0:
            return []
        predicates, qualifiers = slot_hints(query)
        category = qualifiers.get("product_category")
        eligible = [fact for fact in facts if time_eligible(fact, as_of=as_of, history=history)
                    and (not category or not getattr(fact, "qualifiers", {}).get("product_category")
                         or fact.qualifiers["product_category"] == category)]
        if not eligible:
            return []
        slots = {}
        for i, fact in enumerate(eligible):
            if predicates and getattr(fact, "predicate", "") in predicates:
                slots[i] = 2 if category and getattr(fact, "qualifiers", {}).get("product_category") == category else 1
        lexical = self._bm25(query, [fact.content + " " + json.dumps(getattr(fact, "qualifiers", {}), ensure_ascii=False) for fact in eligible])
        if lexical:
            peak = max(lexical.values())
            lexical = {i: score for i, score in lexical.items() if score / peak >= self.min_score}
        vectors = {i: score for i, score in self._vector_scores(query, eligible).items() if score >= self.min_score}
        from app.config.settings import settings
        candidates = settings.memory_hybrid_candidates
        rrf_k = getattr(settings, "memory_rrf_k", 60)
        scores = {}
        for channel, weight in ((slots, 2), (lexical, 1), (vectors, 1)):
            order = sorted(channel, key=lambda i: (-channel[i], eligible[i].memory_id))[:candidates]
            for rank, index in enumerate(order, 1):
                scores[index] = scores.get(index, 0) + weight / (rrf_k + rank)
        ranked = sorted(scores, key=lambda i: (-scores[i], eligible[i].memory_id))
        selected, seen = [], set()
        for index in ranked:
            fact = eligible[index]
            identity = (fact.memory_id or content_hash(fact), getattr(fact, "version", 1))
            if identity not in seen:
                selected.append(fact)
                seen.add(identity)
            if len(selected) == top_k:
                break
        return selected
