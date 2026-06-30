from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from odysseus_desktop_backend.progress import emit_progress
from odysseus_desktop_backend.services.artifact_service import (
    SUPPORTED_IMAGE_EXTENSIONS,
    ArtifactService,
)
from odysseus_desktop_backend.services.document_service import (
    SUPPORTED_EXTENSIONS,
    DocumentService,
    normalize_source_scope,
)
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.storage import utc_ms

if TYPE_CHECKING:
    from odysseus_desktop_backend.services.ocr_service import OCRService


SOURCE_FILTERS = {"all", "documents", "images", "screenshots", "indexed", "needs_attention"}


class SourceService:
    def __init__(
        self,
        documents: DocumentService,
        artifacts: ArtifactService,
        rag: RAGService,
        ocr: OCRService | None = None,
    ):
        self.documents = documents
        self.artifacts = artifacts
        self.rag = rag
        self.ocr = ocr

    def list(
        self,
        *,
        scope: str | None = "library",
        include_session: bool = False,
        include_deleted: bool = False,
        filter_kind: str = "all",
    ) -> list[dict[str, Any]]:
        clean_filter = normalize_source_filter(filter_kind)
        effective_scope = None if include_session else scope
        sources = [
            self.document_summary(document)
            for document in self.documents.list(scope=effective_scope, include_deleted=include_deleted)
        ]
        sources.extend(
            self.artifact_summary(artifact)
            for artifact in self.artifacts.list(scope=effective_scope, include_deleted=include_deleted)
        )
        filtered = [source for source in sources if self._matches_filter(source, clean_filter)]
        return sorted(filtered, key=lambda source: int(source.get("updated_at") or source.get("created_at") or 0), reverse=True)

    def import_path(
        self,
        path: str,
        *,
        scope: str = "library",
        index: bool = True,
        source_kind: str = "file",
    ) -> dict[str, Any]:
        emit_progress("source_import")
        source_path = Path(path)
        extension = source_path.suffix.lower()
        clean_scope = normalize_source_scope(scope)
        if extension in SUPPORTED_IMAGE_EXTENSIONS:
            artifact = self.artifacts.import_path(path, source_kind=source_kind, scope=clean_scope)
            return {"source": self.artifact_summary(artifact), "artifact": artifact, "index": None}
        if extension in SUPPORTED_EXTENSIONS:
            document = self.documents.import_document(path, scope=clean_scope)
            index_result = None
            if index:
                try:
                    emit_progress("source_index")
                    index_result = self._index_document_with_ocr_fallback(document["id"])
                    document = index_result["document"]
                except Exception as exc:
                    self.documents.mark_error(document["id"], f"Indexing failed: {exc}")
                    raise RuntimeError(f"Indexing failed: {exc}") from exc
            return {"source": self.document_summary(document), "document": document, "index": index_result}
        raise ValueError("Unsupported file type. Choose PDF, TXT, Markdown, PNG, JPEG, or WebP.")

    def import_many(
        self,
        paths: list[str],
        *,
        scope: str = "library",
        index: bool = True,
        source_kind: str = "file",
    ) -> dict[str, Any]:
        imported: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for path in paths:
            try:
                imported.append(self.import_path(path, scope=scope, index=index, source_kind=source_kind))
            except Exception as exc:  # noqa: BLE001 - mixed imports should keep valid files
                failed.append({"name": Path(path).name or "file", "error": str(exc)})
        return {
            "imported": imported,
            "sources": [item["source"] for item in imported],
            "failed": failed,
        }

    def promote(self, backend_kind: str, source_id: str, *, index: bool = False, derivation_id: str = "") -> dict[str, Any]:
        if backend_kind == "document":
            document = self.documents.promote(source_id)
            index_result = self._index_document_with_ocr_fallback(source_id) if index else None
            if index_result:
                document = index_result["document"]
            return {"source": self.document_summary(document), "document": document, "index": index_result}
        if backend_kind == "artifact":
            artifact = self.artifacts.promote(source_id)
            index_result = None
            if index:
                chosen = derivation_id or self._latest_text_derivation_id(source_id)
                if chosen:
                    index_result = self.artifacts.index_derivation(source_id, chosen)
            return {"source": self.artifact_summary(self.artifacts.get(source_id)), "artifact": artifact, "index": index_result}
        raise ValueError("backend_kind must be document or artifact")

    def delete(self, backend_kind: str, source_id: str) -> dict[str, Any]:
        if backend_kind == "document":
            return self.rag.delete_document(source_id)
        if backend_kind == "artifact":
            return self.artifacts.delete(source_id)
        raise ValueError("backend_kind must be document or artifact")

    def add_to_conversation(
        self,
        session_id: str,
        sources: list[tuple[str, str]],
        *,
        added_message_id: str = "",
    ) -> list[dict[str, Any]]:
        if not session_id or not sources:
            return self.conversation_context(session_id)
        now = utc_ms()
        with self.documents.db.conn:
            for backend_kind, source_id in sources:
                clean_kind = normalize_backend_kind(backend_kind)
                clean_source_id = str(source_id).strip()
                if not clean_source_id:
                    continue
                if clean_kind == "document":
                    document = self.documents.get(clean_source_id)
                    if document["is_deleted"]:
                        raise ValueError("cannot add a deleted document to conversation context")
                else:
                    artifact = self.artifacts.get(clean_source_id)
                    if artifact["is_deleted"]:
                        raise ValueError("cannot add a deleted image to conversation context")
                self.documents.db.conn.execute(
                    """
                    INSERT INTO conversation_attachments(
                        session_id, backend_kind, source_id, added_message_id, created_at, removed_at
                    )
                    VALUES (?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(session_id, backend_kind, source_id) DO UPDATE SET
                        added_message_id = excluded.added_message_id,
                        removed_at = NULL,
                        created_at = excluded.created_at
                    """,
                    (session_id, clean_kind, clean_source_id, added_message_id, now),
                )
        return self.conversation_context(session_id)

    def remove_from_conversation(self, session_id: str, backend_kind: str, source_id: str) -> list[dict[str, Any]]:
        clean_kind = normalize_backend_kind(backend_kind)
        now = utc_ms()
        self.documents.db.conn.execute(
            """
            UPDATE conversation_attachments
            SET removed_at = ?
            WHERE session_id = ? AND backend_kind = ? AND source_id = ? AND removed_at IS NULL
            """,
            (now, session_id, clean_kind, source_id),
        )
        self.documents.db.conn.commit()
        return self.conversation_context(session_id)

    def conversation_context(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            return []
        rows = self.documents.db.conn.execute(
            """
            SELECT session_id, backend_kind, source_id, added_message_id, created_at
            FROM conversation_attachments
            WHERE session_id = ? AND removed_at IS NULL
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        context: list[dict[str, Any]] = []
        for row in rows:
            backend_kind = str(row["backend_kind"])
            source_id = str(row["source_id"])
            try:
                if backend_kind == "document":
                    summary = self.document_summary(self.documents.get(source_id))
                else:
                    summary = self.artifact_summary(self.artifacts.get(source_id))
            except KeyError:
                continue
            if summary.get("document", {}).get("is_deleted") or summary.get("artifact", {}).get("is_deleted"):
                continue
            summary["conversation_status"] = "in_conversation"
            summary["conversation_added_message_id"] = str(row["added_message_id"] or "")
            summary["conversation_created_at"] = int(row["created_at"] or 0)
            context.append(summary)
        return context

    def active_document_ids(self, session_id: str) -> list[str]:
        return [
            str(source["id"])
            for source in self.conversation_context(session_id)
            if source.get("backend_kind") == "document"
        ]

    def active_artifact_ids(self, session_id: str) -> list[str]:
        return [
            str(source["id"])
            for source in self.conversation_context(session_id)
            if source.get("backend_kind") == "artifact"
        ]

    def document_summary(self, document: dict[str, Any]) -> dict[str, Any]:
        file_type = str(document.get("file_type") or "").lower()
        diagnostics = self.documents.text_diagnostics(str(document["id"]))
        return {
            "id": str(document["id"]),
            "backend_kind": "document",
            "source_type": document_source_type(file_type),
            "scope": str(document.get("scope") or "library"),
            "display_name": str(document.get("title") or document.get("file_name") or "Document"),
            "mime_type": document_mime_type(file_type),
            "size_bytes": int(document.get("size_bytes") or 0),
            "created_at": int(document.get("created_at") or 0),
            "updated_at": int(document.get("updated_at") or 0),
            "processing_status": str(document.get("status") or ""),
            "indexing_status": str(document.get("index_status") or ""),
            "page_count": int(diagnostics.get("page_count") or 0),
            "extracted_text_char_count": int(diagnostics.get("extracted_text_char_count") or 0),
            "ocr_text_char_count": int(diagnostics.get("ocr_text_char_count") or 0),
            "ocr_pages_with_text": int(diagnostics.get("ocr_pages_with_text") or 0),
            "chunk_count": int(diagnostics.get("chunk_count") or 0),
            "warning": document_warning(document),
            "error": str(document.get("error") or ""),
            "diagnostics": diagnostics,
            "document": document,
        }

    def artifact_summary(self, artifact: dict[str, Any]) -> dict[str, Any]:
        source_type = "screenshot" if str(artifact.get("source_kind") or "").startswith("screenshot") else "image"
        return {
            "id": str(artifact["id"]),
            "backend_kind": "artifact",
            "source_type": source_type,
            "scope": str(artifact.get("scope") or "library"),
            "display_name": str(artifact.get("name") or "Image"),
            "mime_type": str(artifact.get("mime_type") or "image/png"),
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "created_at": int(artifact.get("created_at") or 0),
            "updated_at": int(artifact.get("updated_at") or 0),
            "processing_status": str(artifact.get("status") or ""),
            "indexing_status": self._artifact_index_status(str(artifact["id"])),
            "thumbnail_path": str(artifact.get("thumbnail_path") or ""),
            "width": int(artifact.get("width") or 0),
            "height": int(artifact.get("height") or 0),
            "warning": "",
            "error": str(artifact.get("error") or ""),
            "artifact": artifact,
        }

    def _matches_filter(self, source: dict[str, Any], filter_kind: str) -> bool:
        if filter_kind == "all":
            return True
        if filter_kind == "documents":
            return source["backend_kind"] == "document"
        if filter_kind == "images":
            return source["source_type"] == "image"
        if filter_kind == "screenshots":
            return source["source_type"] == "screenshot"
        if filter_kind == "indexed":
            return source["indexing_status"] == "indexed"
        if filter_kind == "needs_attention":
            return bool(source.get("error") or source.get("warning")) or source["indexing_status"] in {"error", "low_text", "pending"}
        return True

    def _artifact_index_status(self, artifact_id: str) -> str:
        row = self.documents.db.conn.execute(
            "SELECT COUNT(*) AS count FROM artifact_rag_documents WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        return "indexed" if row and int(row["count"] or 0) > 0 else "not_indexed"

    def index_document(self, document_id: str) -> dict[str, Any]:
        return self._index_document_with_ocr_fallback(document_id)

    def _index_document_with_ocr_fallback(self, document_id: str) -> dict[str, Any]:
        if self.ocr is not None and self.documents.needs_ocr(document_id):
            ocr_result = self.ocr.ensure_document_ocr_indexed(document_id)
            document = ocr_result.get("document") if isinstance(ocr_result.get("document"), dict) else self.documents.get(document_id)
            index_result = ocr_result.get("index")
            if isinstance(index_result, dict):
                return index_result
            if not document.get("is_low_text") and document.get("index_status") != "low_text":
                return self.rag.index_document(document_id)
            return {
                "document": document,
                "chunks": [],
                "embedded": 0,
                "cached": 0,
                "low_text": True,
                "ocr": ocr_result,
            }
        return self.rag.index_document(document_id)

    def _latest_text_derivation_id(self, artifact_id: str) -> str:
        row = self.documents.db.conn.execute(
            """
            SELECT id
            FROM artifact_derivations
            WHERE artifact_id = ?
              AND text_content <> ''
              AND kind IN ('combined_evidence', 'ocr_text', 'vision_observations')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (artifact_id,),
        ).fetchone()
        return str(row["id"]) if row else ""


def normalize_source_filter(value: str | None) -> str:
    cleaned = (value or "all").strip()
    if cleaned not in SOURCE_FILTERS:
        raise ValueError("source filter must be all, documents, images, screenshots, indexed, or needs_attention")
    return cleaned


def normalize_backend_kind(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in {"document", "artifact"}:
        raise ValueError("backend_kind must be document or artifact")
    return cleaned


def document_source_type(file_type: str) -> str:
    if file_type == "pdf":
        return "pdf"
    if file_type == "md":
        return "markdown"
    return "text"


def document_mime_type(file_type: str) -> str:
    if file_type == "pdf":
        return "application/pdf"
    if file_type == "md":
        return "text/markdown"
    return "text/plain"


def document_warning(document: dict[str, Any]) -> str:
    ocr_status = str(document.get("ocr_status") or "")
    if ocr_status == "indexed":
        return "Image-based PDF. OCR extracted text from page images."
    if ocr_status == "running":
        return "Image-based PDF. OCR is still processing."
    if ocr_status == "no_text":
        return "Image-based PDF. OCR attempted, but no reliable text was extracted."
    if document.get("is_low_text") or document.get("index_status") == "low_text" or ocr_status == "needed":
        return "Image-based PDF. OCR is needed before answers can use rendered page text."
    if ocr_status == "unavailable":
        reason = str(document.get("ocr_error") or "").strip()
        return f"Image-based PDF. OCR could not run. {reason}".strip()
    return ""
