"""Long-document evidence packets: ranked hits survive focusing, neighbors are anchored to them.

All offline: lexical embeddings, a recording model double, and, where the exact
ranking matters, a pinned vector ranking over real indexed chunks. No inference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService, OCRPage
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.ocr_service import OCREngineStatus, OCRService
from odysseus_desktop_backend.services.rag_service import MAX_EVIDENCE_SNIPPET_CHARS, RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SearchResult, SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database
from test_v020_low_text_pdf_rag import write_image_only_pdf

OPENING = "The visitor badge office opens at seven each morning."
MIDDLE = "Forklift charging happens only in bay nine overnight."
CLOSING = "Refunds above forty dollars need a supervisor signature before release."
OPENING_QUESTION = "When does the visitor badge office open?"
CLOSING_QUESTION = "Who must sign refunds above forty dollars?"
NO_OVERLAP = "zzqx qqvw"  # pinned-ranking tests: no lexical or OCR term can reorder the hits


class CapturingModels(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, model: str, messages: list[dict[str, str]], **_: Any) -> str:
        self.calls.append(messages)
        return "captured"


def build(tmp_path: Path):
    db = Database(tmp_path / "profile")
    db.set_setting("embedding_backend", "lexical")
    documents = DocumentService(db)
    rag = RAGService(documents, EmbeddingService(db), SQLiteNumPyVectorStore(db))
    models = CapturingModels(db)
    chat = ChatService(SessionService(db), SettingsService(db), models, rag=rag, documents=documents)
    return db, documents, rag, models, chat


def page_text(number: int) -> str:
    text = f"Section {number}. Routine notes about aisle {number} shelving, pallet rotation and dock scheduling."
    return {2: f"{text} {OPENING}", 15: f"{text} {MIDDLE}", 29: f"{text} {CLOSING}"}.get(number, text)


def write_pdf(path: Path, pages: int, text=page_text) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for number in range(1, pages + 1):
        y = 720
        line = ""
        for word in text(number).split():
            if len(line) + len(word) > 80:
                pdf.drawString(72, y, line)
                y, line = y - 16, ""
            line = f"{line} {word}".strip()
        pdf.drawString(72, y, line)
        pdf.showPage()
    pdf.save()
    return path


def indexed(documents: DocumentService, rag: RAGService, path: Path) -> dict[str, Any]:
    document = documents.import_document(str(path))
    rag.index_document(document["id"])
    return documents.get(document["id"])


def pin_ranking(rag: RAGService, ranking: list[tuple[str, int, float]]) -> None:
    """Vector search returns these (document, chunk position, score) hits, honoring source scope."""
    hits = []
    for document_id, position, score in ranking:
        chunk = rag.documents.chunks(document_id)[position]
        hits.append(
            SearchResult(
                chunk_id=chunk["id"],
                document_id=document_id,
                content=chunk["content"],
                score=score,
                page_start=chunk["page_start"],
                page_end=chunk["page_end"],
                metadata=chunk["metadata"],
            )
        )

    def search(_vector, *, limit=5, metadata_filter=None, **_):
        allowed = (metadata_filter or {}).get("document_id")
        return [hit for hit in hits if allowed is None or hit.document_id in allowed][:limit]

    rag.vector_store.similarity_search = search


def positions(rag: RAGService, results: list[dict[str, Any]]) -> list[int]:
    by_id = {
        chunk["id"]: index
        for document_id in {result["document_id"] for result in results}
        for index, chunk in enumerate(rag.documents.chunks(document_id))
    }
    return [by_id[result["chunk_id"]] for result in results]


def assert_packet_maps_to_chunks(rag: RAGService, context: str, results, snippets) -> None:
    chunks = {
        chunk["id"]: chunk
        for document_id in {snippet["document_id"] for snippet in snippets}
        for chunk in rag.documents.chunks(document_id)
    }
    retrieved = {result["chunk_id"] for result in results}
    for snippet in snippets:
        chunk = chunks[snippet["chunk_id"]]
        assert snippet["chunk_id"] in retrieved
        assert snippet["document_id"] == chunk["document_id"]
        assert (snippet["page_start"], snippet["page_end"]) == (chunk["page_start"], chunk["page_end"])
        assert " ".join(snippet["text"].split()).removesuffix("...") in " ".join(chunk["content"].split())
        assert len(snippet["text"]) <= MAX_EVIDENCE_SNIPPET_CHARS + 3
        assert (
            f"[{snippet['snippet_id']}] {snippet['source']}, page {snippet['page_start']}, chunk {snippet['chunk_id']}"
            in context
        )


# ------------------------------------------------------------ real retrieval, real packet


def test_late_page_evidence_reaches_the_model_input(tmp_path: Path):
    db, documents, rag, models, chat = build(tmp_path)
    document = indexed(documents, rag, write_pdf(tmp_path / "handbook.pdf", 30))
    assert len(documents.chunks(document["id"])) == 30  # one chunk per page

    result = chat.send(CLOSING_QUESTION, use_rag=True, document_ids=[document["id"]], rag_preset="standard")

    system = models.calls[0][0]["content"]
    assert CLOSING in " ".join(system.split())
    snippets = result["retrieved_snippets"]
    assert snippets[0]["page_start"] == 29
    assert all(snippet["coverage"] == "excerpt" for snippet in snippets)
    context, results, direct = rag.build_quote_context(
        CLOSING_QUESTION, document_ids=[document["id"]], complete_markdown=True
    )
    assert [snippet["chunk_id"] for snippet in direct] == [snippet["chunk_id"] for snippet in snippets]
    assert context in system
    assert 28 in positions(rag, results)  # the hit itself, not the opening pages
    assert_packet_maps_to_chunks(rag, context, results, direct)
    db.close()


def test_opening_page_evidence_still_reaches_the_model_input(tmp_path: Path):
    db, documents, rag, models, chat = build(tmp_path)
    document = indexed(documents, rag, write_pdf(tmp_path / "handbook.pdf", 30))

    result = chat.send(OPENING_QUESTION, use_rag=True, document_ids=[document["id"]], rag_preset="standard")

    assert OPENING in " ".join(models.calls[0][0]["content"].split())
    assert result["retrieved_snippets"][0]["page_start"] == 2
    db.close()


def test_explicit_scope_keeps_its_own_late_evidence(tmp_path: Path):
    db, documents, rag, models, chat = build(tmp_path)
    other_closing = "Refunds above forty dollars need two cashier stamps instead."
    first = indexed(documents, rag, write_pdf(tmp_path / "first.pdf", 30))
    second = indexed(
        documents,
        rag,
        write_pdf(tmp_path / "second.pdf", 30, lambda n: other_closing if n == 27 else page_text(n).replace(CLOSING, "")),
    )

    result = chat.send(CLOSING_QUESTION, use_rag=True, document_ids=[second["id"]], rag_preset="standard")

    system = " ".join(models.calls[0][0]["content"].split())
    assert other_closing in system
    assert CLOSING not in system
    assert {chunk["document_id"] for chunk in result["retrieved_chunks"]} == {second["id"]}
    assert {snippet["document_id"] for snippet in result["retrieved_snippets"]} == {second["id"]}
    assert result["retrieved_snippets"][0]["page_start"] == 27
    assert not any(chunk["id"] in system for chunk in documents.chunks(first["id"]))
    db.close()


def test_large_markdown_excerpts_include_late_sections(tmp_path: Path):
    db, documents, rag, models, chat = build(tmp_path)
    filler = "".join(f"## Section {n}\n\nRoutine notes about aisle {n} shelving and pallet rotation.\n\n" for n in range(1, 120))
    path = tmp_path / "handbook.md"
    path.write_text(f"# Handbook\n\n{filler}## Refunds\n\n{CLOSING}\n", encoding="utf-8")
    document = indexed(documents, rag, path)
    assert len(documents.chunks(document["id"])) > 8

    result = chat.send(CLOSING_QUESTION, use_rag=True, document_ids=[document["id"]], rag_preset="standard")

    assert CLOSING in models.calls[0][0]["content"]
    snippets = result["retrieved_snippets"]
    assert all(snippet["coverage"] == "excerpt" for snippet in snippets)  # too large for complete Markdown
    assert snippets[0]["metadata"]["file_type"] == "md"
    db.close()


def test_potato_packet_reaches_late_evidence_within_its_limit(tmp_path: Path):
    db, documents, rag, _models, chat = build(tmp_path)
    document = indexed(documents, rag, write_pdf(tmp_path / "handbook.pdf", 30))

    result = chat.send(CLOSING_QUESTION, use_rag=True, document_ids=[document["id"]], rag_preset="potato")

    assert result["retrieved_snippets"][0]["page_start"] == 29
    assert CLOSING in " ".join(result["retrieved_snippets"][0]["text"].split())
    assert len(result["retrieved_chunks"]) <= 8
    db.close()


# ------------------------------------------------------------ pinned rankings: exact assembly


@pytest.mark.parametrize(
    ("ranking", "expected"),
    [
        ([(28, 0.9), (3, 0.8), (15, 0.7)], [28, 3, 15, 29, 27, 4, 2, 16]),  # distant hits
        ([(29, 0.9)], [29, 28, 27, 26, 25, 24, 23, 22]),  # last chunk: clamped at the end
        ([(0, 0.9), (1, 0.8)], [0, 1, 2, 3, 4, 5, 6, 7]),  # opening hits
        ([(14, 0.9), (15, 0.8)], [14, 15, 13, 16, 12, 17, 11, 18]),  # adjacent hits
    ],
)
def test_focused_results_keep_ranked_hits_then_nearest_neighbors(tmp_path: Path, ranking, expected):
    db, documents, rag, _models, _chat = build(tmp_path)
    document = indexed(documents, rag, write_pdf(tmp_path / "handbook.pdf", 30))
    pin_ranking(rag, [(document["id"], position, score) for position, score in ranking])
    hits = rag.search(NO_OVERLAP, limit=4, document_ids=[document["id"]])

    context, results, snippets = rag.build_quote_context(NO_OVERLAP, document_ids=[document["id"]], complete_markdown=True)

    assert positions(rag, results) == expected
    assert results[: len(hits)] == hits  # unchanged, in rank order, with their scores
    chunks = documents.chunks(document["id"])
    anchors = {hit["chunk_id"] for hit in hits}
    for result in results[len(hits):]:
        assert result["score"] == 0.0
        assert result["neighbor_of"] in anchors
        index = positions(rag, [result])[0]
        assert result["page_start"] == chunks[index]["page_start"] == index + 1
    assert len({result["chunk_id"] for result in results}) == len(results)
    assert_packet_maps_to_chunks(rag, context, results, snippets)
    db.close()


@pytest.mark.parametrize(("limit", "pages", "expected"), [(2, 30, 8), (4, 30, 8), (10, 30, 10), (4, 5, 5)])
def test_focused_results_stay_within_the_existing_budget(tmp_path: Path, limit, pages, expected):
    db, documents, rag, _models, _chat = build(tmp_path)
    document = indexed(documents, rag, write_pdf(tmp_path / "handbook.pdf", pages))
    pin_ranking(rag, [(document["id"], position, 0.9 - 0.1 * order) for order, position in enumerate((pages - 1, 0, pages // 2))])

    _context, results, snippets = rag.build_quote_context(NO_OVERLAP, limit=limit, document_ids=[document["id"]])

    assert len(results) == expected  # max(limit, 8), or the whole short document
    assert len(snippets) <= 6
    assert rag.build_quote_context(NO_OVERLAP, limit=limit, document_ids=[document["id"]], max_snippets=0)[::2] == ("", [])
    db.close()


def test_mixed_documents_focus_drops_noise_but_keeps_ranked_hits(tmp_path: Path):
    db, documents, rag, _models, _chat = build(tmp_path)
    long = indexed(documents, rag, write_pdf(tmp_path / "long.pdf", 30))
    short = indexed(documents, rag, write_pdf(tmp_path / "short.pdf", 3))

    # Two of the top three hits share a document: focus on it, as before.
    pin_ranking(rag, [(long["id"], 28, 0.9), (short["id"], 0, 0.8), (long["id"], 3, 0.7)])
    _context, results, _snippets = rag.build_quote_context(NO_OVERLAP, document_ids=[long["id"], short["id"]])
    assert {result["document_id"] for result in results} == {long["id"]}
    assert positions(rag, results)[:2] == [28, 3]

    # No clear focus: the mixed ranked results pass through untouched.
    pin_ranking(rag, [(long["id"], 28, 0.9), (short["id"], 0, 0.85), (short["id"], 1, 0.6), (long["id"], 3, 0.55)])
    hits = rag.search(NO_OVERLAP, limit=4, document_ids=[long["id"], short["id"]])
    _context, results, _snippets = rag.build_quote_context(NO_OVERLAP, document_ids=[long["id"], short["id"]])
    assert results == hits
    assert {result["document_id"] for result in results} == {long["id"], short["id"]}
    assert not any("neighbor_of" in result for result in results)
    db.close()


class LongOcrEngine:
    name = "mock-ocr"

    def status(self):
        return OCREngineStatus(True, self.name, "mock-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, source_path: str):
        return [
            OCRPage(
                source_path=source_path,
                page_number=number,
                engine_name=self.name,
                confidence=90.0,
                text=page_text(number),
                metadata={"renderer": "mock-renderer"},
            )
            for number in range(1, 21)
        ]


def test_ocr_document_neighbors_are_anchored_and_keep_ocr_metadata(tmp_path: Path):
    db, documents, rag, _models, _chat = build(tmp_path)
    ocr = OCRService(documents, rag, engine=LongOcrEngine())
    document = documents.import_document(str(write_image_only_pdf(tmp_path / "scan.pdf")))
    ocr.ensure_document_ocr_indexed(document["id"])
    chunks = documents.chunks(document["id"])
    assert len(chunks) == 20 and all(chunk["metadata"].get("ocr") for chunk in chunks)
    pin_ranking(rag, [(document["id"], 17, 0.9)])

    context, results, snippets = rag.build_quote_context(NO_OVERLAP, document_ids=[document["id"]])

    assert [result["page_start"] for result in results] == [18, 19, 17, 20, 16, 15, 14, 13]
    assert all(result["metadata"].get("ocr") for result in results)
    assert not any(result["metadata"].get("ocr_lexical_match") for result in results)
    assert_packet_maps_to_chunks(rag, context, results, snippets)
    db.close()
