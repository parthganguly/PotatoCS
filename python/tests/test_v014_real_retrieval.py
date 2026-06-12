from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import (
    EmbeddingService,
    LOCAL_HASH_MODEL,
    LocalHashEmbeddingProvider,
    OllamaEmbeddingProvider,
)
from odysseus_desktop_backend.services.eval_service import EvalService, evaluate_case
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database
from rpc_server import SidecarApp


def test_ollama_embedding_provider_uses_batch_embed_api():
    provider = OllamaEmbeddingProvider("nomic-embed-text")
    captured: dict[str, Any] = {}

    def fake_post(url: str, payload: dict, timeout: float) -> dict:
        captured.update({"url": url, "payload": payload, "timeout": timeout})
        return {"embeddings": [[3.0, 0.0], [0.0, 4.0]]}

    provider._post_json = fake_post  # type: ignore[method-assign]

    vectors = provider.embed(["alpha", "beta"])

    assert captured["url"].endswith("/api/embed")
    assert captured["payload"] == {"model": "nomic-embed-text", "input": ["alpha", "beta"]}
    assert len(vectors) == 2
    assert np.allclose(vectors[0], np.array([1.0, 0.0], dtype=np.float32))
    assert np.allclose(vectors[1], np.array([0.0, 1.0], dtype=np.float32))


def test_embedding_service_falls_back_to_local_hash_when_semantic_provider_fails(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        service = EmbeddingService(
            db,
            provider=FailingSemanticProvider(),
            fallback_provider=LocalHashEmbeddingProvider(dimensions=16),
        )

        [result] = service.embed_texts(["grandfather came out of the hole"])

        assert result.backend == "lexical"
        assert result.model == LOCAL_HASH_MODEL
        assert service.status()["semantic"] is False
        assert "Semantic embedding failed" in str(service.status()["message"])
    finally:
        db.close()


def test_embedding_model_change_reports_documents_needing_reindex(tmp_path: Path):
    source = tmp_path / "story.txt"
    source.write_text("Grandfather came out of the hole and had tea with comrades.", encoding="utf-8")
    db = Database(tmp_path / "profile")
    try:
        documents = DocumentService(db)
        vector_store = SQLiteNumPyVectorStore(db)
        first_rag = RAGService(
            documents,
            EmbeddingService(db, provider=KeywordSemanticProvider("nomic-embed-text")),
            vector_store,
        )
        document = documents.import_document(str(source))
        first_rag.index_document(document["id"])

        second_rag = RAGService(
            documents,
            EmbeddingService(db, provider=KeywordSemanticProvider("mxbai-embed-large")),
            vector_store,
        )

        assert second_rag.documents_needing_reindex() == 1
    finally:
        db.close()


def test_vector_search_skips_old_vector_dimensions_safely(tmp_path: Path):
    source = tmp_path / "short.txt"
    source.write_text("A tiny document about tea.", encoding="utf-8")
    db = Database(tmp_path / "profile")
    try:
        documents = DocumentService(db)
        vector_store = SQLiteNumPyVectorStore(db)
        provider = FixedVectorProvider("tiny-2d", [1.0, 0.0])
        rag = RAGService(documents, EmbeddingService(db, provider=provider), vector_store)
        document = documents.import_document(str(source))
        rag.index_document(document["id"])

        results = vector_store.similarity_search(
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            embedding_model=provider.model_key,
        )

        assert results == []
    finally:
        db.close()


def test_semantic_retrieval_ranks_grandfather_above_lexical_decoy_unscoped(tmp_path: Path):
    grandfather = tmp_path / "hill_story.md"
    grandfather.write_text(
        "The grandfather got stuck in a hole during a hill fight. "
        "He eventually came out of the hole, had tea with his comrades, "
        "and told the story for the next 60-70 years.",
        encoding="utf-8",
    )
    decoy = tmp_path / "trench_decoy.md"
    decoy.write_text(
        "An ancestor left the trench open during a garden excavation with survey markers.",
        encoding="utf-8",
    )
    db = Database(tmp_path / "profile")
    try:
        documents = DocumentService(db)
        rag = RAGService(
            documents,
            EmbeddingService(db, provider=KeywordSemanticProvider("nomic-embed-text")),
            SQLiteNumPyVectorStore(db),
        )
        grandfather_doc = documents.import_document(str(grandfather))
        decoy_doc = documents.import_document(str(decoy))
        rag.index_document(grandfather_doc["id"])
        rag.index_document(decoy_doc["id"])

        results = rag.search("What happened after the ancestor left the trench?", limit=2)

        assert results
        assert results[0]["document_id"] == grandfather_doc["id"]
        assert results[0]["document_id"] != decoy_doc["id"]
    finally:
        db.close()


def test_unscoped_retrieval_chooses_water_doc_without_grandfather_contamination(tmp_path: Path):
    grandfather = tmp_path / "frame_10.md"
    grandfather.write_text(
        "The grandfather came out of a hole and had tea with comrades.",
        encoding="utf-8",
    )
    water = tmp_path / "water.md"
    water.write_text(
        "Public drinking water systems must follow sample collection and testing requirements.",
        encoding="utf-8",
    )
    db = Database(tmp_path / "profile")
    try:
        documents = DocumentService(db)
        rag = RAGService(
            documents,
            EmbeddingService(db, provider=KeywordSemanticProvider("nomic-embed-text")),
            SQLiteNumPyVectorStore(db),
        )
        grandfather_doc = documents.import_document(str(grandfather))
        water_doc = documents.import_document(str(water))
        rag.index_document(grandfather_doc["id"])
        rag.index_document(water_doc["id"])

        results = rag.search("What water testing requirements are described?", limit=2)

        assert results
        assert results[0]["document_id"] == water_doc["id"]
        assert results[0]["document_id"] != grandfather_doc["id"]
    finally:
        db.close()


def test_paraphrase_tolerant_eval_grader_passes_correct_chronology():
    case = {
        "expected_facts": [
            {"label": "came out", "any": ["eventually came out of the hole"]},
            {"label": "tea", "any": ["had tea with comrades"]},
            {"label": "story duration", "any": ["told the story for 60-70 years"]},
        ],
        "forbidden_claims": [
            {"label": "wrong duration", "any": ["stayed in the hole for 60-70 years"]},
        ],
    }
    result = {
        "assistant_message": {
            "content": (
                "He later came out of that hole, drank tea with comrades, "
                "and repeated the story for the next 60 to 70 years."
            )
        },
        "retrieved_chunks": [{"document_id": "doc-frame", "chunk_id": "chunk-1"}],
    }

    outcome = evaluate_case(case, result, "doc-frame")

    assert outcome["passed"] is True
    assert outcome["expected_passed"] is True
    assert outcome["forbidden_passed"] is True


def test_eval_grader_fails_verbatim_answer_from_wrong_document():
    case = {
        "expected_facts": [
            {"label": "latency", "any": ["average latency"]},
        ],
        "forbidden_claims": [],
    }
    result = {
        "assistant_message": {"content": "The benchmark report records average latency."},
        "retrieved_chunks": [{"document_id": "wrong-doc", "chunk_id": "wrong-chunk"}],
    }

    outcome = evaluate_case(case, result, "required-doc")

    assert outcome["expected_passed"] is True
    assert outcome["source_passed"] is False
    assert outcome["passed"] is False


def test_temperature_parity_between_runtime_chat_and_eval(tmp_path: Path):
    runtime_db = Database(tmp_path / "runtime")
    try:
        runtime_models = CapturingOptionsModelService(runtime_db, "runtime answer")
        runtime_chat = ChatService(
            SessionService(runtime_db),
            SettingsService(runtime_db),
            runtime_models,
        )
        runtime_chat.send("hello", model="fake:latest", temperature=0.33)
        assert runtime_models.options_seen[0]["temperature"] == 0.33
    finally:
        runtime_db.close()

    cases_dir = write_single_eval_case(tmp_path)
    eval_db = Database(tmp_path / "eval-profile")
    captured_options: list[dict[str, Any]] = []
    try:
        service = EvalService(
            eval_db,
            cases_dir=cases_dir,
            model_service_factory=lambda temp_db, temperature: CapturingOptionsModelService(
                temp_db,
                "The benchmark report tracks pass and fail counts.",
                captured_options,
            ),
        )
        run = service.run(model="fake:latest", temperature=0.42)

        assert captured_options[0]["temperature"] == 0.42
        assert run["temperature"] == 0.42
        assert run["cases"][0]["temperature"] == 0.42
    finally:
        eval_db.close()


def test_diagnostics_reports_active_embedding_backend_honestly(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        app.models.detect_ollama = lambda: {
            "name": "ollama",
            "installed": True,
            "reachable": True,
            "endpoint": "http://127.0.0.1:11434",
            "version": "test",
            "models": ["llama3.2:latest"],
            "model_details": [],
            "error": "",
            "updated_at": 1,
        }
        app.ocr.status = lambda: {
            "available": False,
            "engine_name": "tesseract",
            "renderer": "",
            "message": "OCR unavailable",
            "dependencies": {},
        }
        app.embeddings = EmbeddingService(app.db, provider=KeywordSemanticProvider("nomic-embed-text"))
        app.rag = RAGService(app.documents, app.embeddings, app.vector_store)

        diagnostics = app.dispatch("diagnostics.get", {})

        embedding = diagnostics["rag"]["embedding"]
        assert embedding["backend"] == "semantic"
        assert embedding["model"] == "nomic-embed-text"
        assert embedding["semantic"] is True
        assert "Semantic retrieval active" in embedding["message"]
    finally:
        app.close()


def write_single_eval_case(tmp_path: Path) -> Path:
    fixture_dir = tmp_path / "fixtures" / "documents"
    cases_dir = tmp_path / "rag_cases"
    fixture_dir.mkdir(parents=True)
    cases_dir.mkdir()
    document_path = fixture_dir / "benchmark_doc.md"
    document_path.write_text(
        "The benchmark report tracks pass and fail counts.",
        encoding="utf-8",
    )
    case = {
        "id": "temperature_parity",
        "documents": [{"id": "benchmark_doc", "path": "../fixtures/documents/benchmark_doc.md"}],
        "question": "What does the benchmark report track?",
        "required_source_document": "benchmark_doc",
        "expected_facts": [{"label": "counts", "any": ["pass and fail counts"]}],
        "forbidden_claims": [],
    }
    (cases_dir / "temperature_parity.json").write_text(json.dumps(case), encoding="utf-8")
    return cases_dir


class FailingSemanticProvider:
    backend = "semantic"
    model_name = "nomic-embed-text"
    model_key = "ollama:nomic-embed-text"

    def embed(self, _texts: list[str]) -> list[np.ndarray]:
        raise RuntimeError("mock semantic failure")


class FixedVectorProvider:
    backend = "semantic"

    def __init__(self, model_name: str, vector: list[float]):
        self.model_name = model_name
        self.model_key = f"ollama:{model_name}"
        self.vector = np.array(vector, dtype=np.float32)

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        return [self.vector for _text in texts]


class KeywordSemanticProvider:
    backend = "semantic"

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model_key = f"ollama:{model_name}"

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> np.ndarray:
        lower = text.lower()
        if "water" in lower or "sample collection" in lower or "testing requirements" in lower:
            return unit([0.0, 1.0, 0.0])
        if "garden excavation" in lower or "survey markers" in lower:
            return unit([0.0, 0.0, 1.0])
        if (
            "grandfather" in lower
            or "comrades" in lower
            or "came out of the hole" in lower
            or "ancestor left the trench" in lower
            or "after the ancestor" in lower
        ):
            return unit([1.0, 0.0, 0.0])
        return unit([0.1, 0.1, 0.1])


class CapturingOptionsModelService(ModelService):
    def __init__(
        self,
        db: Database,
        reply: str,
        options_seen: list[dict[str, Any]] | None = None,
    ):
        super().__init__(db)
        self.reply = reply
        self.options_seen = options_seen if options_seen is not None else []

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
    ) -> str:
        self.options_seen.append(dict(options or {}))
        return self.reply


def unit(values: list[float]) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0 else vector / norm
