from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.pathsafety import is_link
from odysseus_desktop_backend.services.artifact_service import ArtifactService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.storage import Database, utc_ms


logger = get_logger("storage")

# Structural safety constants — bounded by design, not tuned Potato Mode
# defaults (V04_STORAGE_CLEANUP_DESIGN.md §8/§10).
LOW_DISK_THRESHOLD_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB
ORPHAN_MIN_AGE_MS = 10 * 60 * 1000  # files younger than this are never orphans

STORAGE_CATEGORIES = ("database", "documents", "images", "logs", "models", "other")

_DB_FILE_NAMES = {"app.db", "app.db-wal", "app.db-shm"}


class StorageService:
    """Profile storage accounting and allow-listed cleanup.

    Scans never follow symlinks/junctions, never leave the profile root, and
    log fixed labels and counts only — never paths or file names
    (V04_STORAGE_CLEANUP_DESIGN.md §7/§10).
    """

    def __init__(
        self,
        db: Database,
        documents: DocumentService,
        artifacts: ArtifactService,
        has_active_jobs: Callable[[], bool] | None = None,
    ):
        self.db = db
        self.profile_dir = Path(db.profile_dir)
        self.documents = documents
        self.artifacts = artifacts
        self._has_active_jobs = has_active_jobs or (lambda: False)

    # ------------------------------------------------------------- accounting

    def status(self) -> dict[str, Any]:
        scan = self._scan_profile()
        try:
            usage = shutil.disk_usage(self.profile_dir)
            free_disk_bytes = int(usage.free)
        except OSError:
            free_disk_bytes = -1
        low_disk = 0 <= free_disk_bytes < LOW_DISK_THRESHOLD_BYTES
        counts = self.db.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM documents
                 WHERE is_deleted = 0 AND COALESCE(is_internal, 0) = 0
                   AND COALESCE(is_staging, 0) = 0) AS documents,
                (SELECT COUNT(*) FROM artifacts WHERE is_deleted = 0) AS images
            """
        ).fetchone()
        return {
            "profile_dir": str(self.profile_dir),
            "total_bytes": scan["total_bytes"],
            "categories": scan["categories"],
            "skipped_count": scan["skipped_count"],
            "free_disk_bytes": free_disk_bytes,
            "low_disk": low_disk,
            "low_disk_threshold_bytes": LOW_DISK_THRESHOLD_BYTES,
            "source_counts": {
                "documents": int(counts["documents"] or 0),
                "images": int(counts["images"] or 0),
            },
        }

    def _scan_profile(self) -> dict[str, Any]:
        categories = {
            name: {"bytes": 0, "files": 0} for name in STORAGE_CATEGORIES
        }
        skipped = 0
        try:
            root = self.profile_dir.resolve(strict=True)
        except OSError:
            logger.warning("storage scan skipped reason=profile_unresolvable")
            return {
                "total_bytes": 0,
                "categories": categories,
                "skipped_count": 1,
            }
        pending: list[tuple[Path, str | None]] = [(root, None)]
        while pending:
            directory, category = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                skipped += 1
                continue
            for entry in entries:
                link = is_link(entry.path)
                if link is None:
                    skipped += 1
                    continue
                if link:
                    # Symlinks and junctions are counted as skipped and never
                    # sized or descended into — their targets may live outside
                    # the profile.
                    skipped += 1
                    continue
                try:
                    entry_category = category or self._top_level_category(root, entry)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((Path(entry.path), entry_category))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        skipped += 1
                        continue
                    size = int(entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    skipped += 1
                    continue
                bucket = categories[entry_category]
                bucket["bytes"] += size
                bucket["files"] += 1
        total = sum(bucket["bytes"] for bucket in categories.values())
        return {
            "total_bytes": total,
            "categories": categories,
            "skipped_count": skipped,
        }

    def _top_level_category(self, root: Path, entry: os.DirEntry[str]) -> str:
        name = entry.name
        try:
            relative = Path(entry.path).relative_to(root)
        except ValueError:
            return "other"
        top = relative.parts[0] if relative.parts else name
        if top in _DB_FILE_NAMES:
            return "database"
        if top == "files":
            if len(relative.parts) >= 2 and relative.parts[1] == "documents":
                return "documents"
            if len(relative.parts) >= 2 and relative.parts[1] == "artifacts":
                return "images"
            # `files` itself or an unknown child: classify by the child when
            # descending (directories pass their category down), else other.
            if len(relative.parts) == 1 and entry.is_dir(follow_symlinks=False):
                return ""  # resolved per child on descent
            return "other"
        if top == "logs":
            return "logs"
        if top == "models":
            return "models"
        return "other"

    # -------------------------------------------------------------- cleanup

    def cleanup_preview(self) -> dict[str, Any]:
        cache_rows, cache_bytes = self._cache_candidates()
        orphans = self._orphan_candidates()
        legacy = self._legacy_candidates()
        orphan_bytes = sum(size for _, size in orphans["candidates"])
        return {
            "embedding_cache": {"rows": cache_rows, "bytes": cache_bytes},
            "orphan_files": {
                "files": len(orphans["candidates"]),
                "bytes": orphan_bytes,
                "skipped": orphans["skipped"],
            },
            "legacy_deleted_sources": {
                "documents": legacy["documents"],
                "artifacts": legacy["artifacts"],
                "rows": legacy["rows"],
                "files": legacy["files"],
                "bytes": legacy["bytes"],
            },
            "reclaimable_file_bytes": orphan_bytes + legacy["bytes"],
            "reclaimable_db_bytes": cache_bytes,
        }

    def cleanup(self) -> dict[str, Any]:
        if self._has_active_jobs():
            # Fixed code; the frontend maps this to fixed copy. Cleanup must
            # not race a staging import between file copy and row insert.
            raise RuntimeError("cleanup_busy")
        cache_result = self._cleanup_cache()
        orphan_result = self._cleanup_orphans()
        legacy_result = self._cleanup_legacy()
        result = {
            "embedding_cache": cache_result,
            "orphan_files": orphan_result,
            "legacy_deleted_sources": legacy_result,
            "reclaimed_file_bytes": orphan_result["bytes"] + legacy_result["bytes"],
            "reclaimed_db_bytes": cache_result["bytes"],
            "skipped_count": orphan_result["skipped"] + legacy_result["skipped"],
            "failed_count": orphan_result["failed"] + legacy_result["failed"],
        }
        logger.info(
            "storage cleanup complete cache_rows=%s orphan_files=%s legacy_documents=%s "
            "legacy_artifacts=%s file_bytes=%s skipped=%s failed=%s",
            cache_result["rows"],
            orphan_result["files"],
            legacy_result["documents"],
            legacy_result["artifacts"],
            result["reclaimed_file_bytes"],
            result["skipped_count"],
            result["failed_count"],
        )
        return result

    # ---------------------------------------------------- embedding cache

    _UNUSED_CACHE_WHERE = """
        NOT EXISTS (
            SELECT 1 FROM rag_chunks c
            WHERE c.is_deleted = 0
              AND c.embedding_hash = embedding_cache.content_hash
              AND c.embedding_model = embedding_cache.embedding_model
        )
    """

    def _cache_candidates(self) -> tuple[int, int]:
        row = self.db.conn.execute(
            f"""
            SELECT COUNT(*) AS rows_count,
                   COALESCE(SUM(LENGTH(vector_blob)), 0) AS blob_bytes
            FROM embedding_cache
            WHERE {self._UNUSED_CACHE_WHERE}
            """
        ).fetchone()
        return int(row["rows_count"] or 0), int(row["blob_bytes"] or 0)

    def _cleanup_cache(self) -> dict[str, int]:
        rows, blob_bytes = self._cache_candidates()
        if rows:
            self.db.conn.execute(
                f"DELETE FROM embedding_cache WHERE {self._UNUSED_CACHE_WHERE}"
            )
            self.db.conn.commit()
        # Bytes become reusable inside the database file; VACUUM is never run
        # automatically (V04_ESSENTIAL_SEMANTICS.md §D).
        return {"rows": rows, "bytes": blob_bytes}

    # -------------------------------------------------------- orphan files

    def _allowlisted_dirs(self) -> list[Path]:
        return [
            self.documents.documents_dir,
            self.artifacts.originals_dir,
            self.artifacts.normalized_dir,
            self.artifacts.vision_dir,
            self.artifacts.ocr_dir,
            self.artifacts.thumbnails_dir,
            self.artifacts.crops_dir,
            self.artifacts.captures_dir,
        ]

    def _live_paths(self) -> set[str]:
        live: set[str] = set()
        rows: Iterable[Any] = self.db.conn.execute(
            "SELECT stored_path FROM documents WHERE is_deleted = 0"
        )
        for row in rows:
            value = str(row["stored_path"] or "")
            if value:
                live.add(os.path.normcase(str(Path(value))))
        for row in self.db.conn.execute(
            "SELECT stored_path FROM artifacts WHERE is_deleted = 0"
        ):
            value = str(row["stored_path"] or "")
            if value:
                live.add(os.path.normcase(str(Path(value))))
        for row in self.db.conn.execute(
            """
            SELECT d.stored_path AS stored_path
            FROM artifact_derivations d
            JOIN artifacts a ON a.id = d.artifact_id
            WHERE a.is_deleted = 0 AND d.stored_path <> ''
            """
        ):
            live.add(os.path.normcase(str(Path(str(row["stored_path"])))))
        return live

    def _orphan_candidates(self) -> dict[str, Any]:
        live = self._live_paths()
        now = utc_ms()
        candidates: list[tuple[Path, int]] = []
        skipped = 0
        for directory in self._allowlisted_dirs():
            try:
                resolved_dir = directory.resolve(strict=True)
            except OSError:
                continue
            try:
                entries = list(os.scandir(resolved_dir))
            except OSError:
                skipped += 1
                continue
            for entry in entries:
                link = is_link(entry.path)
                if link is None or link:
                    skipped += 1
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        # Unknown subdirectories are never removed.
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    continue
                candidate = Path(entry.path)
                if candidate.parent != resolved_dir:
                    skipped += 1
                    continue
                if os.path.normcase(str(candidate)) in live:
                    continue
                mtime_ms = int(st.st_mtime * 1000)
                if now - mtime_ms < ORPHAN_MIN_AGE_MS:
                    # Belt-and-braces against racing a writer; a file this
                    # young is never a cleanup candidate.
                    continue
                candidates.append((candidate, int(st.st_size)))
        candidates.sort(key=lambda item: str(item[0]))
        return {"candidates": candidates, "skipped": skipped}

    def _cleanup_orphans(self) -> dict[str, int]:
        enumerated = self._orphan_candidates()
        removed = 0
        removed_bytes = 0
        failed = 0
        for candidate, size in enumerated["candidates"]:
            link = is_link(candidate)
            if link is None or link:
                failed += 1
                continue
            try:
                candidate.unlink()
                removed += 1
                removed_bytes += size
            except OSError:
                logger.warning("orphan removal failed reason=os_error")
                failed += 1
        return {
            "files": removed,
            "bytes": removed_bytes,
            "skipped": enumerated["skipped"],
            "failed": failed,
        }

    # ------------------------------------------- legacy soft-deleted sources

    def _legacy_candidates(self) -> dict[str, int]:
        doc_rows = self.db.conn.execute(
            "SELECT id, stored_path FROM documents WHERE is_deleted = 1"
        ).fetchall()
        artifact_rows = self.db.conn.execute(
            "SELECT id, stored_path FROM artifacts WHERE is_deleted = 1"
        ).fetchall()
        derived = self.db.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM rag_chunks WHERE is_deleted = 1
                    OR document_id IN (SELECT id FROM documents WHERE is_deleted = 1)) AS chunk_rows,
                (SELECT COUNT(*) FROM document_pages
                 WHERE document_id IN (SELECT id FROM documents WHERE is_deleted = 1)) AS page_rows,
                (SELECT COUNT(*) FROM ocr_pages
                 WHERE document_id IN (SELECT id FROM documents WHERE is_deleted = 1)) AS ocr_rows,
                (SELECT COUNT(*) FROM artifact_derivations
                 WHERE artifact_id IN (SELECT id FROM artifacts WHERE is_deleted = 1)) AS derivation_rows,
                (SELECT COUNT(*) FROM documents WHERE is_deleted = 1
                    AND id NOT IN (SELECT document_id FROM message_documents)) AS hard_doc_rows,
                (SELECT COUNT(*) FROM artifacts WHERE is_deleted = 1
                    AND id NOT IN (SELECT artifact_id FROM message_artifacts)
                    AND id NOT IN (SELECT artifact_id FROM artifact_analysis_runs)) AS hard_artifact_rows
            """
        ).fetchone()
        files = 0
        file_bytes = 0
        for row in doc_rows:
            size = self._measure_owned_file(
                str(row["stored_path"] or ""), self.documents.documents_dir
            )
            if size is not None:
                files += 1
                file_bytes += size
        for row in artifact_rows:
            size = self._measure_owned_file(
                str(row["stored_path"] or ""), self.artifacts.root, direct_child=False
            )
            if size is not None:
                files += 1
                file_bytes += size
        return {
            "documents": len(doc_rows),
            "artifacts": len(artifact_rows),
            "rows": int(derived["chunk_rows"] or 0)
            + int(derived["page_rows"] or 0)
            + int(derived["ocr_rows"] or 0)
            + int(derived["derivation_rows"] or 0),
            "files": files,
            "bytes": file_bytes,
        }

    def _measure_owned_file(
        self, stored_path: str, owning_dir: Path, *, direct_child: bool = True
    ) -> int | None:
        if not stored_path:
            return None
        candidate = Path(stored_path)
        try:
            root = owning_dir.resolve(strict=True)
        except OSError:
            return None
        try:
            link = is_link(candidate)
            if link is None or link or not candidate.exists():
                return None
            resolved = candidate.resolve(strict=True)
            if direct_child:
                if resolved.parent != root:
                    return None
            else:
                resolved.relative_to(root)
            if not resolved.is_file():
                return None
            return int(resolved.stat().st_size)
        except (OSError, ValueError):
            return None

    def _cleanup_legacy(self) -> dict[str, int]:
        documents_purged = 0
        artifacts_purged = 0
        rows_removed = 0
        files_removed = 0
        file_bytes = 0
        skipped = 0
        failed = 0

        doc_rows = self.db.conn.execute(
            "SELECT id FROM documents WHERE is_deleted = 1 ORDER BY id"
        ).fetchall()
        for row in doc_rows:
            result = self.documents.purge_deleted_document(str(row["id"]))
            documents_purged += 1
            rows_removed += result["deleted_rows"]
            if result["file_removed"]:
                files_removed += 1
                file_bytes += result["bytes_reclaimed"]
            elif not result["file_missing"]:
                failed += 1

        artifact_rows = self.db.conn.execute(
            "SELECT id FROM artifacts WHERE is_deleted = 1 ORDER BY id"
        ).fetchall()
        for row in artifact_rows:
            result = self.artifacts.purge_deleted_artifact(str(row["id"]))
            artifacts_purged += 1
            rows_removed += result["deleted_rows"]
            files_removed += result["files_removed"]
            file_bytes += result["bytes_reclaimed"]
            failed += result["failed_files"]

        # Soft-deleted chunk rows left on live documents by reindexing are
        # unreachable by every query (is_deleted = 0 filters) and safe to drop.
        cursor = self.db.conn.execute("DELETE FROM rag_chunks WHERE is_deleted = 1")
        rows_removed += max(0, cursor.rowcount)
        self.db.conn.commit()

        return {
            "documents": documents_purged,
            "artifacts": artifacts_purged,
            "rows": rows_removed,
            "files": files_removed,
            "bytes": file_bytes,
            "skipped": skipped,
            "failed": failed,
        }
