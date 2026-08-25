from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.services.document_service import DocumentService, ExtractedPage
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.web_extraction import ExtractedWebPage
from odysseus_desktop_backend.services.web_fetcher import FetchResponse
from odysseus_desktop_backend.storage import utc_ms


SAFE_FILE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


class WebSourceStoreError(RuntimeError):
    code = "search_cache_failed"


class WebSourceStore:
    """Persists fetched observations in the existing document/RAG corpus."""

    def __init__(self, documents: DocumentService, rag: RAGService):
        self.documents = documents
        self.rag = rag

    def persist(
        self,
        *,
        canonical_url: str,
        fetch: FetchResponse,
        extracted: ExtractedWebPage,
        provider_metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        try:
            return self._persist(
                canonical_url=canonical_url,
                fetch=fetch,
                extracted=extracted,
                provider_metadata=provider_metadata,
            )
        except (OSError, sqlite3.Error) as exc:
            raise WebSourceStoreError("web Search cache persistence failed") from exc

    def _persist(
        self,
        *,
        canonical_url: str,
        fetch: FetchResponse,
        extracted: ExtractedWebPage,
        provider_metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        text_bytes = extracted.text.encode("utf-8")
        text_hash = hashlib.sha256(text_bytes).hexdigest()
        response_hash = hashlib.sha256(fetch.body).hexdigest()
        now = utc_ms()
        existing = self.documents.db.conn.execute(
            """
            SELECT id
            FROM documents
            WHERE is_deleted = 0 AND source_origin IN ('web', 'cached_web')
              AND canonical_url = ? AND content_hash = ?
            ORDER BY web_revision_current DESC, fetched_at DESC
            LIMIT 1
            """,
            (canonical_url, text_hash),
        ).fetchone()
        acquisition = {
            "response_hash": response_hash,
            "response_bytes": len(fetch.body),
            "bytes_downloaded": int(fetch.bytes_downloaded),
            "redirect_count": int(fetch.redirects),
            "visual_candidate_signals": list(extracted.visual_signals),
            "extraction_metadata": dict(extracted.metadata),
            "provider": dict(provider_metadata or {}),
        }
        if existing is not None:
            document_id = str(existing["id"])
            self.documents.db.conn.execute(
                """
                UPDATE documents
                SET source_origin = 'cached_web', final_url = ?, fetched_at = ?,
                    http_content_type = ?, http_etag = ?, http_last_modified = ?,
                    acquisition_metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    fetch.final_url,
                    now,
                    fetch.content_type,
                    fetch.headers.get("etag", ""),
                    fetch.headers.get("last-modified", ""),
                    json.dumps(acquisition, ensure_ascii=False, separators=(",", ":")),
                    now,
                    document_id,
                ),
            )
            self.documents.db.conn.commit()
            return self.documents.get(document_id), True

        document_id = str(uuid.uuid4())
        host = canonical_url.split("/", 3)[2].split(":", 1)[0]
        safe_host = SAFE_FILE_RE.sub("-", host).strip("-.")[:80] or "web-source"
        file_name = f"{safe_host}-{text_hash[:10]}.txt"
        stored_path = self.documents.documents_dir / f"{document_id}.txt"
        stored_path.write_bytes(text_bytes)
        title = " ".join(str(extracted.title or host).replace("\x00", " ").split()).strip()[:300] or host
        self.documents.db.conn.execute(
            """
            INSERT INTO documents(
                id, title, source_path, stored_path, file_name, file_type, content_hash,
                size_bytes, status, index_status, is_deleted, is_low_text, error,
                created_at, updated_at, indexed_at, scope, is_staging, source_origin,
                canonical_url, final_url, fetched_at, http_content_type, http_etag,
                http_last_modified, acquisition_metadata_json, web_revision_current
            )
            VALUES (?, ?, ?, ?, ?, 'txt', ?, ?, 'imported', 'pending', 0, 0, '',
                    ?, ?, NULL, 'library', 1, 'web', ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                document_id,
                title,
                canonical_url,
                str(stored_path),
                file_name,
                text_hash,
                len(text_bytes),
                now,
                now,
                canonical_url,
                fetch.final_url,
                now,
                fetch.content_type,
                fetch.headers.get("etag", ""),
                fetch.headers.get("last-modified", ""),
                json.dumps(acquisition, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        self.documents.db.conn.commit()
        page_metadata = {
            "source_origin": "web",
            "canonical_url": canonical_url,
            "final_url": fetch.final_url,
            "fetched_at": now,
            "provenance_kind": extracted.provenance_kind,
            **dict(extracted.metadata),
        }
        self.documents.replace_pages(
            document_id,
            [
                ExtractedPage(
                    page_number=1,
                    text=extracted.text,
                    extraction_method=f"web_{extracted.provenance_kind}",
                    metadata=page_metadata,
                )
            ],
        )
        try:
            self.rag.index_document(document_id)
        except Exception:
            self.documents.purge_document(document_id)
            raise
        return self.documents.get(document_id), False

    def finalize_success(self, revisions: list[tuple[str, str]]) -> None:
        """Promote observed revisions into the internal Search cache atomically."""
        now = utc_ms()
        with self.documents.db.conn:
            for document_id, canonical_url in revisions:
                old_rows = self.documents.db.conn.execute(
                    """
                    SELECT id FROM documents
                    WHERE is_deleted = 0 AND source_origin IN ('web', 'cached_web')
                      AND canonical_url = ? AND id <> ? AND web_revision_current = 1
                    """,
                    (canonical_url, document_id),
                ).fetchall()
                old_ids = [str(row["id"]) for row in old_rows]
                if old_ids:
                    placeholders = ",".join("?" for _ in old_ids)
                    self.documents.db.conn.execute(
                        f"UPDATE documents SET web_revision_current = 0, updated_at = ? WHERE id IN ({placeholders})",
                        (now, *old_ids),
                    )
                    self.documents.db.conn.execute(
                        f"UPDATE rag_chunks SET is_deleted = 1, updated_at = ? WHERE document_id IN ({placeholders})",
                        (now, *old_ids),
                    )
                self.documents.db.conn.execute(
                    """
                    UPDATE documents
                    SET is_staging = 0, web_revision_current = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, document_id),
                )
                self.documents.db.conn.execute(
                    "UPDATE rag_chunks SET is_deleted = 0, updated_at = ? WHERE document_id = ?",
                    (now, document_id),
                )

    def rollback_new(self, document_ids: list[str]) -> None:
        for document_id in dict.fromkeys(document_ids):
            row = self.documents.db.conn.execute(
                "SELECT is_staging FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is not None and bool(row["is_staging"]):
                self.documents.purge_document(document_id)


def acquisition_metadata(document: dict[str, Any]) -> dict[str, Any]:
    raw = document.get("acquisition_metadata_json")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
