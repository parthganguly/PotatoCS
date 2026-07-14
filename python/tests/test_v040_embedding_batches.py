"""Issue #16 embedding batching and cancellation safe points."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from odysseus_desktop_backend.cancellation import (
    JobCancelledError,
    cancellation_scope,
    shield,
)
from odysseus_desktop_backend.services.embedding_service import (
    EMBEDDING_BATCH_SIZE,
    EmbeddingService,
    LocalHashEmbeddingProvider,
    content_hash,
)
from odysseus_desktop_backend.storage import Database


class RecordingProvider:
    backend = "semantic"
    model_name = "synthetic"
    model_key = "synthetic:v1"

    def __init__(self, *, cancel_event: threading.Event | None = None, fail_call: int = 0):
        self.calls: list[list[str]] = []
        self.cancel_event = cancel_event
        self.fail_call = fail_call

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        self.calls.append(list(texts))
        if self.fail_call and len(self.calls) == self.fail_call:
            raise RuntimeError("synthetic provider failure")
        vectors = [
            np.array([float(int(text.split("-")[-1]) + 1), 1.0], dtype=np.float32)
            for text in texts
        ]
        if self.cancel_event is not None and len(self.calls) == 1:
            self.cancel_event.set()
        return vectors


@pytest.fixture()
def db(tmp_path: Path):
    opened = Database(tmp_path / "profile")
    yield opened
    opened.close()


def texts(count: int) -> list[str]:
    return [f"batch-item-{index}" for index in range(count)]


def cache_count(db: Database, model: str) -> int:
    row = db.conn.execute(
        "SELECT COUNT(*) AS count FROM embedding_cache WHERE embedding_model = ?",
        (model,),
    ).fetchone()
    return int(row["count"])


def test_batches_are_bounded_and_results_preserve_input_order(db: Database):
    provider = RecordingProvider()
    items = texts(EMBEDDING_BATCH_SIZE * 2 + 3)
    results = EmbeddingService(db, provider=provider).embed_texts(items)

    assert [len(call) for call in provider.calls] == [16, 16, 3]
    assert max(map(len, provider.calls)) <= EMBEDDING_BATCH_SIZE
    assert [result.content_hash for result in results] == [content_hash(item) for item in items]
    assert [float(result.vector[0]) for result in results] == [float(index + 1) for index in range(len(items))]
    assert all(result.from_cache is False for result in results)


def test_cancel_before_first_batch_makes_no_provider_call(db: Database):
    event = threading.Event()
    event.set()
    provider = RecordingProvider()
    with cancellation_scope(event), pytest.raises(JobCancelledError):
        EmbeddingService(db, provider=provider).embed_texts(texts(17))
    assert provider.calls == []
    assert cache_count(db, provider.model_key) == 0


def test_cancel_between_batches_keeps_completed_batch_cached(db: Database):
    event = threading.Event()
    provider = RecordingProvider(cancel_event=event)
    with cancellation_scope(event), pytest.raises(JobCancelledError):
        EmbeddingService(db, provider=provider).embed_texts(texts(20))

    assert [len(call) for call in provider.calls] == [EMBEDDING_BATCH_SIZE]
    assert cache_count(db, provider.model_key) == EMBEDDING_BATCH_SIZE


def test_provider_failure_midway_preserves_existing_fallback_semantics(db: Database):
    provider = RecordingProvider(fail_call=2)
    fallback = LocalHashEmbeddingProvider(dimensions=8)
    items = texts(20)
    service = EmbeddingService(db, provider=provider, fallback_provider=fallback)

    results = service.embed_texts(items)

    assert [len(call) for call in provider.calls] == [16, 4]
    assert len(results) == len(items)
    assert all(result.backend == "lexical" for result in results)
    assert fallback.calls == len(items)
    assert cache_count(db, provider.model_key) == EMBEDDING_BATCH_SIZE


def test_second_call_reuses_cache_without_provider_calls(db: Database):
    provider = RecordingProvider()
    service = EmbeddingService(db, provider=provider)
    items = texts(19)

    first = service.embed_texts(items)
    provider.calls.clear()
    second = service.embed_texts(items)

    assert all(result.from_cache is False for result in first)
    assert all(result.from_cache is True for result in second)
    assert provider.calls == []
    assert [result.content_hash for result in second] == [content_hash(item) for item in items]


def test_shield_makes_between_batch_checkpoint_inert(db: Database):
    event = threading.Event()
    event.set()
    provider = RecordingProvider()
    with cancellation_scope(event):
        with shield():
            results = EmbeddingService(db, provider=provider).embed_texts(texts(18))

    assert len(results) == 18
    assert [len(call) for call in provider.calls] == [16, 2]
    assert cache_count(db, provider.model_key) == 18


def test_checkpoint_occurs_before_call_and_not_inside_write_transaction(db: Database):
    class TransactionCheckingProvider(RecordingProvider):
        def embed(self, batch: list[str]) -> list[np.ndarray]:
            assert db.conn.in_transaction is False
            return super().embed(batch)

    provider = TransactionCheckingProvider()
    EmbeddingService(db, provider=provider).embed_texts(texts(33))
    assert [len(call) for call in provider.calls] == [16, 16, 1]
