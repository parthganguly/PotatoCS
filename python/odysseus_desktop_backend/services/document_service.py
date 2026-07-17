from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.pathsafety import is_link
from odysseus_desktop_backend.storage import Database, utc_ms


SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf"}
LOW_TEXT_MIN_CHARS = 80
PAGE_OCR_TEXT_MIN_CHARS = 20
SOURCE_SCOPES = {"library", "session"}
logger = get_logger("documents")


@dataclass(frozen=True)
class ExtractedPage:
    page_number: int
    text: str
    extraction_method: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class OCRPage:
    source_path: str
    page_number: int
    engine_name: str
    confidence: float | None
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentService:
    def __init__(self, db: Database):
        self.db = db
        self.documents_dir = self.db.profile_dir / "files" / "documents"
        self.documents_dir.mkdir(parents=True, exist_ok=True)

    def import_document(
        self,
        path: str,
        *,
        scope: str = "library",
        title: str | None = None,
        staging: bool = False,
    ) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        logger.info("document import requested path=%s", source)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        clean_scope = normalize_source_scope(scope)
        extension = source.suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError("Unsupported file type. Choose a .txt, .md, or extractable .pdf file.")

        document_id = str(uuid.uuid4())
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        stored_path = self.documents_dir / f"{document_id}{extension}"
        shutil.copyfile(source, stored_path)

        now = utc_ms()
        display_title = " ".join((title or source.stem or source.name).replace("\x00", "").split()).strip()[:128]
        display_title = display_title or source.name
        file_type = extension.removeprefix(".")
        self.db.conn.execute(
            """
            INSERT INTO documents(
                id, title, source_path, stored_path, file_name, file_type, content_hash,
                size_bytes, status, index_status, is_deleted, is_low_text, error,
                created_at, updated_at, indexed_at, scope, is_staging
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'imported', 'pending', 0, 0, '', ?, ?, NULL, ?, ?)
            """,
            (
                document_id,
                display_title,
                str(source),
                str(stored_path),
                source.name,
                file_type,
                digest,
                len(content),
                now,
                now,
                clean_scope,
                1 if staging else 0,
            ),
        )
        self.db.conn.commit()

        try:
            pages = self.extract_pages(stored_path)
            self.replace_pages(document_id, pages)
            total_text = sum(len(page.text.strip()) for page in pages)
            if extension == ".pdf" and total_text < LOW_TEXT_MIN_CHARS:
                self.mark_low_text(document_id)
                logger.info(
                    "document import low_text document_id=%s file_name=%s pages=%s text_chars=%s",
                    document_id,
                    source.name,
                    len(pages),
                    total_text,
                )
            else:
                logger.info(
                    "document import complete document_id=%s file_name=%s pages=%s text_chars=%s",
                    document_id,
                    source.name,
                    len(pages),
                    total_text,
                )
            return self.get(document_id)
        except Exception as exc:  # noqa: BLE001 - store import failure for UI/reporting
            self.mark_error(document_id, str(exc))
            logger.warning("document import failed document_id=%s path=%s error=%s", document_id, source, exc)
            raise

    def extract_pages(self, path: str | Path) -> list[ExtractedPage]:
        file_path = Path(path)
        extension = file_path.suffix.lower()
        if extension in {".txt", ".md"}:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            return [
                ExtractedPage(
                    page_number=1,
                    text=text,
                    extraction_method="plain_text",
                    metadata={"file_type": extension.removeprefix(".")},
                )
            ]
        if extension == ".pdf":
            return self._extract_pdf_pages(file_path)
        raise ValueError("unsupported document type")

    def replace_pages(self, document_id: str, pages: list[ExtractedPage]) -> None:
        now = utc_ms()
        self.db.conn.execute("DELETE FROM document_pages WHERE document_id = ?", (document_id,))
        for page in pages:
            text = page.text or ""
            self.db.conn.execute(
                """
                INSERT INTO document_pages(
                    id, document_id, page_number, text, text_hash, extraction_method,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    document_id,
                    page.page_number,
                    text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    page.extraction_method,
                    json.dumps(page.metadata),
                    now,
                ),
            )
        self.db.conn.execute(
            """
            UPDATE documents
            SET updated_at = ?, error = ''
            WHERE id = ?
            """,
            (now, document_id),
        )
        self.db.conn.commit()

    def replace_pages_from_ocr(self, document_id: str, pages: list[OCRPage]) -> None:
        existing_by_page = {
            int(page["page_number"]): page
            for page in self.pages(document_id)
        }
        seen_page_numbers: set[int] = set()
        extracted: list[ExtractedPage] = []
        for page in pages:
            seen_page_numbers.add(page.page_number)
            existing = existing_by_page.get(page.page_number)
            existing_text = str((existing or {}).get("text") or "")
            if len(existing_text.strip()) >= PAGE_OCR_TEXT_MIN_CHARS:
                metadata = dict((existing or {}).get("metadata") or {})
                metadata.update(
                    {
                        "file_type": "pdf",
                        "ocr_attempted": True,
                        "ocr_text_available": bool(page.text.strip()),
                        "ocr_engine_name": page.engine_name,
                        "ocr_confidence": page.confidence,
                    }
                )
                extracted.append(
                    ExtractedPage(
                        page_number=page.page_number,
                        text=existing_text,
                        extraction_method=str((existing or {}).get("extraction_method") or "pdf_text"),
                        metadata=metadata,
                    )
                )
                continue
            extracted.append(
                ExtractedPage(
                    page_number=page.page_number,
                    text=page.text,
                    extraction_method=f"ocr:{page.engine_name}",
                    metadata={
                        "file_type": "pdf",
                        "ocr": True,
                        "engine_name": page.engine_name,
                        "confidence": page.confidence,
                        **page.metadata,
                    },
                )
            )
        for page_number, page in existing_by_page.items():
            if page_number in seen_page_numbers:
                continue
            extracted.append(
                ExtractedPage(
                    page_number=page_number,
                    text=str(page.get("text") or ""),
                    extraction_method=str(page.get("extraction_method") or "pdf_text"),
                    metadata=dict(page.get("metadata") or {}),
                )
            )
        extracted.sort(key=lambda page: page.page_number)
        self.replace_pages(document_id, extracted)
        self.replace_ocr_pages(document_id, pages)

    def replace_ocr_pages(self, document_id: str, pages: list[OCRPage], index_status: str = "pending") -> None:
        now = utc_ms()
        self.db.conn.execute("DELETE FROM ocr_pages WHERE document_id = ?", (document_id,))
        for page in pages:
            text = page.text or ""
            self.db.conn.execute(
                """
                INSERT INTO ocr_pages(
                    id, document_id, source_path, page_number, engine_name, confidence,
                    text, text_hash, chunk_ids_json, index_status, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    document_id,
                    page.source_path,
                    page.page_number,
                    page.engine_name,
                    page.confidence,
                    text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    index_status,
                    json.dumps(page.metadata),
                    now,
                    now,
                ),
            )
        self.db.conn.commit()

    def list(self, *, scope: str | None = "library", include_deleted: bool = False) -> list[dict[str, Any]]:
        # Staged (uncommitted) job imports are never listed: a staging row may
        # legally reach index_status='indexed' before commit, so visibility
        # must key off is_staging, not status (V04_ESSENTIAL_SEMANTICS.md §B).
        filters = ["COALESCE(is_internal, 0) = 0", "COALESCE(is_staging, 0) = 0"]
        values: list[Any] = []
        if not include_deleted:
            filters.append("is_deleted = 0")
        if scope is not None:
            filters.append("COALESCE(scope, 'library') = ?")
            values.append(normalize_source_scope(scope))
        where = " AND ".join(filters)
        rows = self.db.conn.execute(
            f"""
            SELECT *
            FROM documents
            WHERE {where}
            ORDER BY updated_at DESC
            """,
            values,
        ).fetchall()
        return [self._document_dict(row) for row in rows]

    def get(self, document_id: str) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT * FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"document not found: {document_id}")
        return self._document_dict(row)

    def pages(self, document_id: str) -> list[dict[str, Any]]:
        self.get(document_id)
        rows = self.db.conn.execute(
            """
            SELECT id, document_id, page_number, text, text_hash, extraction_method,
                   metadata_json, created_at
            FROM document_pages
            WHERE document_id = ?
            ORDER BY page_number ASC
            """,
            (document_id,),
        ).fetchall()
        pages: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            pages.append(item)
        return pages

    def chunks(self, document_id: str) -> list[dict[str, Any]]:
        self.get(document_id)
        rows = self.db.conn.execute(
            """
            SELECT id, document_id, chunk_index, content, content_hash, page_start,
                   page_end, metadata_json, embedding_model, embedding_hash,
                   is_deleted, created_at, updated_at
            FROM rag_chunks
            WHERE document_id = ? AND is_deleted = 0
            ORDER BY chunk_index ASC
            """,
            (document_id,),
        ).fetchall()
        chunks: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            chunks.append(item)
        return chunks

    def ocr_pages(self, document_id: str) -> list[dict[str, Any]]:
        self.get(document_id)
        rows = self.db.conn.execute(
            """
            SELECT id, document_id, source_path, page_number, engine_name, confidence,
                   text, text_hash, chunk_ids_json, index_status, metadata_json, created_at, updated_at
            FROM ocr_pages
            WHERE document_id = ?
            ORDER BY page_number ASC
            """,
            (document_id,),
        ).fetchall()
        pages: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["chunk_ids"] = json.loads(item.pop("chunk_ids_json") or "[]")
            item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
            pages.append(item)
        return pages

    def mark_indexing(self, document_id: str) -> None:
        self._update_status(document_id, index_status="indexing", status="imported", error="")

    def mark_indexed(
        self,
        document_id: str,
        *,
        embedding_model: str = "",
        embedding_backend: str = "",
    ) -> None:
        now = utc_ms()
        self.db.conn.execute(
            """
            UPDATE documents
            SET index_status = 'indexed', status = 'indexed', error = '',
                indexed_embedding_model = ?, indexed_embedding_backend = ?,
                updated_at = ?, indexed_at = ?
            WHERE id = ?
            """,
            (embedding_model, embedding_backend, now, now, document_id),
        )
        self.db.conn.commit()

    def mark_low_text(self, document_id: str) -> None:
        self._update_status(
            document_id,
            status="low_text",
            index_status="low_text",
            is_low_text=1,
            ocr_status="needed",
            error="Image-based or low-text PDF detected. OCR is needed before RAG can use rendered page text.",
        )

    def mark_error(self, document_id: str, error: str) -> None:
        self._update_status(document_id, status="error", index_status="error", error=error)

    def mark_ocr_ready(self, document_id: str, engine_name: str) -> None:
        self._update_status(
            document_id,
            status="ocr_ready",
            index_status="pending",
            is_low_text=0,
            ocr_status="indexing",
            ocr_engine=engine_name,
            ocr_error="",
            error="",
        )

    def mark_ocr_running(self, document_id: str, engine_name: str) -> None:
        self._update_status(
            document_id,
            ocr_status="running",
            ocr_engine=engine_name,
            ocr_error="",
            error="",
        )

    def mark_ocr_unavailable(self, document_id: str, reason: str) -> None:
        self._update_status(
            document_id,
            ocr_status="unavailable",
            ocr_error=reason,
            error=reason,
        )

    def mark_ocr_no_text(self, document_id: str, reason: str) -> None:
        self._update_status(
            document_id,
            status="ocr_no_text",
            index_status="low_text",
            is_low_text=1,
            ocr_status="no_text",
            ocr_error=reason,
            error=reason,
        )

    def mark_ocr_indexed(self, document_id: str, engine_name: str) -> None:
        self._update_status(
            document_id,
            status="ocr_indexed",
            index_status="indexed",
            is_low_text=0,
            ocr_status="indexed",
            ocr_engine=engine_name,
            ocr_error="",
            error="",
        )

    def link_ocr_chunks(self, document_id: str) -> None:
        chunks = self.chunks(document_id)
        now = utc_ms()
        for page in self.ocr_pages(document_id):
            page_number = page["page_number"]
            chunk_ids = [
                chunk["id"]
                for chunk in chunks
                if (chunk["page_start"] or 0) <= page_number <= (chunk["page_end"] or page_number)
            ]
            self.db.conn.execute(
                """
                UPDATE ocr_pages
                SET chunk_ids_json = ?, index_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(chunk_ids),
                    "indexed" if chunk_ids else "pending",
                    now,
                    page["id"],
                ),
            )
        self.db.conn.commit()

    def commit_staging(self, document_id: str) -> dict[str, Any]:
        """Final visibility boundary for a staged job import.

        Idempotent: committing an already-committed document is a no-op
        update. Raises KeyError if the document does not exist.
        """
        self.get(document_id)
        self.db.conn.execute(
            "UPDATE documents SET is_staging = 0, updated_at = ? WHERE id = ?",
            (utc_ms(), document_id),
        )
        self.db.conn.commit()
        logger.info("document staging committed document_id=%s", document_id)
        return self.get(document_id)

    def purge_document(self, document_id: str) -> dict[str, Any]:
        """Hard-delete a document row, its derived rows, and its owned file copy.

        Used for staged-import rollback and startup repair. Derived rows
        (document_pages, ocr_pages, rag_chunks) are removed by ON DELETE
        CASCADE. Idempotent: purging a missing document reports purged=False.
        The user's original external file (source_path) is never touched.
        """
        row = self.db.conn.execute(
            "SELECT stored_path FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            return {
                "purged": False,
                "file_removed": False,
                "file_missing": False,
                "bytes_reclaimed": 0,
                "document_id": document_id,
            }
        file_removed, bytes_reclaimed, file_missing = self._remove_owned_file(
            str(row["stored_path"] or "")
        )
        self.db.conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        self.db.conn.commit()
        logger.info(
            "document purged document_id=%s file_removed=%s file_missing=%s",
            document_id,
            file_removed,
            file_missing,
        )
        return {
            "purged": True,
            "file_removed": file_removed,
            "file_missing": file_missing,
            "bytes_reclaimed": bytes_reclaimed,
            "document_id": document_id,
        }

    def restore_status_fields(self, document_id: str, fields: dict[str, Any]) -> None:
        """Restore previously captured status fields (OCR job rollback)."""
        self._update_status(document_id, **fields)

    def repair_startup_state(self) -> dict[str, int]:
        """Remove staged rows/files left by a dead process; reset stuck OCR.

        Runs at sidecar startup before any job can be submitted, under the
        app's single-instance assumption (same as campaign recovery). Logs
        fixed labels and counts only.
        """
        staged = self.db.conn.execute(
            "SELECT id FROM documents WHERE COALESCE(is_staging, 0) = 1"
        ).fetchall()
        purged = 0
        for row in staged:
            try:
                if self.purge_document(str(row["id"])).get("purged"):
                    purged += 1
            except Exception as exc:  # noqa: BLE001 - repair must not block startup
                logger.error(
                    "startup repair purge failed error_type=%s", type(exc).__name__
                )
        cursor = self.db.conn.execute(
            "UPDATE documents SET ocr_status = 'needed', updated_at = ? WHERE ocr_status = 'running'",
            (utc_ms(),),
        )
        self.db.conn.commit()
        ocr_reset = max(0, cursor.rowcount)
        if purged or ocr_reset:
            logger.warning(
                "startup repair purged_staging=%s ocr_reset=%s", purged, ocr_reset
            )
        return {"purged_staging": purged, "ocr_reset": ocr_reset}

    def _remove_owned_file(self, stored_path: str) -> tuple[bool, int, bool]:
        """Delete an app-owned file copy with fail-closed path validation.

        Returns (file_removed, bytes_reclaimed, file_missing). Refuses
        symlinks and any path that does not resolve to a direct child of the
        profile documents directory — junction/traversal escapes resolve
        elsewhere and are rejected by the parent check.
        """
        if not stored_path:
            return False, 0, True
        candidate = Path(stored_path)
        try:
            documents_root = self.documents_dir.resolve(strict=True)
        except OSError:
            logger.warning("owned file removal refused reason=documents_dir_unresolvable")
            return False, 0, False
        try:
            try:
                candidate.lstat()
            except FileNotFoundError:
                return False, 0, True
            link = is_link(candidate)
            if link is None:
                logger.warning("owned file removal refused reason=os_error")
                return False, 0, False
            if link:
                logger.warning("owned file removal refused reason=symlink")
                return False, 0, False
            if not candidate.exists():
                return False, 0, True
            resolved = candidate.resolve(strict=True)
            if resolved.parent != documents_root or not resolved.is_file():
                logger.warning("owned file removal refused reason=outside_profile")
                return False, 0, False
            size = int(resolved.stat().st_size)
            resolved.unlink()
            return True, size, False
        except OSError:
            logger.warning("owned file removal failed reason=os_error")
            return False, 0, False

    def delete_user_document(self, document_id: str) -> dict[str, Any]:
        """User-facing Source deletion (V04_STORAGE_CLEANUP_DESIGN.md §6).

        Hard-deletes derived rows and the app-owned file copy. The documents
        row is hard-deleted unless chat history references it
        (message_documents ON DELETE RESTRICT), in which case it becomes a
        tombstone with all derived data purged. Idempotent: repeating the
        call returns the same shape with zeroed reclaim numbers. The user's
        original external file (source_path) is never touched.
        """
        row = self.db.conn.execute(
            "SELECT id, stored_path, is_deleted FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            return {
                "deleted": True,
                "already_deleted": True,
                "document_id": document_id,
                "tombstoned": False,
                "deleted_chunks": 0,
                "deleted_pages": 0,
                "deleted_ocr_pages": 0,
                "file_removed": False,
                "file_missing": True,
                "bytes_reclaimed": 0,
            }
        already_deleted = bool(row["is_deleted"])
        purge = self._purge_user_document_data(document_id, str(row["stored_path"] or ""))
        return {
            "deleted": True,
            "already_deleted": already_deleted,
            "document_id": document_id,
            **purge,
        }

    def purge_deleted_document(self, document_id: str) -> dict[str, Any]:
        """Purge derived rows and the owned file of a soft-deleted document.

        Used by storage cleanup for legacy (pre-v0.4) soft deletes. The row
        itself is removed only when chat history holds no reference.
        """
        row = self.db.conn.execute(
            "SELECT id, stored_path FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            return {
                "deleted_rows": 0,
                "row_deleted": False,
                "file_removed": False,
                "file_missing": True,
                "bytes_reclaimed": 0,
            }
        purge = self._purge_user_document_data(document_id, str(row["stored_path"] or ""))
        return {
            "deleted_rows": purge["deleted_chunks"]
            + purge["deleted_pages"]
            + purge["deleted_ocr_pages"]
            + (0 if purge["tombstoned"] else 1),
            "row_deleted": not purge["tombstoned"],
            "file_removed": purge["file_removed"],
            "file_missing": purge["file_missing"],
            "bytes_reclaimed": purge["bytes_reclaimed"],
        }

    def _purge_user_document_data(self, document_id: str, stored_path: str) -> dict[str, Any]:
        """Failure-atomic purge: one DB transaction, then validated file removal.

        A DB failure rolls the transaction back and surfaces a fixed error
        code — never a false "deleted" state. File removal runs only after
        the commit; a locked or unsafe file is reported honestly and later
        reclaimed by storage cleanup as an orphan.
        """
        conn = self.db.conn
        try:
            deleted_chunks = conn.execute(
                "DELETE FROM rag_chunks WHERE document_id = ?", (document_id,)
            ).rowcount
            deleted_pages = conn.execute(
                "DELETE FROM document_pages WHERE document_id = ?", (document_id,)
            ).rowcount
            deleted_ocr_pages = conn.execute(
                "DELETE FROM ocr_pages WHERE document_id = ?", (document_id,)
            ).rowcount
            refs = conn.execute(
                "SELECT COUNT(*) AS n FROM message_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            tombstoned = int(refs["n"] or 0) > 0
            if tombstoned:
                conn.execute(
                    """
                    UPDATE documents
                    SET is_deleted = 1, status = 'deleted', index_status = 'deleted',
                        error = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_ms(), document_id),
                )
            else:
                conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            logger.warning("document delete failed reason=db_error")
            raise RuntimeError("delete_failed") from None
        file_removed, bytes_reclaimed, file_missing = self._remove_owned_file(stored_path)
        logger.info(
            "document deleted document_id=%s tombstoned=%s file_removed=%s file_missing=%s",
            document_id,
            tombstoned,
            file_removed,
            file_missing,
        )
        return {
            "tombstoned": tombstoned,
            "deleted_chunks": max(0, deleted_chunks),
            "deleted_pages": max(0, deleted_pages),
            "deleted_ocr_pages": max(0, deleted_ocr_pages),
            "file_removed": file_removed,
            "file_missing": file_missing,
            "bytes_reclaimed": bytes_reclaimed,
        }

    def promote(self, document_id: str) -> dict[str, Any]:
        document = self.get(document_id)
        if document["is_deleted"]:
            raise ValueError("cannot promote a deleted document")
        now = utc_ms()
        self.db.conn.execute(
            """
            UPDATE documents
            SET scope = 'library',
                promoted_at = COALESCE(promoted_at, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, document_id),
        )
        self.db.conn.commit()
        return self.get(document_id)

    def link_message(self, message_id: str, document_ids: list[str]) -> None:
        now = utc_ms()
        for order, document_id in enumerate(document_ids):
            document = self.get(document_id)
            if document["is_deleted"]:
                raise ValueError("cannot attach a deleted document")
            self.db.conn.execute(
                """
                INSERT OR IGNORE INTO message_documents(
                    message_id, document_id, display_order, scope_at_link, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    document_id,
                    order,
                    str(document.get("scope") or "library"),
                    now,
                ),
            )
        self.db.conn.commit()

    def attachments_for_messages(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not message_ids:
            return {}
        placeholders = ",".join("?" for _ in message_ids)
        rows = self.db.conn.execute(
            f"""
            SELECT md.message_id, md.display_order, md.scope_at_link, d.*
            FROM message_documents md
            JOIN documents d ON d.id = md.document_id
            WHERE md.message_id IN ({placeholders})
            ORDER BY md.message_id, md.display_order
            """,
            message_ids,
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {message_id: [] for message_id in message_ids}
        for row in rows:
            item = self._document_dict(row)
            item["display_order"] = int(row["display_order"])
            item["scope_at_link"] = str(row["scope_at_link"] or item.get("scope") or "library")
            item["status_label"] = "attachment deleted" if item["is_deleted"] else item["status"]
            grouped.setdefault(str(row["message_id"]), []).append(item)
        return grouped

    def hydrate_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped = self.attachments_for_messages([str(message["id"]) for message in messages])
        hydrated = []
        for message in messages:
            item = dict(message)
            item["documents"] = grouped.get(str(message["id"]), [])
            hydrated.append(item)
        return hydrated

    def needs_ocr(self, document_id: str) -> bool:
        document = self.get(document_id)
        if document["is_deleted"] or document["file_type"] != "pdf":
            return False
        ocr_status = str(document.get("ocr_status") or "")
        if ocr_status in {"indexed", "running"}:
            return False
        if (
            document["is_low_text"]
            or document["index_status"] == "low_text"
            or ocr_status in {"needed", "unavailable", "no_text"}
        ):
            return True
        pages = self.pages(document_id)
        if not pages:
            return True
        return any(len(str(page.get("text") or "").strip()) < PAGE_OCR_TEXT_MIN_CHARS for page in pages)

    def text_diagnostics(self, document_id: str) -> dict[str, Any]:
        document = self.get(document_id)
        pages = self.pages(document_id)
        ocr_pages = self.ocr_pages(document_id)
        chunks = self.chunks(document_id)
        extracted_text_char_count = sum(len(str(page.get("text") or "").strip()) for page in pages)
        ocr_text_char_count = sum(len(str(page.get("text") or "").strip()) for page in ocr_pages)
        ocr_quality_counts: dict[str, int] = {}
        ocr_attempt_count = 0
        ocr_crop_count = 0
        vlm_text_available = False
        vlm_text_char_count = 0
        vlm_backends: list[str] = []
        selected_attempts: list[dict[str, Any]] = []
        for page in ocr_pages:
            metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
            quality = metadata.get("ocr_quality") if isinstance(metadata.get("ocr_quality"), dict) else metadata.get("quality")
            label = str((quality or {}).get("label") or "")
            if label:
                ocr_quality_counts[label] = ocr_quality_counts.get(label, 0) + 1
            try:
                ocr_attempt_count += int(metadata.get("ocr_attempt_count") or 0)
            except (TypeError, ValueError):
                pass
            try:
                ocr_crop_count += int(metadata.get("ocr_crop_count") or 0)
            except (TypeError, ValueError):
                pass
            selected = metadata.get("selected_ocr_attempt")
            if isinstance(selected, dict):
                selected_attempts.append(selected)
            vlm = metadata.get("vlm_text_evidence") if isinstance(metadata.get("vlm_text_evidence"), dict) else {}
            if metadata.get("vlm_text_available") or int(metadata.get("vlm_text_char_count") or 0) > 0:
                vlm_text_available = True
            try:
                vlm_text_char_count += int(metadata.get("vlm_text_char_count") or vlm.get("text_char_count") or 0)
            except (TypeError, ValueError):
                pass
            backend = str(vlm.get("backend") or "")
            if backend and backend not in vlm_backends:
                vlm_backends.append(backend)
        return {
            "document_id": document_id,
            "file_type": str(document.get("file_type") or ""),
            "index_status": str(document.get("index_status") or ""),
            "ocr_status": str(document.get("ocr_status") or ""),
            "ocr_error": str(document.get("ocr_error") or ""),
            "is_low_text": bool(document.get("is_low_text")),
            "page_count": len(pages),
            "pages_with_extracted_text": sum(1 for page in pages if str(page.get("text") or "").strip()),
            "low_text_page_count": sum(
                1 for page in pages if len(str(page.get("text") or "").strip()) < PAGE_OCR_TEXT_MIN_CHARS
            ),
            "extracted_text_char_count": extracted_text_char_count,
            "ocr_page_count": len(ocr_pages),
            "ocr_pages_with_text": sum(1 for page in ocr_pages if str(page.get("text") or "").strip()),
            "ocr_text_char_count": ocr_text_char_count,
            "chunk_count": len(chunks),
            "needs_ocr": self.needs_ocr(document_id),
            "ocr_quality": aggregate_ocr_quality(ocr_quality_counts),
            "ocr_quality_counts": ocr_quality_counts,
            "ocr_attempt_count": ocr_attempt_count,
            "ocr_crop_count": ocr_crop_count,
            "ocr_selected_attempts": selected_attempts[:6],
            "vlm_text_available": vlm_text_available,
            "vlm_text_char_count": vlm_text_char_count,
            "vlm_text_backends": vlm_backends,
        }

    def _extract_pdf_pages(self, path: Path) -> list[ExtractedPage]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF text extraction requires pypdf. Install python/requirements.txt before importing PDFs."
            ) from exc

        reader = PdfReader(str(path))
        pages: list[ExtractedPage] = []
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append(
                ExtractedPage(
                    page_number=index,
                    text=text,
                    extraction_method="pdf_text",
                    metadata={"file_type": "pdf"},
                )
            )
        return pages

    def _update_status(self, document_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "index_status",
            "is_low_text",
            "error",
            "ocr_status",
            "ocr_engine",
            "ocr_error",
            "indexed_embedding_model",
            "indexed_embedding_backend",
        }
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return
        fields["updated_at"] = utc_ms()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.db.conn.execute(
            f"UPDATE documents SET {assignments} WHERE id = ?",
            [*fields.values(), document_id],
        )
        self.db.conn.commit()

    def _document_dict(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["is_deleted"] = bool(item["is_deleted"])
        item["is_low_text"] = bool(item["is_low_text"])
        item["is_internal"] = bool(item.get("is_internal", 0))
        item["is_staging"] = bool(item.get("is_staging", 0))
        item["scope"] = str(item.get("scope") or "library")
        return item


def normalize_source_scope(value: str | None) -> str:
    cleaned = (value or "library").strip()
    if cleaned not in SOURCE_SCOPES:
        raise ValueError("source scope must be library or session")
    return cleaned


def aggregate_ocr_quality(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    for label in ("poor", "usable_noisy", "good", "no_text"):
        if counts.get(label):
            return label
    return next(iter(counts))
