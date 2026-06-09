from __future__ import annotations

from pathlib import Path

import numpy as np

from odysseus_desktop_backend.services.document_service import (
    DocumentService,
    ExtractedPage,
)
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.embedding_service import (
    EmbeddingService,
    LocalHashEmbeddingProvider,
)
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import (
    SQLiteNumPyVectorStore,
    SearchResult,
    VectorStore,
)
from odysseus_desktop_backend.storage import Database


def build_rag(profile_dir: Path, provider: LocalHashEmbeddingProvider | None = None):
    db = Database(profile_dir)
    documents = DocumentService(db)
    embeddings = EmbeddingService(db, provider=provider)
    vector_store = SQLiteNumPyVectorStore(db)
    rag = RAGService(documents, embeddings, vector_store)
    return db, documents, embeddings, vector_store, rag


def test_import_index_search_delete_and_restart_persistence(tmp_path: Path):
    source = tmp_path / "mars.txt"
    source.write_text(
        "Mars rover missions collect rock samples and study ancient river deltas.\n\n"
        "Kitchen recipes use flour, salt, and yeast for bread.",
        encoding="utf-8",
    )
    provider = LocalHashEmbeddingProvider()
    db, documents, _embeddings, vector_store, rag = build_rag(tmp_path / "profile", provider)

    document = documents.import_document(str(source))
    indexed = rag.index_document(document["id"])

    assert indexed["document"]["index_status"] == "indexed"
    assert indexed["embedded"] == len(indexed["chunks"])
    assert provider.calls == len(indexed["chunks"])
    assert vector_store.health()["chunks"] == len(indexed["chunks"])

    results = rag.search("ancient mars rover samples", limit=2)
    assert results
    assert results[0]["document_id"] == document["id"]
    assert "Mars rover" in results[0]["content"]

    db.close()
    reopened_db, reopened_documents, _reopened_embeddings, _store, reopened_rag = build_rag(
        tmp_path / "profile"
    )
    reopened_results = reopened_rag.search("river deltas on mars", limit=1)
    assert reopened_results[0]["document_id"] == document["id"]

    deleted = reopened_rag.delete_document(document["id"])
    assert deleted["deleted"] is True
    assert reopened_documents.list() == []
    assert reopened_rag.search("mars rover", limit=3) == []
    reopened_db.close()


def test_embedding_cache_skips_unchanged_chunks_on_reindex(tmp_path: Path):
    source = tmp_path / "notes.md"
    source.write_text(
        "# Notes\n\n"
        "Local AI workspace notes mention sqlite vector storage and cached embeddings.",
        encoding="utf-8",
    )
    provider = LocalHashEmbeddingProvider()
    db, documents, _embeddings, _vector_store, rag = build_rag(tmp_path / "profile", provider)

    document = documents.import_document(str(source))
    first = rag.index_document(document["id"])
    calls_after_first_index = provider.calls

    second = rag.reindex_document(document["id"])

    assert first["chunks"]
    assert second["cached"] == len(second["chunks"])
    assert second["embedded"] == 0
    assert provider.calls == calls_after_first_index
    db.close()


def test_metadata_filtering_and_health(tmp_path: Path):
    txt = tmp_path / "alpha.txt"
    md = tmp_path / "beta.md"
    txt.write_text("Alpha document about blue lakes and mountain weather.", encoding="utf-8")
    md.write_text("Beta markdown about red deserts and dry wind.", encoding="utf-8")
    db, documents, _embeddings, vector_store, rag = build_rag(tmp_path / "profile")

    txt_doc = documents.import_document(str(txt))
    md_doc = documents.import_document(str(md))
    rag.index_document(txt_doc["id"])
    rag.index_document(md_doc["id"])

    txt_results = rag.search("blue lakes", metadata_filter={"file_type": "txt"})
    md_results = rag.search("blue lakes", metadata_filter={"file_type": "md"})

    assert txt_results[0]["document_id"] == txt_doc["id"]
    assert all(result["metadata"]["file_type"] == "md" for result in md_results)
    health = vector_store.health()
    assert health["ok"] is True
    assert health["version"] == "sqlite-numpy-v1"
    assert health["documents"] == 2
    db.close()


def test_low_text_pdf_is_marked_for_milestone3_without_ocr(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% low text placeholder")
    db, documents, _embeddings, vector_store, rag = build_rag(tmp_path / "profile")

    monkeypatch.setattr(
        DocumentService,
        "_extract_pdf_pages",
        lambda _self, _path: [
            ExtractedPage(
                page_number=1,
                text="",
                extraction_method="pdf_text",
                metadata={"file_type": "pdf"},
            )
        ],
    )

    document = documents.import_document(str(pdf))
    indexed = rag.index_document(document["id"])

    assert document["is_low_text"] is True
    assert document["index_status"] == "low_text"
    assert indexed["low_text"] is True
    assert vector_store.health()["chunks"] == 0
    db.close()


def test_extractable_pdf_imports_and_indexes(tmp_path: Path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    pdf = tmp_path / "mission.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 50 250 Td "
        b"(Mars rover sample cache and relay orbiter telemetry for mission planning "
        b"with enough extractable text to pass the Milestone 2 low text detector) Tj ET"
    )
    page[NameObject("/Contents")] = stream
    with pdf.open("wb") as handle:
        writer.write(handle)

    db, documents, _embeddings, _vector_store, rag = build_rag(tmp_path / "profile")

    document = documents.import_document(str(pdf))
    indexed = rag.index_document(document["id"])
    results = rag.search("relay orbiter telemetry", limit=1)

    assert indexed["document"]["index_status"] == "indexed"
    assert indexed["document"]["file_type"] == "pdf"
    assert results[0]["document_id"] == document["id"]
    assert results[0]["page_start"] == 1
    db.close()


class SpyVectorStore(VectorStore):
    def __init__(self):
        self.query_vectors: list[np.ndarray] = []
        self.metadata_filters: list[dict | None] = []

    def upsert_chunks(self, _chunks):
        return []

    def delete_by_document(self, _document_id):
        return 0

    def similarity_search(self, query_vector, *, limit=5, metadata_filter=None):
        self.query_vectors.append(query_vector)
        self.metadata_filters.append(metadata_filter)
        return [
            SearchResult(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="stub context",
                score=0.5,
                page_start=1,
                page_end=1,
                metadata={"file_type": "txt"},
            )
        ][:limit]

    def reindex_document(self, _document_id, _chunks):
        return []

    def health(self):
        return {"ok": True, "version": "spy"}


class FixedResultVectorStore(VectorStore):
    def __init__(self, results: list[SearchResult]):
        self.results = results
        self.requested_limits: list[int] = []
        self.metadata_filters: list[dict | None] = []

    def upsert_chunks(self, _chunks):
        return []

    def delete_by_document(self, _document_id):
        return 0

    def similarity_search(self, _query_vector, *, limit=5, metadata_filter=None):
        self.requested_limits.append(limit)
        self.metadata_filters.append(metadata_filter)
        filtered = [
            result
            for result in self.results
            if metadata_matches(result.metadata, metadata_filter)
        ]
        return filtered[:limit]

    def reindex_document(self, _document_id, _chunks):
        return []

    def health(self):
        return {"ok": True, "version": "fixed"}


def test_rag_search_depends_on_vector_store_interface(tmp_path: Path):
    db = Database(tmp_path / "profile")
    documents = DocumentService(db)
    embeddings = EmbeddingService(db)
    spy = SpyVectorStore()
    rag = RAGService(documents, embeddings, spy)

    results = rag.search("anything")

    assert results[0]["content"] == "stub context"
    assert len(spy.query_vectors) == 1
    db.close()


def test_rag_reranks_distinctive_content_match_above_vector_noise(tmp_path: Path):
    db = Database(tmp_path / "profile")
    documents = DocumentService(db)
    embeddings = EmbeddingService(db)
    noisy_results = [
        SearchResult(
            chunk_id="water-1",
            document_id="doc-water",
            content="Public drinking water systems discuss sampling protocols and reporting requirements.",
            score=0.95,
            page_start=5,
            page_end=5,
            metadata={"title": "PublicWaterMassMailing", "file_name": "PublicWaterMassMailing.pdf"},
        ),
        SearchResult(
            chunk_id="frame-10-1",
            document_id="doc-frame-10",
            content=(
                "Tribute to my Grandfather. When you are young, you are always in stasis. "
                "This story explains nostalgia and family memory."
            ),
            score=0.05,
            page_start=1,
            page_end=1,
            metadata={"title": "Frame 10", "file_name": "Frame 10.pdf"},
        ),
    ]
    store = FixedResultVectorStore(noisy_results)
    rag = RAGService(documents, embeddings, store)

    results = rag.search("the document is about a tribute to Grandfather", limit=1)

    assert results[0]["document_id"] == "doc-frame-10"
    assert store.requested_limits[0] > 1
    db.close()


def test_rag_reranks_title_and_file_name_match(tmp_path: Path):
    db = Database(tmp_path / "profile")
    documents = DocumentService(db)
    embeddings = EmbeddingService(db)
    noisy_results = [
        SearchResult(
            chunk_id="water-1",
            document_id="doc-water",
            content="Sample collection instructions for public drinking water compliance.",
            score=0.8,
            page_start=1,
            page_end=1,
            metadata={"title": "PublicWaterMassMailing", "file_name": "PublicWaterMassMailing.pdf"},
        ),
        SearchResult(
            chunk_id="frame-10-1",
            document_id="doc-frame-10",
            content="A personal tribute essay about memory, age, and a grandfather.",
            score=0.0,
            page_start=1,
            page_end=1,
            metadata={"title": "Frame 10", "file_name": "Frame 10.pdf"},
        ),
    ]
    store = FixedResultVectorStore(noisy_results)
    rag = RAGService(documents, embeddings, store)

    results = rag.search("frame 10.pdf", limit=1)

    assert results[0]["document_id"] == "doc-frame-10"
    db.close()


def test_rag_context_expands_clear_top_document_and_drops_noise(tmp_path: Path):
    frame = tmp_path / "Frame 10.txt"
    frame.write_text(
        "Tribute to my Grandfather. When you are young, life feels endless. "
        "A soldier was fighting with his comrades to take control of the hill. "
        "He got stuck in a hole and later questioned the morality of taking a shot. "
        + ("This filler keeps the first chunk long enough to split. " * 20)
        + "Meaning of the Story to Us. We are all puppets dancing on strings laid upon us by our ancestors. "
        "The way they lived will determine the way we approach our lives. "
        "All of us are born in a hole and die in a hole. In the middle, we forget where we come from but I never forgot. Grandpa.",
        encoding="utf-8",
    )
    water = tmp_path / "PublicWaterMassMailing.txt"
    water.write_text(
        "Public drinking water systems must follow sample collection and reporting requirements.",
        encoding="utf-8",
    )
    db, documents, _embeddings, _vector_store, rag = build_rag(tmp_path / "profile")

    frame_doc = documents.import_document(str(frame))
    water_doc = documents.import_document(str(water))
    rag.index_document(frame_doc["id"])
    rag.index_document(water_doc["id"])
    context, chunks = rag.build_context("Tell me about the grandfather?", limit=4)

    assert chunks
    assert {chunk["document_id"] for chunk in chunks} == {frame_doc["id"]}
    assert "Meaning of the Story to Us" in context
    assert "Public drinking water" not in context
    db.close()


class CapturingModelService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls = []

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        self.calls.append({"model": model, "messages": messages})
        return "RAG answer"


def test_rag_chat_is_explicit_and_injects_retrieved_chunks(tmp_path: Path):
    source = tmp_path / "mission.txt"
    source.write_text(
        "Mars mission planning requires rover batteries, sample caches, and relay orbiters.",
        encoding="utf-8",
    )
    db, documents, _embeddings, _vector_store, rag = build_rag(tmp_path / "profile")
    settings = SettingsService(db)
    sessions = SessionService(db)
    models = CapturingModelService(db)
    chat = ChatService(sessions, settings, models, rag=rag)

    document = documents.import_document(str(source))
    rag.index_document(document["id"])
    result = chat.send("What does mars mission planning require?", use_rag=True)

    assert result["retrieved_chunks"]
    assert result["retrieved_snippets"]
    assert models.calls[0]["messages"][0]["role"] == "system"
    assert "Retrieved evidence snippets" in models.calls[0]["messages"][0]["content"]
    assert "Use only the evidence snippets below for factual claims" in models.calls[0]["messages"][0]["content"]
    assert "rover batteries" in models.calls[0]["messages"][0]["content"]
    db.close()


def test_rag_chat_preserves_grandfather_chronology_with_source_scope(tmp_path: Path):
    frame = tmp_path / "Frame 10.txt"
    frame.write_text(
        "Tribute to my Grandfather. A soldier was fighting with his comrades to take control of a hill. "
        "He got stuck in a hole and wrestled with whether to take a shot. "
        "He eventually came out of the hole, enjoyed tea with comrades, and then told the story "
        "for the next 60-70 years. "
        "The broader section reflects on nostalgia, inherited memory, and how a war anecdote becomes "
        "part of a family interpretation.",
        encoding="utf-8",
    )
    water = tmp_path / "PublicWaterMassMailing.txt"
    water.write_text(
        "PublicWaterMassMailing explains public drinking water sample collection, testing, "
        "and reporting requirements.",
        encoding="utf-8",
    )
    db, documents, _embeddings, _vector_store, rag = build_rag(tmp_path / "profile")
    settings = SettingsService(db)
    sessions = SessionService(db)
    models = GrandfatherGroundingModelService(db)
    chat = ChatService(sessions, settings, models, rag=rag)

    frame_doc = documents.import_document(str(frame))
    water_doc = documents.import_document(str(water))
    rag.index_document(frame_doc["id"])
    rag.index_document(water_doc["id"])
    result = chat.send(
        "Tell me about the grandfather?",
        use_rag=True,
        document_ids=[frame_doc["id"]],
    )

    answer = result["assistant_message"]["content"]
    system_prompt = models.calls[0]["messages"][0]["content"]
    assert "Preserve chronology exactly" in system_prompt
    assert "Never merge facts across unrelated documents" in system_prompt
    assert "came out of the hole" in system_prompt
    assert "told the story for the next 60-70 years" in system_prompt
    assert "PublicWaterMassMailing" not in system_prompt
    assert all(chunk["document_id"] == frame_doc["id"] for chunk in result["retrieved_chunks"])
    assert "stayed in the hole for 60-70 years" not in answer
    assert "eventually came out" in answer
    assert "tea with comrades" in answer
    assert "next 60-70 years" in answer
    assert "war anecdote" in answer
    assert "broader interpretation" in answer
    db.close()


def test_source_scoped_rag_context_does_not_cross_contaminate_documents(tmp_path: Path):
    frame = tmp_path / "Frame 10.txt"
    frame.write_text(
        "Tribute to my Grandfather. He eventually came out of the hole and had tea with comrades.",
        encoding="utf-8",
    )
    water = tmp_path / "PublicWaterMassMailing.txt"
    water.write_text(
        "PublicWaterMassMailing contains water testing and sample collection requirements.",
        encoding="utf-8",
    )
    db, documents, _embeddings, _vector_store, rag = build_rag(tmp_path / "profile")

    frame_doc = documents.import_document(str(frame))
    water_doc = documents.import_document(str(water))
    rag.index_document(frame_doc["id"])
    rag.index_document(water_doc["id"])

    frame_context, frame_chunks = rag.build_context(
        "Tell me about the grandfather?",
        document_ids=[frame_doc["id"]],
    )
    water_context, water_chunks = rag.build_context(
        "What does the water PDF say about testing?",
        document_ids=[water_doc["id"]],
    )

    assert frame_chunks
    assert water_chunks
    assert {chunk["document_id"] for chunk in frame_chunks} == {frame_doc["id"]}
    assert {chunk["document_id"] for chunk in water_chunks} == {water_doc["id"]}
    assert "PublicWaterMassMailing" not in frame_context
    assert "water testing" not in frame_context
    assert "Grandfather" not in water_context
    assert "tea with comrades" not in water_context
    db.close()


class GrandfatherGroundingModelService(CapturingModelService):
    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        self.calls.append({"model": model, "messages": messages})
        system_prompt = messages[0]["content"]
        assert "the retrieved context does not say" in system_prompt
        assert "Distinguish direct claims from inference" in system_prompt
        return (
            "The retrieved context directly says the grandfather was in a war anecdote, "
            "eventually came out of the hole, and had tea with comrades. It also directly "
            "says he told the story for the next 60-70 years. A broader interpretation "
            "suggested by the text is that the anecdote became part of family memory."
        )


def metadata_matches(metadata: dict, metadata_filter: dict | None) -> bool:
    if not metadata_filter:
        return True
    for key, expected in metadata_filter.items():
        actual = metadata.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True
