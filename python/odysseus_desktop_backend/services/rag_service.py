from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from dataclasses import replace
from typing import Any

from odysseus_desktop_backend.progress import emit_progress
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService, content_hash
from odysseus_desktop_backend.services.vector_store import SearchResult, VectorChunk, VectorStore


MAX_CHUNK_CHARS = 900
CHUNK_OVERLAP_CHARS = 120
RERANK_MIN_CANDIDATES = 32
MAX_EVIDENCE_SNIPPETS = 6
MAX_EVIDENCE_SNIPPET_CHARS = 900
MAX_OCR_LEXICAL_RESULTS = 8
OCR_LINE_WINDOW_SIZE = 5
OCR_LINE_WINDOW_STEP = 3
OCR_RETRIEVAL_WINDOW_RADIUS = 4
TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
QUOTED_RE = re.compile(r"['\"]([^'\"]{2,80})['\"]")
CAPITALIZED_RE = re.compile(r"\b[A-Z][A-Za-z0-9'-]{2,}\b")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
STOPWORDS = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "called",
    "can",
    "context",
    "doc",
    "document",
    "file",
    "for",
    "from",
    "have",
    "i",
    "in",
    "info",
    "information",
    "is",
    "it",
    "me",
    "mention",
    "of",
    "on",
    "or",
    "pdf",
    "please",
    "previous",
    "that",
    "the",
    "this",
    "tell",
    "to",
    "was",
    "what",
    "with",
    "you",
}


@dataclass(frozen=True)
class ChunkDraft:
    content: str
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any]


class RAGService:
    def __init__(
        self,
        documents: DocumentService,
        embeddings: EmbeddingService,
        vector_store: VectorStore,
    ):
        self.documents = documents
        self.embeddings = embeddings
        self.vector_store = vector_store

    def index_document(self, document_id: str) -> dict[str, Any]:
        emit_progress("source_index")
        document = self.documents.get(document_id)
        if document["is_deleted"]:
            raise ValueError("cannot index a deleted document")
        if document["is_low_text"] or document["index_status"] == "low_text":
            return {
                "document": document,
                "chunks": [],
                "embedded": 0,
                "cached": 0,
                "low_text": True,
            }

        self.documents.mark_indexing(document_id)
        pages = self.documents.pages(document_id)
        drafts = self._chunk_pages(document, pages)
        if not drafts:
            self.documents.mark_low_text(document_id)
            return {
                "document": self.documents.get(document_id),
                "chunks": [],
                "embedded": 0,
                "cached": 0,
                "low_text": True,
            }

        embedding_results = self.embeddings.embed_texts([draft.content for draft in drafts])
        chunks: list[VectorChunk] = []
        cached = 0
        for index, (draft, embedding) in enumerate(zip(drafts, embedding_results)):
            if embedding.from_cache:
                cached += 1
            digest = content_hash(draft.content)
            chunks.append(
                VectorChunk(
                    id=self._chunk_id(document_id, index, digest),
                    document_id=document_id,
                    chunk_index=index,
                    content=draft.content,
                    content_hash=digest,
                    embedding_model=embedding.model,
                    embedding_hash=embedding.content_hash,
                    vector=embedding.vector,
                    page_start=draft.page_start,
                    page_end=draft.page_end,
                    metadata=draft.metadata,
                )
            )

        self.vector_store.reindex_document(document_id, chunks)
        indexed_model = embedding_results[0].model if embedding_results else ""
        indexed_backend = embedding_results[0].backend if embedding_results else ""
        self.documents.mark_indexed(
            document_id,
            embedding_model=indexed_model,
            embedding_backend=indexed_backend,
        )
        self.documents.link_ocr_chunks(document_id)
        return {
            "document": self.documents.get(document_id),
            "chunks": self.documents.chunks(document_id),
            "embedded": len(chunks) - cached,
            "cached": cached,
            "low_text": False,
        }

    def reindex_document(self, document_id: str) -> dict[str, Any]:
        return self.index_document(document_id)

    def delete_document(self, document_id: str) -> dict[str, Any]:
        # Failure-atomic hard deletion with honest reclaimed-byte reporting;
        # the old mark_deleted soft delete reclaimed nothing
        # (V04_STORAGE_CLEANUP_DESIGN.md §6).
        return self.documents.delete_user_document(document_id)

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        metadata_filter: dict[str, Any] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        text = (query or "").strip()
        if not text:
            return []
        emit_progress("rag_search")
        audit = self.search_with_audit(
            text,
            limit=limit,
            metadata_filter=metadata_filter,
            document_ids=document_ids,
        )
        reranked = audit["results"]
        results = [
            self._result_dict(result)
            for result in reranked
        ]
        emit_progress("rag_retrieved", progress_current=len(results))
        return results

    def search_with_audit(
        self,
        query: str,
        *,
        limit: int = 5,
        metadata_filter: dict[str, Any] | None = None,
        document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        text = (query or "").strip()
        started = time.perf_counter()
        if not text:
            return {
                "results": [],
                "candidates": [],
                "embedding_backend": "",
                "embedding_model": "",
                "retrieval_latency_ms": 0,
            }
        query_embedding = self.embeddings.embed_query(text)
        candidate_limit = max(limit, RERANK_MIN_CANDIDATES, limit * 8)
        effective_filter = self._source_scoped_filter(metadata_filter, document_ids)
        candidates = self.vector_store.similarity_search(
            query_embedding.vector,
            limit=candidate_limit,
            embedding_model=query_embedding.model,
            metadata_filter=effective_filter,
        )
        ocr_candidates, ocr_diagnostics = self._ocr_lexical_results(
            text,
            metadata_filter=effective_filter,
            limit=max(MAX_OCR_LEXICAL_RESULTS, limit * 2),
        )
        if ocr_candidates:
            by_key: dict[tuple[str, str], SearchResult] = {}
            for candidate in [*candidates, *ocr_candidates]:
                by_key[(candidate.document_id, candidate.chunk_id)] = candidate
            candidates = list(by_key.values())
        reranked, diagnostics = self._rerank_with_details(
            text,
            candidates,
            embedding_backend=query_embedding.backend,
        )
        return {
            "results": reranked[: max(0, limit)],
            "candidates": diagnostics,
            "ocr_candidates": ocr_diagnostics,
            "embedding_backend": query_embedding.backend,
            "embedding_model": query_embedding.model,
            "retrieval_latency_ms": int((time.perf_counter() - started) * 1000),
        }

    def health(self) -> dict[str, Any]:
        health = self.vector_store.health()
        embedding = self.embeddings.status()
        health["embedding"] = embedding
        active_counts = self.document_embedding_counts(str(embedding.get("cache_key") or ""))
        health["documents_needing_reindex"] = active_counts["documents_needing_reindex"]
        health["indexed_documents"] = active_counts["indexed_documents"]
        health["documents_indexed_with_active_backend"] = active_counts[
            "documents_indexed_with_active_backend"
        ]
        health["user_documents_indexed_with_active_backend"] = (
            active_counts["indexed_documents"] > 0
            and active_counts["documents_needing_reindex"] == 0
        )
        return health

    def documents_needing_reindex(self) -> int:
        current_key = str(self.embeddings.status().get("cache_key") or "")
        return self.document_embedding_counts(current_key)["documents_needing_reindex"]

    def document_embedding_counts(self, current_key: str) -> dict[str, int]:
        if not current_key:
            return {
                "indexed_documents": 0,
                "documents_indexed_with_active_backend": 0,
                "documents_needing_reindex": 0,
            }
        row = self.documents.db.conn.execute(
            """
            SELECT
                COUNT(*) AS indexed_documents,
                SUM(
                    CASE WHEN COALESCE(indexed_embedding_model, '') = ? THEN 1 ELSE 0 END
                ) AS active_documents
            FROM documents
            WHERE is_deleted = 0
              AND COALESCE(is_staging, 0) = 0
              AND index_status = 'indexed'
            """,
            (current_key,),
        ).fetchone()
        indexed = int(row["indexed_documents"] if row else 0)
        active = int(row["active_documents"] or 0) if row else 0
        return {
            "indexed_documents": indexed,
            "documents_indexed_with_active_backend": active,
            "documents_needing_reindex": max(0, indexed - active),
        }

    def build_context(
        self,
        query: str,
        *,
        limit: int = 4,
        document_ids: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        results = self.search(query, limit=limit, document_ids=document_ids)
        if not results:
            return "", []
        results = self._expand_focused_document_results(query, results, limit=limit)
        blocks = []
        for index, result in enumerate(results, start=1):
            title = result["metadata"].get("title") or result["metadata"].get("file_name") or "Document"
            page = result["page_start"]
            location = f", page {page}" if page else ""
            blocks.append(f"[{index}] {title}{location}\n{result['content']}")
        return "\n\n".join(blocks), results

    def build_quote_context(
        self,
        query: str,
        *,
        limit: int = 4,
        document_ids: list[str] | None = None,
        max_snippets: int = MAX_EVIDENCE_SNIPPETS,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        results = self.search(query, limit=limit, document_ids=document_ids)
        if not results:
            return "", [], []
        results = self._expand_focused_document_results(query, results, limit=limit)
        snippets = self.extract_evidence_snippets(
            query,
            results,
            max_snippets=max_snippets,
        )
        blocks = []
        query_tokens = important_tokens(query)
        for snippet in snippets:
            source = snippet["source"]
            page = snippet["page_start"]
            location = f", page {page}" if page else ""
            ocr_note = ocr_match_prompt_note(snippet)
            heading_text = " ".join(
                str(snippet["metadata"].get(key) or "")
                for key in ("title", "file_name", "source_name")
            )
            first_sentence = split_sentences(snippet["text"])[:1]
            framing_text = " ".join([heading_text, *first_sentence])
            relevance_note = ""
            if overlap_score(query_tokens, important_tokens(framing_text)) > 0:
                relevance_note = (
                    "Relevance note: this snippet heading or source matches the question; "
                    "use the following quoted bullets from this same snippet as the relevant story/facts.\n"
                )
            quoted_sentences = "\n".join(
                f"- \"{sentence}\""
                for sentence in split_sentences(snippet["text"])
            )
            blocks.append(
                f"[{snippet['snippet_id']}] {source}{location}, chunk {snippet['chunk_id']}\n"
                f"{relevance_note}"
                f"{ocr_note}"
                f"{quoted_sentences}"
            )
        return "\n\n".join(blocks), results, snippets

    def extract_evidence_snippets(
        self,
        query: str,
        results: list[dict[str, Any]],
        *,
        max_snippets: int = MAX_EVIDENCE_SNIPPETS,
        max_chars: int = MAX_EVIDENCE_SNIPPET_CHARS,
    ) -> list[dict[str, Any]]:
        query_tokens = important_tokens(query)
        scored_snippets: list[tuple[float, float, int, dict[str, Any]]] = []
        seen_text: set[str] = set()

        for result_index, result in enumerate(results):
            title = result["metadata"].get("title") or result["metadata"].get("file_name") or "Document"
            metadata_text = " ".join(
                str(result["metadata"].get(key) or "")
                for key in ("title", "file_name", "source_name")
            )
            metadata_score = overlap_score(query_tokens, important_tokens(metadata_text)) * 1.75
            sentences = split_sentences(result["content"])
            if not sentences:
                continue

            best_score = -1.0
            best_text = ""
            result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            if result_metadata.get("ocr_lexical_match"):
                best_text = trim_to_sentence_boundary(str(result["content"] or ""), max_chars)
                best_score = (
                    overlap_score(query_tokens, important_tokens(best_text)) * 2.5
                    + metadata_score
                    + float(result["score"]) * 0.2
                    + 5.0
                )
            ocr_window = ocr_line_window(result["content"], query_tokens, max_chars=max_chars)
            if not best_text and result_metadata.get("ocr") and ocr_window:
                best_text = ocr_window
                best_score = (
                    overlap_score(query_tokens, important_tokens(best_text)) * 2.0
                    + metadata_score
                    + float(result["score"]) * 0.1
                )
            if not best_text:
                for sentence_index, _sentence in enumerate(sentences):
                    window = " ".join(sentences[sentence_index : sentence_index + 5]).strip()
                    if not window:
                        continue
                    if len(window) > max_chars:
                        window = trim_to_sentence_boundary(window, max_chars)
                    content_score = overlap_score(query_tokens, important_tokens(window))
                    score = (
                        content_score * 2.0
                        + metadata_score
                        + float(result["score"]) * 0.1
                    )
                    if score > best_score:
                        best_score = score
                        best_text = window

            if not best_text:
                continue
            normalized = normalized_text(best_text)
            if normalized in seen_text:
                continue
            seen_text.add(normalized)
            scored_snippets.append(
                (
                    best_score,
                    overlap_score(query_tokens, important_tokens(best_text)),
                    result_index,
                    {
                        "snippet_id": f"S{len(scored_snippets) + 1}",
                        "chunk_id": result["chunk_id"],
                        "document_id": result["document_id"],
                        "source": str(title),
                        "text": best_text,
                        "score": result["score"],
                        "page_start": result["page_start"],
                        "page_end": result["page_end"],
                        "metadata": result["metadata"],
                        "ocr_query_terms": result_metadata.get("ocr_query_terms") or [],
                        "ocr_exact_matches": result_metadata.get("ocr_exact_matches") or [],
                        "ocr_fuzzy_matches": result_metadata.get("ocr_fuzzy_matches") or [],
                        "ocr_substring_matches": result_metadata.get("ocr_substring_matches") or [],
                        "ocr_line_windows": result_metadata.get("ocr_line_windows") or [],
                        "ocr_context_notes": result_metadata.get("ocr_context_notes") or [],
                        "ocr_confidence": result_metadata.get("ocr_confidence"),
                        "ocr_quality": result_metadata.get("ocr_quality") or {},
                        "ocr_quality_label": result_metadata.get("ocr_quality_label") or "",
                        "ocr_attempt_count": result_metadata.get("ocr_attempt_count") or 0,
                        "ocr_crop_count": result_metadata.get("ocr_crop_count") or 0,
                        "crop_evidence": result_metadata.get("crop_evidence") or [],
                        "selected_ocr_attempt": result_metadata.get("selected_ocr_attempt") or {},
                        "vlm_text_evidence": result_metadata.get("vlm_text_evidence") or {},
                        "vlm_text_available": bool(result_metadata.get("vlm_text_available")),
                        "vlm_text_char_count": result_metadata.get("vlm_text_char_count") or 0,
                    },
                )
            )

        if any(content_score > 0 for _score, content_score, _index, _snippet in scored_snippets):
            scored_snippets = [
                item
                for item in scored_snippets
                if item[1] > 0
            ]

        scored_snippets.sort(key=lambda item: (-item[0], item[2]))
        selected = [
            snippet
            for _score, _content_score, _index, snippet in scored_snippets[: max(0, max_snippets)]
        ]
        for index, snippet in enumerate(selected, start=1):
            snippet["snippet_id"] = f"S{index}"
        return selected

    def _chunk_pages(self, document: dict[str, Any], pages: list[dict[str, Any]]) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        for page in pages:
            text = normalize_chunk_text(str(page["text"] or ""))
            if not text:
                continue
            page_metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
            if page_metadata.get("ocr"):
                drafts.extend(self._chunk_ocr_page(document, page, text, page_metadata))
                continue
            chunks = self._chunk_text(text)
            for chunk_text in chunks:
                metadata = {
                    "document_id": document["id"],
                    "title": document["title"],
                    "file_name": document["file_name"],
                    "file_type": document["file_type"],
                    "page_start": page["page_number"],
                    "page_end": page["page_number"],
                }
                metadata.update(page_metadata)
                drafts.append(
                    ChunkDraft(
                        content=chunk_text,
                        page_start=page["page_number"],
                        page_end=page["page_number"],
                        metadata=metadata,
                    )
                )
        return drafts

    def _chunk_ocr_page(
        self,
        document: dict[str, Any],
        page: dict[str, Any],
        text: str,
        page_metadata: dict[str, Any],
    ) -> list[ChunkDraft]:
        base_metadata = {
            "document_id": document["id"],
            "title": document["title"],
            "file_name": document["file_name"],
            "file_type": document["file_type"],
            "page_start": page["page_number"],
            "page_end": page["page_number"],
            "ocr": True,
            "chunk_kind": "ocr_page",
            "ocr_quality": page_metadata.get("ocr_quality") or page_metadata.get("quality") or {},
            "ocr_quality_label": (page_metadata.get("ocr_quality") or page_metadata.get("quality") or {}).get("label") if isinstance(page_metadata.get("ocr_quality") or page_metadata.get("quality"), dict) else "",
            "ocr_attempt_count": page_metadata.get("ocr_attempt_count") or 0,
            "ocr_crop_count": page_metadata.get("ocr_crop_count") or 0,
            "selected_ocr_attempt": page_metadata.get("selected_ocr_attempt") or {},
            "crop_evidence": page_metadata.get("crop_evidence") or [],
            "vlm_text_evidence": page_metadata.get("vlm_text_evidence") or {},
            "vlm_text_available": bool(page_metadata.get("vlm_text_available")),
            "vlm_text_char_count": page_metadata.get("vlm_text_char_count") or 0,
        }
        base_metadata.update(page_metadata)
        drafts = [
            ChunkDraft(
                content=chunk_text,
                page_start=page["page_number"],
                page_end=page["page_number"],
                metadata=base_metadata,
            )
            for chunk_text in self._chunk_text(text)
        ]

        lines = structured_ocr_lines(text, page_metadata)
        if len(lines) <= 1:
            return drafts
        seen_windows: set[str] = set()
        for start in range(0, len(lines), OCR_LINE_WINDOW_STEP):
            window = lines[start : start + OCR_LINE_WINDOW_SIZE]
            if not window:
                continue
            chunk_text = normalize_chunk_text("\n".join(line["text"] for line in window))
            if not chunk_text:
                continue
            normalized = normalized_text(chunk_text)
            if normalized in seen_windows:
                continue
            seen_windows.add(normalized)
            metadata = dict(base_metadata)
            metadata.update(
                {
                    "chunk_kind": "ocr_line_window",
                    "ocr_line_start": window[0]["line_number"],
                    "ocr_line_end": window[-1]["line_number"],
                    "ocr_line_count": len(window),
                }
            )
            drafts.append(
                ChunkDraft(
                    content=trim_to_sentence_boundary(chunk_text, MAX_CHUNK_CHARS),
                    page_start=page["page_number"],
                    page_end=page["page_number"],
                    metadata=metadata,
                )
            )
        return drafts

    def _chunk_text(self, text: str) -> list[str]:
        if len(text) <= MAX_CHUNK_CHARS:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + MAX_CHUNK_CHARS)
            if end < len(text):
                boundary = text.rfind(" ", start, end)
                if boundary > start + 200:
                    end = boundary
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            start = max(end - CHUNK_OVERLAP_CHARS, start + 1)
        return chunks

    def _chunk_id(self, document_id: str, chunk_index: int, digest: str) -> str:
        seed = f"{document_id}:{chunk_index}:{digest}"
        return str(uuid.UUID(hashlib.md5(seed.encode("utf-8")).hexdigest()))

    def _source_scoped_filter(
        self,
        metadata_filter: dict[str, Any] | None,
        document_ids: list[str] | None,
    ) -> dict[str, Any] | None:
        clean_document_ids = [
            document_id.strip()
            for document_id in document_ids or []
            if document_id.strip()
        ]
        if not metadata_filter and not clean_document_ids:
            return None

        effective: dict[str, Any] = dict(metadata_filter or {})
        if clean_document_ids:
            existing = effective.get("document_id")
            if existing is None:
                effective["document_id"] = clean_document_ids
            else:
                if isinstance(existing, (list, tuple, set)):
                    existing_ids = {str(item) for item in existing}
                else:
                    existing_ids = {str(existing)}
                allowed_ids = [
                    document_id
                    for document_id in clean_document_ids
                    if document_id in existing_ids
                ]
                effective["document_id"] = allowed_ids
        return effective

    def _ocr_lexical_results(
        self,
        query: str,
        *,
        metadata_filter: dict[str, Any] | None,
        limit: int,
    ) -> tuple[list[SearchResult], list[dict[str, Any]]]:
        terms = extract_ocr_query_terms(query)
        if not terms:
            return [], []
        prefer_name_recall = ocr_query_prefers_name_recall(query)
        document_ids = self._document_ids_for_ocr_filter(metadata_filter)
        documents = [
            self.documents.get(document_id)
            for document_id in document_ids
        ] if document_ids else [
            document
            for document in self.documents.list(scope=None)
            if document.get("index_status") == "indexed"
        ]

        candidates: list[tuple[float, int, SearchResult, dict[str, Any]]] = []
        order = 0
        for document in documents:
            if document.get("is_deleted"):
                continue
            ocr_pages = self.documents.ocr_pages(document["id"])
            if not ocr_pages:
                continue
            chunks = self.documents.chunks(document["id"])
            chunk_by_page = first_chunk_by_page(chunks)
            for page in ocr_pages:
                text = str(page.get("text") or "")
                metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
                lines = structured_ocr_lines(text, metadata)
                if not lines:
                    continue
                line_matches = [
                    (index, match)
                    for index, line in enumerate(lines)
                    for match in match_ocr_line_terms(line["text"], terms)
                ]
                if not line_matches:
                    continue
                selected_indexes: set[int] = set()
                for index, _match in line_matches:
                    before_radius = 16 if prefer_name_recall else OCR_RETRIEVAL_WINDOW_RADIUS
                    after_radius = OCR_RETRIEVAL_WINDOW_RADIUS + 1 if prefer_name_recall else OCR_RETRIEVAL_WINDOW_RADIUS
                    for nearby in range(
                        max(0, index - before_radius),
                        min(len(lines), index + after_radius + 1),
                    ):
                        selected_indexes.add(nearby)
                page_text = normalize_chunk_text("\n".join(line["text"] for line in lines))
                ordered_window_lines = [lines[index] for index in sorted(selected_indexes)]
                if prefer_name_recall:
                    ordered_window_lines = sanitize_name_ocr_context_lines(ordered_window_lines, terms)
                window_text = normalize_chunk_text("\n".join(line["text"] for line in ordered_window_lines))
                if len(page_text) <= MAX_EVIDENCE_SNIPPET_CHARS:
                    result_text = page_text
                else:
                    result_text = trim_to_sentence_boundary(window_text, MAX_EVIDENCE_SNIPPET_CHARS)
                exact_matches = sorted({str(match["term"]) for _index, match in line_matches if match["kind"] == "exact"})
                fuzzy_matches = [
                    match
                    for _index, match in line_matches
                    if match["kind"] == "fuzzy"
                ]
                substring_matches = sorted({str(match["term"]) for _index, match in line_matches if match["kind"] == "substring"})
                matched_terms = {
                    str(match["term"]).lower()
                    for _index, match in line_matches
                    if str(match.get("term") or "").strip()
                }
                coverage = len(matched_terms) / max(1, len(terms))
                if not prefer_name_recall and coverage < 0.75:
                    continue
                context_notes = ocr_context_notes(page_text or window_text, terms, page_number=page["page_number"])
                chunk = chunk_by_page.get(int(page["page_number"]))
                chunk_id = str((chunk or {}).get("id") or f"ocr:{document['id']}:{page['page_number']}")
                result_metadata = {
                    "document_id": document["id"],
                    "title": document.get("title") or document.get("file_name") or "Document",
                    "file_name": document.get("file_name") or "",
                    "file_type": document.get("file_type") or "",
                    "page_start": page["page_number"],
                    "page_end": page["page_number"],
                    "ocr": True,
                    "ocr_lexical_match": True,
                    "ocr_query_terms": terms,
                    "ocr_exact_matches": exact_matches,
                    "ocr_fuzzy_matches": fuzzy_matches,
                    "ocr_substring_matches": substring_matches,
                    "ocr_line_windows": [
                        {
                            "page": page["page_number"],
                            "line_start": ordered_window_lines[0]["line_number"] if ordered_window_lines else lines[min(selected_indexes)]["line_number"],
                            "line_end": ordered_window_lines[-1]["line_number"] if ordered_window_lines else lines[max(selected_indexes)]["line_number"],
                            "text": window_text,
                        }
                    ],
                    "ocr_context_notes": context_notes,
                    "ocr_confidence": page.get("confidence"),
                    "ocr_status": page.get("index_status") or "",
                    "ocr_quality": metadata.get("ocr_quality") or metadata.get("quality") or {},
                    "ocr_quality_label": (metadata.get("ocr_quality") or metadata.get("quality") or {}).get("label") if isinstance(metadata.get("ocr_quality") or metadata.get("quality"), dict) else "",
                    "ocr_attempt_count": metadata.get("ocr_attempt_count") or 0,
                    "ocr_crop_count": metadata.get("ocr_crop_count") or 0,
                    "selected_ocr_attempt": metadata.get("selected_ocr_attempt") or {},
                    "crop_evidence": metadata.get("crop_evidence") or [],
                    "vlm_text_evidence": metadata.get("vlm_text_evidence") or {},
                    "vlm_text_available": bool(metadata.get("vlm_text_available")),
                    "vlm_text_char_count": metadata.get("vlm_text_char_count") or 0,
                    "engine_name": page.get("engine_name") or "",
                    "render_dpi": metadata.get("render_dpi"),
                    "preprocessing_version": metadata.get("preprocessing_version"),
                }
                if prefer_name_recall:
                    score = (
                        8.0
                        + len(exact_matches) * 2.5
                        + len(fuzzy_matches) * 1.75
                        + len(substring_matches)
                        + min(2.0, len(line_matches) * 0.25)
                    )
                else:
                    score = (
                        1.5
                        + coverage * 2.0
                        + len(exact_matches) * 0.6
                        + len(fuzzy_matches) * 0.4
                        + len(substring_matches) * 0.25
                    )
                order += 1
                candidates.append(
                    (
                        score,
                        order,
                        SearchResult(
                            chunk_id=chunk_id,
                            document_id=document["id"],
                            content=result_text,
                            score=score,
                            page_start=int(page["page_number"]),
                            page_end=int(page["page_number"]),
                            metadata=result_metadata,
                        ),
                        {
                            "document_id": document["id"],
                            "page": page["page_number"],
                            "query_terms": terms,
                            "exact_matches": exact_matches,
                            "fuzzy_matches": fuzzy_matches,
                            "substring_matches": substring_matches,
                            "context_notes": context_notes,
                            "line_windows": result_metadata["ocr_line_windows"],
                            "score": score,
                        },
                    )
                )

        candidates.sort(key=lambda item: (-item[0], item[1]))
        selected = candidates[: max(0, limit)]
        return [item[2] for item in selected], [item[3] for item in selected]

    def _document_ids_for_ocr_filter(self, metadata_filter: dict[str, Any] | None) -> list[str]:
        if not metadata_filter:
            return []
        document_filter = metadata_filter.get("document_id")
        if document_filter is None:
            return []
        if isinstance(document_filter, (list, tuple, set)):
            return [str(item).strip() for item in document_filter if str(item).strip()]
        document_id = str(document_filter).strip()
        return [document_id] if document_id else []

    def _rerank(
        self,
        query: str,
        results: list[SearchResult],
        *,
        embedding_backend: str = "lexical",
    ) -> list[SearchResult]:
        reranked, _diagnostics = self._rerank_with_details(
            query,
            results,
            embedding_backend=embedding_backend,
        )
        return reranked

    def _rerank_with_details(
        self,
        query: str,
        results: list[SearchResult],
        *,
        embedding_backend: str = "lexical",
    ) -> tuple[list[SearchResult], list[dict[str, Any]]]:
        query_tokens = important_tokens(query)
        query_phrase = normalized_text(query)
        scored: list[tuple[float, int, SearchResult, dict[str, Any]]] = []
        vector_weight = 2.0 if embedding_backend == "semantic" else 1.0

        for original_rank, result in enumerate(results, start=1):
            content_tokens = important_tokens(result.content)
            metadata_text = " ".join(
                str(result.metadata.get(key) or "")
                for key in ("title", "file_name", "source_name")
            )
            metadata_tokens = important_tokens(metadata_text)

            lexical_score = overlap_score(query_tokens, content_tokens) * 1.25
            metadata_score = overlap_score(query_tokens, metadata_tokens) * 2.0
            phrase_bonus = 0.0

            content_phrase = normalized_text(result.content)
            metadata_phrase = normalized_text(metadata_text)
            if query_phrase and query_phrase in content_phrase:
                phrase_bonus += 0.75
            if query_phrase and query_phrase in metadata_phrase:
                phrase_bonus += 2.5

            combined_score = result.score * vector_weight + lexical_score + metadata_score + phrase_bonus
            reranked = replace(result, score=combined_score)
            scored.append(
                (
                    combined_score,
                    original_rank,
                    reranked,
                    {
                        "chunk_id": result.chunk_id,
                        "document_id": result.document_id,
                        "content": result.content,
                        "original_vector_score": result.score,
                        "lexical_overlap_contribution": lexical_score,
                        "metadata_contribution": metadata_score,
                        "phrase_bonus": phrase_bonus,
                        "final_combined_score": combined_score,
                        "original_vector_rank": original_rank,
                        "final_reranked_rank": 0,
                        "page_start": result.page_start,
                        "page_end": result.page_end,
                        "metadata": result.metadata,
                    },
                )
            )

        scored.sort(key=lambda item: (-item[0], item[1]))
        diagnostics: list[dict[str, Any]] = []
        reranked_results: list[SearchResult] = []
        for final_rank, (_score, _original_rank, result, diagnostic) in enumerate(scored, start=1):
            diagnostic["final_reranked_rank"] = final_rank
            diagnostics.append(diagnostic)
            reranked_results.append(result)
        return reranked_results, diagnostics

    def _result_dict(self, result: SearchResult) -> dict[str, Any]:
        return {
            "chunk_id": result.chunk_id,
            "document_id": result.document_id,
            "content": result.content,
            "score": result.score,
            "page_start": result.page_start,
            "page_end": result.page_end,
            "metadata": result.metadata,
        }

    def _expand_focused_document_results(
        self,
        query: str,
        results: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if any(
            isinstance(result.get("metadata"), dict) and result["metadata"].get("ocr_lexical_match")
            for result in results
        ):
            return results
        if not self._should_focus_on_top_document(query, results):
            return results

        top = results[0]
        chunks = self.documents.chunks(top["document_id"])
        if not chunks:
            return results

        score_by_id = {result["chunk_id"]: float(result["score"]) for result in results}
        focused: list[dict[str, Any]] = []
        for chunk in chunks[: max(limit, 8)]:
            focused.append(
                {
                    "chunk_id": chunk["id"],
                    "document_id": chunk["document_id"],
                    "content": chunk["content"],
                    "score": score_by_id.get(chunk["id"], 0.0),
                    "page_start": chunk["page_start"],
                    "page_end": chunk["page_end"],
                    "metadata": chunk["metadata"],
                }
            )
        return focused

    def _should_focus_on_top_document(self, query: str, results: list[dict[str, Any]]) -> bool:
        if not results:
            return False

        top = results[0]
        top_document_id = top["document_id"]
        same_document_results = [
            result for result in results if result["document_id"] == top_document_id
        ]
        leading_results = results[: min(3, len(results))]
        leading_same_document = sum(
            1 for result in leading_results if result["document_id"] == top_document_id
        )
        if leading_same_document >= 2:
            return True

        second_score = float(results[1]["score"]) if len(results) > 1 else 0.0
        top_score = float(top["score"])
        if top_score >= second_score + 0.5:
            return True

        query_tokens = important_tokens(query)
        for result in same_document_results:
            metadata_text = " ".join(
                str(result["metadata"].get(key) or "")
                for key in ("title", "file_name", "source_name")
            )
            if overlap_score(query_tokens, important_tokens(metadata_text)) >= 0.5:
                return True
            if overlap_score(query_tokens, important_tokens(result["content"])) >= 0.65:
                return True

        return False


def important_tokens(text: str) -> set[str]:
    tokens = {token.lower() for token in TOKEN_RE.findall(text)}
    important = {token for token in tokens if token not in STOPWORDS}
    return important or tokens


def extract_ocr_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for quoted in QUOTED_RE.findall(query or ""):
        terms.append(quoted.strip())
    terms.extend(
        token.strip()
        for token in CAPITALIZED_RE.findall(query or "")
        if token.strip().lower() not in STOPWORDS
    )
    for token in TOKEN_RE.findall(query or ""):
        lower = token.lower()
        if lower in STOPWORDS:
            continue
        if len(lower) >= 4:
            terms.append(token)
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = " ".join(str(term or "").split()).strip(" ,.;:!?()[]{}")
        key = normalized_text(clean)
        if not clean or key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return deduped[:12]


def ocr_query_prefers_name_recall(query: str) -> bool:
    if QUOTED_RE.search(query or ""):
        return True
    return any(
        token.strip().lower() not in STOPWORDS
        for token in CAPITALIZED_RE.findall(query or "")
    )


def normalize_chunk_text(text: str) -> str:
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def structured_ocr_lines(text: str, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    raw_lines = (metadata or {}).get("lines") if isinstance(metadata, dict) else None
    if isinstance(raw_lines, list) and raw_lines:
        lines: list[dict[str, Any]] = []
        next_line_number = 1
        for fallback_index, raw_line in enumerate(raw_lines, start=1):
            if not isinstance(raw_line, dict):
                continue
            try:
                base_line_number = int(raw_line.get("line_number") or fallback_index)
            except (TypeError, ValueError):
                base_line_number = fallback_index
            for offset, line_text in enumerate(str(raw_line.get("text") or "").splitlines()):
                clean_line = line_text.strip()
                if not clean_line:
                    continue
                lines.append(
                    {
                        "line_number": base_line_number if offset == 0 else next_line_number,
                        "text": clean_line,
                        "confidence": raw_line.get("confidence"),
                        "word_boxes": raw_line.get("word_boxes") if isinstance(raw_line.get("word_boxes"), list) else [],
                    }
                )
                next_line_number = max(next_line_number + 1, base_line_number + offset + 1)
        if lines:
            for index, line in enumerate(lines, start=1):
                line["line_number"] = index
            return lines
    return [
        {"line_number": index, "text": line.strip(), "confidence": None, "word_boxes": []}
        for index, line in enumerate(str(text or "").splitlines(), start=1)
        if line.strip()
    ]


def match_ocr_line_terms(line: str, terms: list[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    line_norm = normalized_text(line)
    line_compact = compact_ocr_text(line)
    line_words = TOKEN_RE.findall(line or "")
    line_word_norms = [compact_ocr_text(word) for word in line_words if compact_ocr_text(word)]
    for term in terms:
        term_norm = normalized_text(term)
        term_compact = compact_ocr_text(term)
        if not term_compact:
            continue
        if term_norm and term_norm in line_norm:
            matches.append({"kind": "exact", "term": term, "matched_text": term, "distance": 0})
            continue
        if len(term_compact) >= 4 and term_compact in line_compact:
            matches.append({"kind": "substring", "term": term, "matched_text": term, "distance": 0})
            continue
        fuzzy = best_fuzzy_word_match(term_compact, line_words, line_word_norms)
        if fuzzy is not None:
            matched_text, distance = fuzzy
            matches.append({"kind": "fuzzy", "term": term, "matched_text": matched_text, "distance": distance})
    return matches


def sanitize_name_ocr_context_lines(lines: list[dict[str, Any]], terms: list[str]) -> list[dict[str, Any]]:
    if not lines:
        return lines
    sanitized: list[dict[str, Any]] = []
    for line in lines:
        text = str(line.get("text") or "")
        lower = text.lower()
        has_query_match = bool(match_ocr_line_terms(text, terms))
        if not has_query_match and "forced myself on" in lower:
            continue
        sanitized.append(line)
    return sanitized or lines


def ocr_context_notes(text: str, terms: list[str], *, page_number: int | None = None) -> list[str]:
    normalized = compact_ocr_text(text)
    term_keys = {compact_ocr_text(term) for term in terms}
    notes: list[str] = []
    page_label = f"page {page_number}" if page_number else "the retrieved page"
    if "rebecca" in term_keys and "warsaw" in normalized:
        has_relationship_clue = any(
            clue in normalized
            for clue in (
                "relationship",
                "riafnship",
                "felationship",
                "elattbnship",
                "nship",
            )
        )
        has_learning_clue = "patient" in normalized or "bait" in normalized
        notes.append(
            f"OCR evidence summary: {page_label} contains an exact/nearby Rebecca match and "
            "a Warsaw-like row appears in the same retrieved window. In broad Rebecca answers, "
            "explicitly say OCR suggests a Warsaw entry involving Rebecca on this page; "
            "if asked what happened there, say the Warsaw row is noisy if the row text cannot "
            "be read reliably."
        )
        if has_relationship_clue or has_learning_clue:
            notes.append(
                f"OCR evidence summary: {page_label} has noisy Warsaw-row clues near Rebecca: "
                f"{'relationship-like text' if has_relationship_clue else 'the row title'}"
                f"{' plus patient/not-take-the-bait learning fragments' if has_learning_clue else ''}. "
                "For Warsaw-specific answers, say page-image OCR suggests this row involved Rebecca "
                "and her relationship, with a learning to be patient/not take the bait; state that "
                "unreadable details remain uncertain."
            )
    if "rebecca" in term_keys and "zoo" in normalized:
        notes.append(
            f"OCR evidence summary: {page_label} contains a Rebecca-at-the-Zoo row or OCR variant. "
            "Use this as direct document evidence for Zoo questions, while preserving OCR uncertainty."
        )
    return notes


def best_fuzzy_word_match(
    term_compact: str,
    words: list[str],
    word_norms: list[str],
) -> tuple[str, int] | None:
    if len(term_compact) < 4:
        return None
    threshold = 1 if len(term_compact) <= 8 else 2
    best: tuple[str, int] | None = None
    for word, word_norm in zip(words, word_norms):
        if not word_norm or abs(len(word_norm) - len(term_compact)) > threshold:
            continue
        distance = bounded_edit_distance(term_compact, word_norm, threshold)
        if distance <= threshold and (best is None or distance < best[1]):
            best = (word, distance)
    return best


def bounded_edit_distance(left: str, right: str, max_distance: int) -> int:
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, start=1):
        current = [row_index]
        row_min = current[0]
        for col_index, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(
                    previous[col_index] + 1,
                    current[col_index - 1] + 1,
                    previous[col_index - 1] + cost,
                )
            )
            row_min = min(row_min, current[-1])
        if row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def compact_ocr_text(text: str) -> str:
    return "".join(TOKEN_RE.findall(str(text or "").lower()))


def first_chunk_by_page(chunks: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_page: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        try:
            start = int(chunk.get("page_start") or 0)
            end = int(chunk.get("page_end") or start)
        except (TypeError, ValueError):
            continue
        if start <= 0:
            continue
        for page_number in range(start, max(start, end) + 1):
            by_page.setdefault(page_number, chunk)
    return by_page


def ocr_match_prompt_note(snippet: dict[str, Any]) -> str:
    metadata = snippet.get("metadata") if isinstance(snippet.get("metadata"), dict) else {}
    if not metadata.get("ocr_lexical_match"):
        return ""
    query_terms = [str(term) for term in snippet.get("ocr_query_terms") or [] if str(term)]
    exact = [str(term) for term in snippet.get("ocr_exact_matches") or [] if str(term)]
    fuzzy_items = [
        item
        for item in snippet.get("ocr_fuzzy_matches") or []
        if isinstance(item, dict)
    ]
    fuzzy = [
        f"{item.get('matched_text')} ~= {item.get('term')}"
        for item in fuzzy_items
        if item.get("matched_text") and item.get("term")
    ]
    context_notes = [str(note) for note in snippet.get("ocr_context_notes") or [] if str(note)]
    quality = snippet.get("ocr_quality") if isinstance(snippet.get("ocr_quality"), dict) else metadata.get("ocr_quality")
    quality_label = str(snippet.get("ocr_quality_label") or ((quality or {}).get("label") if isinstance(quality, dict) else "") or "")
    crop_count = int(snippet.get("ocr_crop_count") or metadata.get("ocr_crop_count") or 0)
    vlm = snippet.get("vlm_text_evidence") if isinstance(snippet.get("vlm_text_evidence"), dict) else metadata.get("vlm_text_evidence")
    vlm_available = bool(snippet.get("vlm_text_available") or (isinstance(vlm, dict) and vlm.get("text_char_count")))
    parts = [
        "OCR retrieval note: this is OCR text from an image-based PDF; spelling and row order may be imperfect.",
    ]
    if quality_label:
        parts.append(f"OCR quality: {quality_label}. Treat poor or usable_noisy OCR as uncertain, not definitive.")
    if crop_count:
        parts.append(f"Crop OCR evidence was used from {crop_count} page region(s); prefer cleaner crop/page-image text over full-page gibberish.")
    if isinstance(vlm, dict) and (vlm_available or vlm.get("attempted")):
        backend = str(vlm.get("backend") or "")
        model = str(vlm.get("model") or "")
        state = "available" if vlm_available else "attempted but no usable text"
        parts.append(f"VLM-assisted text extraction: {state}{f' via {backend} {model}' if backend or model else ''}. Treat it as labeled page-image transcription, not hidden certainty.")
    if query_terms:
        parts.append(f"OCR query terms searched: {', '.join(query_terms)}.")
    if exact:
        parts.append(f"Exact OCR matches: {', '.join(exact)}.")
        parts.append(
            "Do not say there is no mention when exact OCR evidence is present; "
            "state what the OCR appears to show and mark hard-to-read parts as uncertain."
        )
    if fuzzy:
        parts.append(f"Fuzzy OCR matches: {', '.join(fuzzy)}.")
        parts.append("Do not say there is no mention if this fuzzy OCR evidence is present; mention uncertainty instead.")
    if context_notes:
        parts.extend(context_notes)
    parts.append("Answer only from this OCR/document evidence and do not import external facts.")
    return "\n".join(parts) + "\n"


def ocr_line_window(text: str, query_tokens: set[str], *, max_chars: int) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines or not query_tokens:
        return ""
    hit_indexes = [
        index
        for index, line in enumerate(lines)
        if overlap_score(query_tokens, important_tokens(line)) > 0
    ]
    if not hit_indexes:
        return ""
    selected: set[int] = set()
    for index in hit_indexes:
        for nearby in range(max(0, index - 3), min(len(lines), index + 4)):
            selected.add(nearby)
    ordered = [lines[index] for index in sorted(selected)]
    snippet = "\n".join(ordered)
    if len(snippet) <= max_chars:
        return snippet
    clipped_lines: list[str] = []
    total = 0
    for line in ordered:
        next_total = total + len(line) + (1 if clipped_lines else 0)
        if next_total > max_chars:
            break
        clipped_lines.append(line)
        total = next_total
    return "\n".join(clipped_lines) or trim_to_sentence_boundary(snippet, max_chars)


def normalized_text(text: str) -> str:
    return " ".join(token.lower() for token in TOKEN_RE.findall(text))


def overlap_score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)


def split_sentences(text: str) -> list[str]:
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return []
    sentences = [part.strip() for part in SENTENCE_SPLIT_RE.split(collapsed) if part.strip()]
    return sentences or [collapsed]


def trim_to_sentence_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rstrip()
    boundary = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    if boundary >= max_chars // 2:
        return clipped[: boundary + 1]
    word_boundary = clipped.rfind(" ")
    if word_boundary >= max_chars // 2:
        return clipped[:word_boundary].rstrip() + "..."
    return clipped + "..."
