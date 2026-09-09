from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.pathsafety import is_link
from odysseus_desktop_backend.services.document_service import DocumentService, normalize_source_scope
from odysseus_desktop_backend.services.image_preprocessing_service import (
    IMAGE_PREPROCESS_VERSION,
    MAX_DECODED_PIXELS,
    MAX_ORIGINAL_BYTES,
    OCR_MAX_EDGE,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_IMAGE_FORMATS,
    THUMBNAIL_MAX_EDGE,
    VISION_JPEG_QUALITY,
    VISION_MAX_EDGE,
    VISION_MAX_PIXELS,
    ImagePreprocessingError,
    ImagePreprocessingService,
)
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.storage import Database, utc_ms


MAX_IMAGES_PER_TURN = 4
NORMALIZED_MAX_EDGE = VISION_MAX_EDGE
ARTIFACT_PROMPT_VERSION = "image-analysis-v0.2.0"
logger = get_logger("artifacts")


class ArtifactError(ValueError):
    category = "artifact_error"


class ArtifactPathError(ArtifactError):
    category = "path_confined"


class ArtifactValidationError(ArtifactError):
    category = "invalid_image"


class ArtifactService:
    def __init__(self, db: Database, documents: DocumentService | None = None, rag: RAGService | None = None):
        self.db = db
        self.documents = documents
        self.rag = rag
        self.root = self.db.profile_dir / "files" / "artifacts"
        self.originals_dir = self.root / "originals"
        self.normalized_dir = self.root / "normalized"
        self.vision_dir = self.root / "vision"
        self.ocr_dir = self.root / "ocr"
        self.thumbnails_dir = self.root / "thumbnails"
        self.crops_dir = self.root / "crops"
        self.captures_dir = self.root / "captures"
        self.preprocessor = ImagePreprocessingService()
        for path in (
            self.originals_dir,
            self.normalized_dir,
            self.vision_dir,
            self.ocr_dir,
            self.thumbnails_dir,
            self.crops_dir,
            self.captures_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def limits(self) -> dict[str, Any]:
        return {
            "supported_formats": sorted(SUPPORTED_IMAGE_EXTENSIONS),
            "max_original_bytes": MAX_ORIGINAL_BYTES,
            "max_decoded_pixels": MAX_DECODED_PIXELS,
            "max_images_per_turn": MAX_IMAGES_PER_TURN,
            "preprocessing_version": IMAGE_PREPROCESS_VERSION,
            "thumbnail_max_edge": THUMBNAIL_MAX_EDGE,
            "vision_max_edge": VISION_MAX_EDGE,
            "vision_max_pixels": VISION_MAX_PIXELS,
            "vision_jpeg_quality": VISION_JPEG_QUALITY,
            "ocr_max_edge": OCR_MAX_EDGE,
            "normalized_max_edge": NORMALIZED_MAX_EDGE,
            "storage_root": str(self.root),
        }

    def import_path(
        self,
        path: str,
        *,
        source_kind: str = "file",
        name: str | None = None,
        scope: str = "library",
    ) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"image file not found: {path}")
        if source.stat().st_size > MAX_ORIGINAL_BYTES:
            raise ArtifactValidationError(f"image exceeds {MAX_ORIGINAL_BYTES} byte limit")
        content = source.read_bytes()
        original_hash = hashlib.sha256(content).hexdigest()
        try:
            decoded = self.preprocessor.decode(source)
        except ImagePreprocessingError as exc:
            raise ArtifactValidationError(str(exc)) from exc
        image = decoded.image
        mime_type = str(decoded.mime_type)
        original_extension = str(decoded.extension)
        artifact_id = str(uuid.uuid4())
        stored_path = self.originals_dir / f"{artifact_id}-{original_hash[:16]}{original_extension}"
        shutil.copyfile(source, stored_path)
        stored_path = self._confined(stored_path)

        derivatives = self.preprocessor.write_derivatives(
            artifact_id=artifact_id,
            image=image,
            original_hash=original_hash,
            output_root=self.root,
        )
        now = utc_ms()
        display_name = clean_display_name(name or source.stem or "Image")
        clean_scope = normalize_source_scope(scope)
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO artifacts(
                    id, kind, source_kind, name, original_filename, mime_type,
                    original_extension, stored_path, content_hash, size_bytes,
                    width, height, normalized_orientation, status, error,
                    is_deleted, created_at, updated_at, scope,
                    original_format, original_color_mode, original_pixel_count,
                    original_has_alpha, original_exif_orientation, preprocessing_version
                )
                VALUES (?, 'image', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', '', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    normalize_source_kind(source_kind),
                    display_name,
                    source.name if source_kind == "file" else "",
                    mime_type,
                    original_extension,
                    str(stored_path),
                    original_hash,
                    len(content),
                    int(decoded.width),
                    int(decoded.height),
                    1 if decoded.orientation_normalized else 0,
                    now,
                    now,
                    clean_scope,
                    decoded.format,
                    decoded.original_mode,
                    int(decoded.original_pixel_count),
                    1 if decoded.has_alpha else 0,
                    decoded.exif_orientation,
                    IMAGE_PREPROCESS_VERSION,
                ),
            )
            self._insert_image_derivatives(artifact_id, derivatives, decoded.pillow_version, commit=False)
        logger.info("artifact imported artifact_id=%s source_kind=%s bytes=%s", artifact_id, source_kind, len(content))
        return self.get(artifact_id)

    def import_many(self, paths: list[str], *, source_kind: str = "file", scope: str = "library") -> dict[str, Any]:
        imported: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for path in paths[:MAX_IMAGES_PER_TURN]:
            try:
                imported.append(self.import_path(path, source_kind=source_kind, scope=scope))
            except Exception as exc:  # noqa: BLE001 - partial success is intentional
                failed.append({"path": path, "error": str(exc)})
        skipped = [{"path": path, "error": "image attachment limit reached"} for path in paths[MAX_IMAGES_PER_TURN:]]
        return {"imported": imported, "failed": failed + skipped}

    def list(self, *, include_deleted: bool = False, scope: str | None = "library") -> list[dict[str, Any]]:
        query = "SELECT * FROM artifacts"
        filters: list[str] = []
        values: list[Any] = []
        if not include_deleted:
            filters.append("is_deleted = 0")
        if scope is not None:
            filters.append("COALESCE(scope, 'library') = ?")
            values.append(normalize_source_scope(scope))
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY updated_at DESC"
        rows = self.db.conn.execute(query, values).fetchall()
        return [self._artifact_dict(row) for row in rows]

    def get(self, artifact_id: str) -> dict[str, Any]:
        row = self.db.conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        item = self._artifact_dict(row)
        item["derivations"] = self.derivations(artifact_id)
        return item

    def derivations(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT *
            FROM artifact_derivations
            WHERE artifact_id = ?
            ORDER BY created_at ASC
            """,
            (artifact_id,),
        ).fetchall()
        return [derivation_dict(row) for row in rows]

    def thumbnail_path(self, artifact_id: str) -> str:
        derivation = self._ensure_derivative(artifact_id, "thumbnail")
        return str(derivation.get("stored_path") or "")

    def normalized_image_path(self, artifact_id: str) -> str:
        derivation = self._ensure_derivative(artifact_id, "normalized_image")
        return str(derivation.get("stored_path") or self.ocr_image_path(artifact_id))

    def vision_image_path(self, artifact_id: str) -> str:
        derivation = self._ensure_derivative(artifact_id, "vision_input")
        return str(derivation.get("stored_path") or "")

    def ocr_image_path(self, artifact_id: str) -> str:
        derivation = self._ensure_derivative(artifact_id, "ocr_input")
        return str(derivation.get("stored_path") or "")

    def derivative_details(self, artifact_id: str) -> dict[str, Any]:
        row = self.db.conn.execute(
            """
            SELECT content_hash, size_bytes, width, height, original_format,
                   original_color_mode, original_pixel_count, original_has_alpha,
                   original_exif_orientation, preprocessing_version
            FROM artifacts
            WHERE id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        details: dict[str, Any] = {
            "original": {
                "content_hash": row["content_hash"],
                "format": row["original_format"] or "",
                "width": int(row["width"] or 0),
                "height": int(row["height"] or 0),
                "pixels": int(row["width"] or 0) * int(row["height"] or 0),
                "byte_size": int(row["size_bytes"] or 0),
                "color_mode": row["original_color_mode"] or "",
                "original_pixel_count": int(row["original_pixel_count"] or 0),
                "has_alpha": bool(row["original_has_alpha"] or 0),
                "exif_orientation": row["original_exif_orientation"] or "",
                "preprocessing_version": row["preprocessing_version"] or "",
            },
        }
        for kind, label in (("thumbnail", "thumbnail"), ("vision_input", "vision_input"), ("ocr_input", "ocr_input")):
            derivation = self.latest_derivation(artifact_id, kind)
            if not derivation:
                continue
            metadata = dict(derivation.get("metadata") or {})
            details[label] = {
                "derivative_id": derivation.get("id"),
                "derivative_kind": kind,
                "format": metadata.get("format") or "",
                "width": int(metadata.get("width") or 0),
                "height": int(metadata.get("height") or 0),
                "pixels": int(metadata.get("pixels") or 0),
                "byte_size": int(metadata.get("size_bytes") or 0),
                "mime_type": metadata.get("mime_type") or "",
                "color_mode": metadata.get("color_mode") or "",
                "preprocessing_version": metadata.get("preprocessing_version") or "",
                "alpha_flattened": bool(metadata.get("alpha_flattened") or False),
                "alpha_background": metadata.get("alpha_background") or "",
                "max_edge": int(metadata.get("max_edge") or 0),
                "max_pixels": int(metadata.get("max_pixels") or 0),
                "jpeg_quality": int(metadata.get("jpeg_quality") or 0),
                "content_hash": derivation.get("content_hash") or "",
            }
        return details

    def latest_derivation(self, artifact_id: str, kind: str) -> dict[str, Any]:
        row = self.db.conn.execute(
            """
            SELECT *
            FROM artifact_derivations
            WHERE artifact_id = ? AND kind = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (artifact_id, kind),
        ).fetchone()
        if row is None:
            return {}
        return derivation_dict(row)

    def _ensure_derivative(self, artifact_id: str, kind: str) -> dict[str, Any]:
        derivation = self.latest_derivation(artifact_id, kind)
        if self._valid_image_derivation(derivation):
            return derivation
        self._regenerate_image_derivatives(artifact_id)
        derivation = self.latest_derivation(artifact_id, kind)
        if kind == "normalized_image" and not derivation:
            derivation = self.latest_derivation(artifact_id, "ocr_input")
        if not self._valid_image_derivation(derivation):
            raise ArtifactError(f"{kind} derivative is unavailable")
        return derivation

    def _valid_image_derivation(self, derivation: dict[str, Any]) -> bool:
        if not derivation:
            return False
        metadata = derivation.get("metadata") if isinstance(derivation.get("metadata"), dict) else {}
        if metadata.get("preprocessing_version") != IMAGE_PREPROCESS_VERSION:
            return False
        path_value = str(derivation.get("stored_path") or "")
        if not path_value:
            return False
        try:
            path = self._confined(Path(path_value))
        except ArtifactPathError:
            return False
        if not path.exists() or not path.is_file():
            return False
        content_hash = str(derivation.get("content_hash") or "")
        return not content_hash or file_sha256(path) == content_hash

    def _regenerate_image_derivatives(self, artifact_id: str) -> None:
        row = self.db.conn.execute("SELECT stored_path, content_hash FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        source = self._confined(Path(str(row["stored_path"])))
        try:
            decoded = self.preprocessor.decode(source)
        except ImagePreprocessingError as exc:
            raise ArtifactValidationError(str(exc)) from exc
        derivatives = self.preprocessor.write_derivatives(
            artifact_id=artifact_id,
            image=decoded.image,
            original_hash=str(row["content_hash"] or ""),
            output_root=self.root,
        )
        with self.db.conn:
            self._insert_image_derivatives(artifact_id, derivatives, decoded.pillow_version, commit=False)
            self.db.conn.execute(
                """
                UPDATE artifacts
                SET original_format = COALESCE(NULLIF(original_format, ''), ?),
                    original_color_mode = COALESCE(NULLIF(original_color_mode, ''), ?),
                    original_pixel_count = COALESCE(NULLIF(original_pixel_count, 0), ?),
                    original_has_alpha = COALESCE(NULLIF(original_has_alpha, 0), ?),
                    original_exif_orientation = COALESCE(NULLIF(original_exif_orientation, ''), ?),
                    preprocessing_version = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    decoded.format,
                    decoded.original_mode,
                    int(decoded.original_pixel_count),
                    1 if decoded.has_alpha else 0,
                    decoded.exif_orientation,
                    IMAGE_PREPROCESS_VERSION,
                    utc_ms(),
                    artifact_id,
                ),
            )

    def _insert_image_derivatives(
        self,
        artifact_id: str,
        derivatives: dict[str, dict[str, Any]],
        pillow_version: str,
        *,
        commit: bool = True,
    ) -> None:
        for kind in ("thumbnail", "vision_input", "ocr_input"):
            metadata = dict(derivatives[kind])
            stored_path = str(metadata.pop("stored_path"))
            content_hash = str(metadata.pop("content_hash"))
            self._insert_derivation(
                artifact_id,
                kind,
                stored_path=stored_path,
                content_hash=content_hash,
                producer_type="preprocessor",
                producer_name="Pillow",
                producer_version=pillow_version,
                prompt_version=IMAGE_PREPROCESS_VERSION,
                metadata=metadata,
                status="created",
                commit=False,
            )
        ocr_metadata = dict(derivatives["ocr_input"])
        normalized_path = str(ocr_metadata.pop("stored_path"))
        normalized_hash = str(ocr_metadata.pop("content_hash"))
        self._insert_derivation(
            artifact_id,
            "normalized_image",
            stored_path=normalized_path,
            content_hash=normalized_hash,
            producer_type="preprocessor",
            producer_name="Pillow",
            producer_version=pillow_version,
            prompt_version=IMAGE_PREPROCESS_VERSION,
            metadata={**ocr_metadata, "kind": "normalized_image", "alias_for": "ocr_input"},
            status="created",
            commit=False,
        )
        if commit:
            self.db.conn.commit()

    def link_message(self, message_id: str, artifact_ids: list[str]) -> None:
        now = utc_ms()
        for order, artifact_id in enumerate(artifact_ids[:MAX_IMAGES_PER_TURN]):
            artifact = self.get(artifact_id)
            if artifact["is_deleted"]:
                raise ArtifactError("cannot attach a deleted artifact")
            self.db.conn.execute(
                """
                INSERT OR IGNORE INTO message_artifacts(message_id, artifact_id, display_order, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, artifact_id, order, now),
            )
        self.db.conn.commit()

    def attachments_for_messages(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not message_ids:
            return {}
        placeholders = ",".join("?" for _ in message_ids)
        rows = self.db.conn.execute(
            f"""
            SELECT ma.message_id, ma.display_order, a.*
            FROM message_artifacts ma
            JOIN artifacts a ON a.id = ma.artifact_id
            WHERE ma.message_id IN ({placeholders})
            ORDER BY ma.message_id, ma.display_order
            """,
            message_ids,
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {message_id: [] for message_id in message_ids}
        for row in rows:
            item = self._artifact_dict(row)
            item["display_order"] = int(row["display_order"])
            item["status_label"] = "attachment deleted" if item["is_deleted"] else item["status"]
            grouped.setdefault(str(row["message_id"]), []).append(item)
        return grouped

    def hydrate_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped = self.attachments_for_messages([str(message["id"]) for message in messages])
        hydrated = []
        for message in messages:
            item = dict(message)
            item["artifacts"] = grouped.get(str(message["id"]), [])
            hydrated.append(item)
        return hydrated

    def create_crop(self, artifact_id: str, crop: dict[str, Any]) -> dict[str, Any]:
        from PIL import Image

        normalized = self._confined(Path(self.normalized_image_path(artifact_id)))
        x = float(crop.get("x") or 0)
        y = float(crop.get("y") or 0)
        width = float(crop.get("width") or 0)
        height = float(crop.get("height") or 0)
        if width <= 0 or height <= 0 or x < 0 or y < 0:
            raise ArtifactValidationError("crop coordinates must be positive and in bounds")
        with Image.open(normalized) as image:
            img_width, img_height = image.size
            left = round(x * img_width) if x <= 1 and width <= 1 else round(x)
            top = round(y * img_height) if y <= 1 and height <= 1 else round(y)
            crop_width = round(width * img_width) if x <= 1 and width <= 1 else round(width)
            crop_height = round(height * img_height) if y <= 1 and height <= 1 else round(height)
            if left < 0 or top < 0 or crop_width <= 0 or crop_height <= 0 or left + crop_width > img_width or top + crop_height > img_height:
                raise ArtifactValidationError("crop coordinates are outside the image")
            cropped = image.crop((left, top, left + crop_width, top + crop_height))
            derivation_id = str(uuid.uuid4())
            path = self.crops_dir / f"{derivation_id}.png"
            cropped.save(path, format="PNG")
        content_hash = file_sha256(path)
        derivation = self._insert_derivation(
            artifact_id,
            "crop",
            stored_path=str(self._confined(path)),
            content_hash=content_hash,
            producer_type="user",
            producer_name="crop-selection",
            metadata={
                "x": left,
                "y": top,
                "width": crop_width,
                "height": crop_height,
                "normalized_width": img_width,
                "normalized_height": img_height,
            },
            status="created",
        )
        return derivation

    def insert_text_derivation(
        self,
        artifact_id: str,
        kind: str,
        text: str,
        *,
        producer_type: str,
        producer_name: str,
        producer_version: str = "",
        producer_model: str = "",
        prompt_version: str = "",
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "created",
        error: str = "",
    ) -> dict[str, Any]:
        return self._insert_derivation(
            artifact_id,
            kind,
            text_content=text,
            content_hash=hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
            producer_type=producer_type,
            producer_name=producer_name,
            producer_version=producer_version,
            producer_model=producer_model,
            prompt_version=prompt_version,
            confidence=confidence,
            metadata=metadata or {},
            status=status,
            error=error,
        )

    def create_analysis_run(
        self,
        *,
        request_id: str,
        artifact_id: str,
        mode: str,
        user_question: str,
        requested_vision_backend: str = "",
        requested_vision_model: str = "",
        ocr_engine: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        existing = self.analysis_by_request(request_id)
        if existing:
            return existing
        now = utc_ms()
        run_id = str(uuid.uuid4())
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO artifact_analysis_runs(
                    id, request_id, artifact_id, message_id, mode, user_question,
                    requested_vision_backend, actual_vision_backend, requested_vision_model,
                    actual_vision_model, ocr_engine, prompt_version, status, stage,
                    warnings_json, error, output_json, evidence_json, timings_json,
                    created_at, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, '', ?, ?, 'running', 'created', '[]', '', '{}', '{}', '{}', ?, ?, NULL)
                """,
                (
                    run_id,
                    request_id,
                    artifact_id,
                    message_id,
                    mode,
                    user_question,
                    requested_vision_backend,
                    requested_vision_model,
                    ocr_engine,
                    ARTIFACT_PROMPT_VERSION,
                    now,
                    now,
                ),
            )
        return self.analysis(run_id)

    def update_analysis_run(self, run_id: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "stage",
            "actual_vision_backend",
            "actual_vision_model",
            "ocr_engine",
            "warnings_json",
            "error",
            "output_json",
            "evidence_json",
            "timings_json",
            "completed_at",
        }
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return self.analysis(run_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.db.conn.execute(f"UPDATE artifact_analysis_runs SET {assignments} WHERE id = ?", [*fields.values(), run_id])
        self.db.conn.commit()
        return self.analysis(run_id)

    def analysis(self, analysis_id: str) -> dict[str, Any]:
        row = self.db.conn.execute("SELECT * FROM artifact_analysis_runs WHERE id = ?", (analysis_id,)).fetchone()
        if row is None:
            raise KeyError(f"artifact analysis not found: {analysis_id}")
        return analysis_dict(row)

    def analysis_by_request(self, request_id: str) -> dict[str, Any] | None:
        if not request_id:
            return None
        row = self.db.conn.execute("SELECT * FROM artifact_analysis_runs WHERE request_id = ?", (request_id,)).fetchone()
        return analysis_dict(row) if row else None

    def latest_completed_analysis(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.db.conn.execute(
            """
            SELECT *
            FROM artifact_analysis_runs
            WHERE artifact_id = ?
              AND status IN ('completed', 'completed_with_warnings')
            ORDER BY COALESCE(completed_at, created_at) DESC, created_at DESC
            LIMIT 1
            """,
            (artifact_id,),
        ).fetchone()
        return analysis_dict(row) if row else None

    def recover_interrupted_analysis_runs(self) -> int:
        now = utc_ms()
        cur = self.db.conn.execute(
            """
            UPDATE artifact_analysis_runs
            SET status = 'interrupted', stage = 'startup_recovery',
                error = 'Analysis was interrupted before the sidecar restarted.',
                completed_at = ?
            WHERE status IN ('running', 'pending')
            """,
            (now,),
        )
        self.db.conn.commit()
        return int(cur.rowcount or 0)

    def index_derivation(self, artifact_id: str, derivation_id: str) -> dict[str, Any]:
        if self.documents is None or self.rag is None:
            raise ArtifactError("RAG indexing is unavailable")
        artifact = self.get(artifact_id)
        derivation = self._derivation(derivation_id)
        text = str(derivation.get("text_content") or "").strip()
        if not text:
            raise ArtifactValidationError("selected derivation has no text to index")
        existing = self.db.conn.execute(
            "SELECT document_id FROM artifact_rag_documents WHERE artifact_id = ? AND derivation_id = ?",
            (artifact_id, derivation_id),
        ).fetchone()
        if existing:
            return {"document": self.documents.get(existing["document_id"]), "mapping": dict(existing)}

        document_id = str(uuid.uuid4())
        now = utc_ms()
        label = source_label_for_derivation(artifact, derivation)
        stored_path = self.db.profile_dir / "files" / "documents" / f"{document_id}.artifact.txt"
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO documents(
                    id, title, source_path, stored_path, file_name, file_type, content_hash,
                    size_bytes, status, index_status, is_deleted, is_low_text, error,
                    created_at, updated_at, indexed_at, is_internal, source_artifact_id,
                    generated_source_label
                )
                VALUES (?, ?, '', ?, ?, 'txt', ?, ?, 'imported', 'pending', 0, 0, '',
                    ?, ?, NULL, 1, ?, ?)
                """,
                (
                    document_id,
                    label,
                    str(stored_path),
                    f"{artifact['name']} - {derivation['kind']}.txt",
                    digest,
                    len(text.encode("utf-8")),
                    now,
                    now,
                    artifact_id,
                    label,
                ),
            )
            self.db.conn.execute(
                """
                INSERT INTO document_pages(id, document_id, page_number, text, text_hash, extraction_method, metadata_json, created_at)
                VALUES (?, ?, 1, ?, ?, 'artifact_derivation', ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    document_id,
                    text,
                    digest,
                    json.dumps(
                        {
                            "source_kind": "artifact",
                            "artifact_id": artifact_id,
                            "artifact_name": artifact["name"],
                            "derivation_id": derivation_id,
                            "derivation_kind": derivation["kind"],
                            "producer": derivation.get("producer_name") or "",
                            "source_label": label,
                        }
                    ),
                    now,
                ),
            )
            self.db.conn.execute(
                """
                INSERT INTO artifact_rag_documents(artifact_id, document_id, derivation_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (artifact_id, document_id, derivation_id, now),
            )
        index = self.rag.index_document(document_id)
        return {"document": self.documents.get(document_id), "index": index}

    def unindex(self, artifact_id: str) -> dict[str, Any]:
        if self.rag is None:
            raise ArtifactError("RAG indexing is unavailable")
        rows = self.db.conn.execute(
            "SELECT document_id FROM artifact_rag_documents WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchall()
        deleted = []
        for row in rows:
            deleted.append(self.rag.delete_document(str(row["document_id"])))
        self.db.conn.execute("DELETE FROM artifact_rag_documents WHERE artifact_id = ?", (artifact_id,))
        self.db.conn.commit()
        return {"artifact_id": artifact_id, "deleted": deleted}

    def delete(self, artifact_id: str) -> dict[str, Any]:
        """User-facing artifact deletion (V04_STORAGE_CLEANUP_DESIGN.md §6).

        Hard-deletes derivation rows and owned files; the artifacts row is
        hard-deleted unless chat messages or analysis history reference it
        (both foreign keys are ON DELETE RESTRICT), in which case it becomes
        a tombstone with all derived data purged. Idempotent.
        """
        row = self.db.conn.execute(
            "SELECT id, is_deleted FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            return {
                "deleted": True,
                "already_deleted": True,
                "artifact_id": artifact_id,
                "tombstoned": False,
                "deleted_rows": 0,
                "files_removed": 0,
                "failed_files": 0,
                "bytes_reclaimed": 0,
            }
        already_deleted = bool(row["is_deleted"])
        purge = self._purge_artifact_data(artifact_id)
        return {
            "deleted": True,
            "already_deleted": already_deleted,
            "artifact_id": artifact_id,
            "tombstoned": purge["tombstoned"],
            "deleted_rows": purge["deleted_rows"],
            "files_removed": purge["files_removed"],
            "failed_files": purge["failed_files"],
            "bytes_reclaimed": purge["bytes_reclaimed"],
        }

    def purge_deleted_artifact(self, artifact_id: str) -> dict[str, Any]:
        """Purge derived rows and owned files of a soft-deleted artifact.

        Used by storage cleanup for legacy (pre-v0.4) soft deletes.
        """
        row = self.db.conn.execute(
            "SELECT id FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            return {
                "deleted_rows": 0,
                "row_deleted": False,
                "files_removed": 0,
                "failed_files": 0,
                "bytes_reclaimed": 0,
            }
        purge = self._purge_artifact_data(artifact_id)
        return {
            "deleted_rows": purge["deleted_rows"] + (0 if purge["tombstoned"] else 1),
            "row_deleted": not purge["tombstoned"],
            "files_removed": purge["files_removed"],
            "failed_files": purge["failed_files"],
            "bytes_reclaimed": purge["bytes_reclaimed"],
        }

    def _purge_artifact_data(self, artifact_id: str) -> dict[str, Any]:
        if self.rag is not None and self.documents is not None:
            self.unindex(artifact_id)
        stored_row = self.db.conn.execute(
            "SELECT stored_path FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        paths = [str(stored_row["stored_path"] or "") if stored_row else ""]
        paths.extend(
            str(derivation.get("stored_path") or "")
            for derivation in self.derivations(artifact_id)
        )
        conn = self.db.conn
        try:
            deleted_rows = conn.execute(
                "DELETE FROM artifact_derivations WHERE artifact_id = ?",
                (artifact_id,),
            ).rowcount
            refs = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM message_artifacts WHERE artifact_id = ?) +
                    (SELECT COUNT(*) FROM artifact_analysis_runs WHERE artifact_id = ?) AS n
                """,
                (artifact_id, artifact_id),
            ).fetchone()
            tombstoned = int(refs["n"] or 0) > 0
            if tombstoned:
                conn.execute(
                    "UPDATE artifacts SET is_deleted = 1, status = 'deleted', error = '', updated_at = ? WHERE id = ?",
                    (utc_ms(), artifact_id),
                )
            else:
                conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            logger.warning("artifact delete failed reason=db_error")
            raise RuntimeError("delete_failed") from None
        files_removed = 0
        failed_files = 0
        bytes_reclaimed = 0
        for value in paths:
            removed, size, failed = self._remove_confined_file(value)
            if removed:
                files_removed += 1
                bytes_reclaimed += size
            elif failed:
                failed_files += 1
        logger.info(
            "artifact deleted artifact_id=%s tombstoned=%s files_removed=%s failed_files=%s",
            artifact_id,
            tombstoned,
            files_removed,
            failed_files,
        )
        return {
            "tombstoned": tombstoned,
            "deleted_rows": max(0, deleted_rows),
            "files_removed": files_removed,
            "failed_files": failed_files,
            "bytes_reclaimed": bytes_reclaimed,
        }

    def _remove_confined_file(self, value: str) -> tuple[bool, int, bool]:
        """Delete one owned artifact file, fail closed.

        Returns (removed, bytes, failed). A missing file is neither removed
        nor failed. Symlinks/junctions and paths escaping the artifact root
        are refused and counted as failed.
        """
        if not value:
            return False, 0, False
        candidate = Path(value)
        try:
            candidate.lstat()
        except FileNotFoundError:
            return False, 0, False
        except OSError:
            logger.warning("artifact file removal refused reason=os_error")
            return False, 0, True
        link = is_link(candidate)
        if link is None or link:
            logger.warning("artifact file removal refused reason=symlink")
            return False, 0, True
        try:
            path = self._confined(candidate)
            if not path.is_file():
                return False, 0, False
            size = int(path.stat().st_size)
            path.unlink()
            return True, size, False
        except ArtifactPathError:
            logger.warning("artifact file removal refused reason=outside_profile")
            return False, 0, True
        except OSError:
            logger.warning("artifact file removal failed reason=os_error")
            return False, 0, True

    def promote(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.get(artifact_id)
        if artifact["is_deleted"]:
            raise ArtifactError("cannot promote a deleted artifact")
        now = utc_ms()
        self.db.conn.execute(
            """
            UPDATE artifacts
            SET scope = 'library',
                promoted_at = COALESCE(promoted_at, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, artifact_id),
        )
        self.db.conn.commit()
        return self.get(artifact_id)

    def diagnostics(self) -> dict[str, Any]:
        row = self.db.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM artifacts WHERE is_deleted = 0) AS artifact_count,
                (SELECT COUNT(*) FROM artifact_derivations) AS derivation_count,
                (SELECT COUNT(*) FROM artifact_derivations WHERE kind = 'vision_input') AS vision_derivative_count,
                (SELECT COUNT(*) FROM artifact_derivations WHERE kind = 'ocr_input') AS ocr_derivative_count,
                (SELECT COUNT(*) FROM artifact_analysis_runs WHERE status IN ('interrupted', 'error', 'timeout')) AS interrupted_or_error_count,
                (SELECT COUNT(*) FROM artifact_rag_documents) AS rag_source_count
            """
        ).fetchone()
        return {
            **self.limits(),
            "artifact_count": int(row["artifact_count"] or 0),
            "derivation_count": int(row["derivation_count"] or 0),
            "vision_derivative_count": int(row["vision_derivative_count"] or 0),
            "ocr_derivative_count": int(row["ocr_derivative_count"] or 0),
            "interrupted_or_error_analysis_count": int(row["interrupted_or_error_count"] or 0),
            "rag_source_count": int(row["rag_source_count"] or 0),
        }

    def _write_normalized(self, artifact_id: str, image: Any) -> tuple[Path, dict[str, Any]]:
        prepared = image.copy()
        prepared.thumbnail((NORMALIZED_MAX_EDGE, NORMALIZED_MAX_EDGE))
        path = self.normalized_dir / f"{artifact_id}.png"
        prepared.save(path, format="PNG")
        return self._confined(path), {"hash": file_sha256(path), "width": prepared.width, "height": prepared.height}

    def _write_thumbnail(self, artifact_id: str, image: Any) -> tuple[Path, dict[str, Any]]:
        prepared = image.copy()
        prepared.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE))
        path = self.thumbnails_dir / f"{artifact_id}.png"
        prepared.save(path, format="PNG")
        return self._confined(path), {"hash": file_sha256(path), "width": prepared.width, "height": prepared.height}

    def _insert_derivation(
        self,
        artifact_id: str,
        kind: str,
        *,
        stored_path: str = "",
        text_content: str = "",
        content_hash: str = "",
        producer_type: str = "",
        producer_name: str = "",
        producer_version: str = "",
        producer_model: str = "",
        prompt_version: str = "",
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "created",
        error: str = "",
        commit: bool = True,
    ) -> dict[str, Any]:
        derivation_id = str(uuid.uuid4())
        now = utc_ms()
        self.db.conn.execute(
            """
            INSERT INTO artifact_derivations(
                id, artifact_id, kind, stored_path, text_content, content_hash,
                producer_type, producer_name, producer_version, producer_model,
                prompt_version, confidence, metadata_json, status, error,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                derivation_id,
                artifact_id,
                kind,
                stored_path,
                text_content,
                content_hash,
                producer_type,
                producer_name,
                producer_version,
                producer_model,
                prompt_version,
                confidence,
                json.dumps(metadata or {}),
                status,
                error,
                now,
                now,
            ),
        )
        if commit:
            self.db.conn.commit()
        return self._derivation(derivation_id)

    def _derivation(self, derivation_id: str) -> dict[str, Any]:
        row = self.db.conn.execute("SELECT * FROM artifact_derivations WHERE id = ?", (derivation_id,)).fetchone()
        if row is None:
            raise KeyError(f"artifact derivation not found: {derivation_id}")
        return derivation_dict(row)

    def _confined(self, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        root = self.root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ArtifactPathError("artifact file operation escaped the profile artifact directory") from exc
        return resolved

    def _artifact_dict(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["is_deleted"] = bool(item["is_deleted"])
        item["normalized_orientation"] = bool(item["normalized_orientation"])
        item["original_has_alpha"] = bool(item.get("original_has_alpha", 0))
        item["scope"] = str(item.get("scope") or "library")
        item["source_path_redacted"] = bool(item.get("original_filename"))
        item["thumbnail_path"] = self.latest_derivation(str(item["id"]), "thumbnail").get("stored_path", "")
        item.pop("stored_path", None)
        return item


def decode_image(path: Path) -> dict[str, Any]:
    try:
        decoded = ImagePreprocessingService().decode(path)
    except ImagePreprocessingError as exc:
        raise ArtifactValidationError(str(exc)) from exc
    return {"image": decoded.image, **decoded.metadata()}


def derivation_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    return item


def analysis_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
    item["output"] = json.loads(item.pop("output_json") or "{}")
    item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
    item["timings"] = json.loads(item.pop("timings_json") or "{}")
    return item


def clean_display_name(value: str) -> str:
    cleaned = " ".join((value or "Image").replace("\x00", "").split()).strip()
    return cleaned[:96] or "Image"


def normalize_source_kind(value: str) -> str:
    allowed = {"file", "clipboard", "screenshot_full", "screenshot_window", "screenshot_region", "derived_crop"}
    cleaned = (value or "file").strip()
    if cleaned not in allowed:
        raise ArtifactValidationError(f"unsupported artifact source kind: {value}")
    return cleaned


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_label_for_derivation(artifact: dict[str, Any], derivation: dict[str, Any]) -> str:
    kind = str(derivation.get("kind") or "").replace("_", " ")
    if kind == "ocr text":
        suffix = "OCR text"
    elif "vision" in kind:
        suffix = "vision description"
    elif "combined" in kind:
        suffix = "combined evidence"
    else:
        suffix = kind or "derived text"
    return f"{artifact.get('name') or 'Image'} - {suffix}"
