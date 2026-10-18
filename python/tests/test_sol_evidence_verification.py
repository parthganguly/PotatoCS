"""Independent offline contract checks owned by Sol; no model inference."""
from pathlib import Path

import pytest

from odysseus_desktop_backend.services.answer_evidence import request_spans
from odysseus_desktop_backend.services.document_service import DocumentService, ExtractedPage, SOURCE_READ_PAGE_CHARS
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.rag_service import RAGService, MAX_EVIDENCE_SNIPPETS, MAX_EVIDENCE_SNIPPET_CHARS
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database


@pytest.fixture
def services(tmp_path):
    db = Database(tmp_path / "profile")
    db.set_setting("embedding_backend", "lexical")
    documents = DocumentService(db)
    rag = RAGService(documents, EmbeddingService(db), SQLiteNumPyVectorStore(db))
    yield db, documents, rag
    db.close()


def imported(tmp_path, documents, text, name="guide.md"):
    source = tmp_path / name
    source.write_text(text, encoding="utf-8", newline="\n")
    return documents.import_document(str(source))


@pytest.mark.parametrize("quote", ["'", "‘"])
def test_balanced_quoted_directives_are_not_cut_into_asks(quote):
    close = "’" if quote == "‘" else quote
    question = f"Explain {quote}what changed and why it failed{close} and list the limits."
    spans = request_spans(question)
    # A conservative fallback is allowed; a boundary inside the quotation is not.
    assert any(f"{quote}what changed and why it failed{close}" in span for span in spans), spans


def test_complete_packet_retains_boundary_newlines(tmp_path, services):
    _db, documents, rag = services
    text = "\n\n# Guide\n\n- Parent action.\n  - Child outcome.\n\n\n"
    doc = imported(tmp_path, documents, text)
    rag.index_document(doc["id"])
    context, _, snippets = rag.build_quote_context("What is the parent action?", document_ids=[doc["id"]], complete_markdown=True)
    assert snippets[0]["text"] == text
    assert f"----- begin S1 -----\n{text}\n----- end S1 -----" in context


@pytest.mark.parametrize("delta,full", [(0, True), (1, False)])
def test_full_markdown_allowance_boundary(tmp_path, services, delta, full):
    _db, documents, rag = services
    budget = MAX_EVIDENCE_SNIPPETS * MAX_EVIDENCE_SNIPPET_CHARS
    doc = imported(tmp_path, documents, "x" * (budget + delta))
    results = [{"document_id": doc["id"], "chunk_id": "retrieved", "page_start": 1, "metadata": {}, "score": 1}]
    snippets = rag.complete_markdown_snippets(results)
    assert bool(snippets) is full
    assert sum(len(s["text"]) for s in snippets) <= budget


def test_multiple_pages_follow_page_order_and_keep_chunk_identity(tmp_path, services):
    _db, documents, rag = services
    doc = imported(tmp_path, documents, "initial")
    documents.replace_pages(doc["id"], [
        ExtractedPage(page_number=2, text="second\n", extraction_method="plain_text", metadata={}),
        ExtractedPage(page_number=1, text="first\n", extraction_method="plain_text", metadata={}),
    ])
    results = [
        {"document_id": doc["id"], "chunk_id": "chunk-second", "page_start": 2, "metadata": {}, "score": 2},
        {"document_id": doc["id"], "chunk_id": "chunk-first", "page_start": 1, "metadata": {}, "score": 1},
    ]
    snippets = rag.complete_markdown_snippets(results)
    assert [s["page_start"] for s in snippets] == [1, 2]
    assert [s["text"] for s in snippets] == ["first\n", "second\n"]
    assert [s["retrieved_chunk_ids"] for s in snippets] == [["chunk-first"], ["chunk-second"]]
    assert all(s["chunk_id"] == "" for s in snippets)
    results[0]["metadata"]["ocr"] = True
    assert rag.complete_markdown_snippets(results) == []


def test_original_reader_caps_output_and_rejects_other_profile(tmp_path, services):
    _db, documents, _rag = services
    text = "a\n" * 13000
    doc = imported(tmp_path, documents, text)
    first = documents.read_imported_source(doc["id"], max_chars=10**9)
    assert first["text"] == text[:SOURCE_READ_PAGE_CHARS]
    assert first["next_offset"] == SOURCE_READ_PAGE_CHARS
    second = documents.read_imported_source(doc["id"], offset=first["next_offset"])
    assert first["text"] + second["text"] == text
    other_db = Database(tmp_path / "other-profile")
    try:
        assert DocumentService(other_db).read_imported_source(doc["id"])["state"] == "not_found"
    finally:
        other_db.close()


def test_answer_evidence_reopens_from_database_and_survives_source_deletion(tmp_path):
    # Deliberately a new Database instance, not merely another sessions.messages call.
    from test_answer_evidence import build, import_indexed
    db, documents, rag, _models, sessions, chat = build(tmp_path, "The instruction is retained [S1].")
    doc = import_indexed(documents, rag, tmp_path / "notes.md", "# Notes\n\nThe instruction is retained.\n")
    result = chat.send("What instruction is retained?", use_rag=True, document_ids=[doc["id"]])
    session_id = result["session"]["id"]
    snapshot = result["assistant_message"]["metadata"]["answer_evidence"]
    rag.delete_document(doc["id"])
    db.close()
    reopened = Database(tmp_path / "profile")
    try:
        answer = SessionService(reopened).messages(session_id)[-1]
        assert answer["metadata"]["answer_evidence"] == snapshot
        assert DocumentService(reopened).read_imported_source(doc["id"])["state"] == "deleted"
    finally:
        reopened.close()
