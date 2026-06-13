from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from dataclasses import replace
from typing import Any

from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService, content_hash
from odysseus_desktop_backend.services.vector_store import SearchResult, VectorChunk, VectorStore


MAX_CHUNK_CHARS = 900
CHUNK_OVERLAP_CHARS = 120
RERANK_MIN_CANDIDATES = 32
MAX_EVIDENCE_SNIPPETS = 6
MAX_EVIDENCE_SNIPPET_CHARS = 520
TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
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
        self.documents.get(document_id)
        deleted_chunks = self.vector_store.delete_by_document(document_id)
        document = self.documents.mark_deleted(document_id)
        return {**document, "deleted_chunks": deleted_chunks}

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
        audit = self.search_with_audit(
            text,
            limit=limit,
            metadata_filter=metadata_filter,
            document_ids=document_ids,
        )
        reranked = audit["results"]
        return [
            self._result_dict(result)
            for result in reranked
        ]

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
        reranked, diagnostics = self._rerank_with_details(
            text,
            candidates,
            embedding_backend=query_embedding.backend,
        )
        return {
            "results": reranked[: max(0, limit)],
            "candidates": diagnostics,
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
            text = " ".join((page["text"] or "").split())
            if not text:
                continue
            chunks = self._chunk_text(text)
            for chunk_text in chunks:
                drafts.append(
                    ChunkDraft(
                        content=chunk_text,
                        page_start=page["page_number"],
                        page_end=page["page_number"],
                        metadata={
                            "document_id": document["id"],
                            "title": document["title"],
                            "file_name": document["file_name"],
                            "file_type": document["file_type"],
                            "page_start": page["page_number"],
                            "page_end": page["page_number"],
                        },
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
