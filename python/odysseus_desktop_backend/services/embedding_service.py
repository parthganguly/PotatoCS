from __future__ import annotations

import hashlib
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Protocol

import numpy as np

from odysseus_desktop_backend.cancellation import JobCancelledError, check_cancelled
from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.storage import Database, utc_ms


LOCAL_HASH_MODEL = "local-hash-v1"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_DIMENSIONS = 384
# Structural safety ceiling — NOT a measured Potato Mode default; Issue
# #14/#20 own tuning.
EMBEDDING_BATCH_SIZE = 16
OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-']*", re.IGNORECASE)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "with",
}
logger = get_logger("embeddings")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmbeddingResult:
    content_hash: str
    model: str
    vector: np.ndarray
    from_cache: bool
    backend: str


@dataclass(frozen=True)
class QueryEmbedding:
    model: str
    vector: np.ndarray
    backend: str


class EmbeddingProvider(Protocol):
    model_key: str
    backend: str
    model_name: str

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        raise NotImplementedError


class LocalHashEmbeddingProvider:
    """Deterministic lexical fallback.

    This is not semantic retrieval. It exists so the local app keeps working
    without any embedding model installed.
    """

    backend = "lexical"
    model_name = LOCAL_HASH_MODEL
    model_key = LOCAL_HASH_MODEL

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS):
        self.dimensions = dimensions
        self.calls = 0

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        self.calls += len(texts)
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimensions, dtype=np.float32)
        tokens = normalized_tokens(text)
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        return vector.astype(np.float32)


class OllamaEmbeddingProvider:
    backend = "semantic"

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        *,
        endpoint: str = OLLAMA_ENDPOINT,
        timeout: float = 60,
    ):
        self.model_name = model_name
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.model_key = f"ollama:{self.model_name}"

    def installed(self) -> bool:
        if not self._tcp_reachable("127.0.0.1", 11434):
            return False
        try:
            tags = self._get_json(f"{self.endpoint}/api/tags", timeout=3)
        except Exception:
            return False
        names = {
            str(item.get("name") or "")
            for item in tags.get("models", [])
            if isinstance(item, dict)
        }
        return self.model_name in names or f"{self.model_name}:latest" in names

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        payload = {"model": self.model_name, "input": texts}
        data = self._post_json(f"{self.endpoint}/api/embed", payload, timeout=self.timeout)
        raw_embeddings = data.get("embeddings")
        if not isinstance(raw_embeddings, list):
            raw_embeddings = data.get("embedding")
            if raw_embeddings and texts:
                raw_embeddings = [raw_embeddings]
        if not isinstance(raw_embeddings, list) or len(raw_embeddings) != len(texts):
            raise RuntimeError("Ollama embedding response did not match input batch")
        vectors = []
        for raw in raw_embeddings:
            vector = np.array(raw, dtype=np.float32)
            if vector.ndim != 1 or vector.shape[0] == 0:
                raise RuntimeError("Ollama returned an invalid embedding vector")
            norm = np.linalg.norm(vector)
            if norm > 0:
                vector = vector / norm
            vectors.append(vector.astype(np.float32))
        return vectors

    def _tcp_reachable(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def _get_json(self, url: str, timeout: float) -> dict:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    def _post_json(self, url: str, payload: dict, timeout: float) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama embeddings are not reachable at {self.endpoint}") from exc
        return json.loads(raw)


class EmbeddingService:
    def __init__(
        self,
        db: Database,
        model: str | None = None,
        provider: EmbeddingProvider | None = None,
        fallback_provider: LocalHashEmbeddingProvider | None = None,
    ):
        self.db = db
        self.forced_provider = provider
        self.fallback_provider = fallback_provider or LocalHashEmbeddingProvider()
        if model is not None:
            self.db.set_setting("embedding_model", model)
        self._last_status: dict[str, object] | None = None

    def embed_texts(self, texts: Iterable[str]) -> list[EmbeddingResult]:
        items = list(texts)
        provider = self._select_provider()
        try:
            return self._embed_texts_with_provider(items, provider)
        except JobCancelledError:
            raise
        except Exception as exc:
            if provider.backend == "lexical":
                raise
            logger.warning("semantic embedding failed; falling back to lexical error=%s", exc)
            results = self._embed_texts_with_provider(items, self.fallback_provider)
            self._set_status(
                self._lexical_status(f"Lexical fallback active. Semantic embedding failed: {exc}")
            )
            return results

    def embed_query(self, query: str) -> QueryEmbedding:
        provider = self._select_provider()
        try:
            vector = provider.embed([query])[0]
            self._set_status(self._provider_status(provider, vector.shape[0]))
            return QueryEmbedding(provider.model_key, vector, provider.backend)
        except Exception as exc:
            if provider.backend == "lexical":
                raise
            logger.warning("semantic query embedding failed; falling back to lexical error=%s", exc)
            vector = self.fallback_provider.embed([query])[0]
            self._set_status(
                self._lexical_status(
                    f"Lexical fallback active. Semantic query embedding failed: {exc}",
                    vector.shape[0],
                )
            )
            return QueryEmbedding(self.fallback_provider.model_key, vector, self.fallback_provider.backend)

    def status(self) -> dict[str, object]:
        if self._last_status is not None:
            return dict(self._last_status)
        provider = self._select_provider()
        return dict(self._last_status or self._provider_status(provider, None))

    def configured_model_key(self) -> str:
        provider = self._select_provider()
        return provider.model_key

    def _embed_texts_with_provider(
        self,
        items: list[str],
        provider: EmbeddingProvider,
    ) -> list[EmbeddingResult]:
        hashes = [content_hash(text) for text in items]
        cached = self._load_cached(hashes, provider.model_key)

        missing_texts: list[str] = []
        missing_hashes: list[str] = []
        for text, digest in zip(items, hashes):
            if digest not in cached:
                missing_texts.append(text)
                missing_hashes.append(digest)

        if missing_texts:
            for offset in range(0, len(missing_texts), EMBEDDING_BATCH_SIZE):
                batch_texts = missing_texts[offset : offset + EMBEDDING_BATCH_SIZE]
                batch_hashes = missing_hashes[offset : offset + EMBEDDING_BATCH_SIZE]
                check_cancelled()
                vectors = provider.embed(batch_texts)
                now = utc_ms()
                for digest, vector in zip(batch_hashes, vectors):
                    vector = vector.astype(np.float32)
                    self.db.conn.execute(
                        """
                        INSERT INTO embedding_cache(
                            content_hash, embedding_model, vector_blob, dimensions, created_at, last_used_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(content_hash, embedding_model) DO UPDATE SET
                            vector_blob = excluded.vector_blob,
                            dimensions = excluded.dimensions,
                            last_used_at = excluded.last_used_at
                        """,
                        (
                            digest,
                            provider.model_key,
                            vector.tobytes(),
                            int(vector.shape[0]),
                            now,
                            now,
                        ),
                    )
                    cached[digest] = vector
                self.db.conn.commit()

        now = utc_ms()
        for digest in hashes:
            self.db.conn.execute(
                """
                UPDATE embedding_cache
                SET last_used_at = ?
                WHERE content_hash = ? AND embedding_model = ?
                """,
                (now, digest, provider.model_key),
            )
        self.db.conn.commit()

        if cached:
            first = next(iter(cached.values()))
            self._set_status(self._provider_status(provider, first.shape[0]))

        return [
            EmbeddingResult(
                content_hash=digest,
                model=provider.model_key,
                vector=cached[digest],
                from_cache=digest not in missing_hashes,
                backend=provider.backend,
            )
            for digest in hashes
        ]

    def _select_provider(self) -> EmbeddingProvider:
        if self.forced_provider is not None:
            self._set_status(self._provider_status(self.forced_provider, None))
            return self.forced_provider

        settings = self.db.get_settings()
        requested_backend = str(settings.get("embedding_backend") or "auto").strip().lower()
        requested_model = str(settings.get("embedding_model") or DEFAULT_EMBEDDING_MODEL).strip()
        if requested_backend in {"local", "lexical"} or requested_model == LOCAL_HASH_MODEL:
            self._set_status(self._lexical_status("Lexical fallback selected in settings."))
            return self.fallback_provider

        semantic = OllamaEmbeddingProvider(requested_model or DEFAULT_EMBEDDING_MODEL)
        if requested_backend in {"auto", "ollama", "semantic"} and semantic.installed():
            self._set_status(self._provider_status(semantic, None))
            return semantic

        message = (
            f"Lexical fallback active. Install an Ollama embedding model such as "
            f"{requested_model or DEFAULT_EMBEDDING_MODEL} for semantic retrieval."
        )
        self._set_status(self._lexical_status(message))
        return self.fallback_provider

    def _load_cached(self, hashes: list[str], model_key: str) -> dict[str, np.ndarray]:
        if not hashes:
            return {}
        placeholders = ",".join("?" for _ in hashes)
        rows = self.db.conn.execute(
            f"""
            SELECT content_hash, vector_blob, dimensions
            FROM embedding_cache
            WHERE embedding_model = ? AND content_hash IN ({placeholders})
            """,
            [model_key, *hashes],
        ).fetchall()
        cached: dict[str, np.ndarray] = {}
        for row in rows:
            vector = np.frombuffer(row["vector_blob"], dtype=np.float32).copy()
            if vector.shape[0] == row["dimensions"]:
                cached[row["content_hash"]] = vector
        return cached

    def _provider_status(self, provider: EmbeddingProvider, dimensions: int | None) -> dict[str, object]:
        if provider.backend == "semantic":
            message = f"Semantic retrieval active: {provider.model_name}"
            model = provider.model_name
        else:
            message = "Lexical fallback active."
            model = LOCAL_HASH_MODEL
        return {
            "backend": provider.backend,
            "provider": "ollama" if provider.backend == "semantic" else "local-hash",
            "model": model,
            "cache_key": provider.model_key,
            "semantic": provider.backend == "semantic",
            "dimensions": dimensions,
            "message": message,
        }

    def _lexical_status(self, message: str, dimensions: int | None = DEFAULT_DIMENSIONS) -> dict[str, object]:
        return {
            "backend": "lexical",
            "provider": "local-hash",
            "model": LOCAL_HASH_MODEL,
            "cache_key": LOCAL_HASH_MODEL,
            "semantic": False,
            "dimensions": dimensions,
            "message": message,
        }

    def _set_status(self, status: dict[str, object]) -> None:
        self._last_status = dict(status)


def normalized_tokens(text: str) -> list[str]:
    raw_tokens = [token.lower().strip("_-'") for token in TOKEN_RE.findall(text or "")]
    tokens = [token for token in raw_tokens if token and token not in STOPWORDS]
    return tokens or [token for token in raw_tokens if token]
