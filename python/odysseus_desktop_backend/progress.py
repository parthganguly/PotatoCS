from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


PROGRESS_DISCRIMINATOR = "__odysseus_progress__"
STRICT_TRACE_ENV_VAR = "ODYSSEUS_STRICT_TRACE"
_SAFE_STATUSES = {"running", "completed", "error"}
_SAFE_CACHE_STATUSES = {"reused", "fresh", "miss", "not_applicable"}
# operation_id is a caller-chosen tag (not a DB row id), so it keeps a broad shape check.
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
# session_id/message_id/artifact_id/source_id always reference real DB rows, which are
# always uuid4 strings at every current call site — validate the shape strictly.
_TRACE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_FIXED_LABELS = {
    "operation_failed": "Operation failed.",
    "done": "Done",
    "model_capability_check": "Checking model capabilities...",
    "image_prepare": "Preparing image...",
    "ocr_run": "Running OCR...",
    "florence_load": "Loading Florence...",
    "local_vision": "Running local vision...",
    "visual_evidence_build": "Building visual evidence...",
    "visual_evidence_reuse": "Reusing visual evidence...",
    "visual_evidence_retrieval": "Retrieving relevant snippets...",
    "rag_search": "Searching sources...",
    "answer_generation": "Generating answer...",
    "answer_verification": "Verifying answer...",
    "pdf_render": "Rendering PDF pages...",
    "crop_ocr": "Running crop OCR...",
    "source_import": "Importing source...",
    "source_index": "Indexing source...",
}


class TraceIdentifierError(ValueError):
    """Raised in strict-trace mode when a progress identifier is not UUID-shaped."""


def _strict_trace_enabled() -> bool:
    return os.environ.get(STRICT_TRACE_ENV_VAR) == "1"


def _safe_identifier(value: object) -> str | None:
    text = str(value or "").strip()
    return text if text and _OPERATION_ID_RE.fullmatch(text) else None


def _safe_trace_uuid(value: object, field: str) -> str | None:
    """Validate a DB-row identifier field (session/message/artifact/source id).

    Defaults to dropping non-UUID-shaped values to None so a bad binding can never
    leak arbitrary text (e.g. a filename or path) into a trace event. Under
    ODYSSEUS_STRICT_TRACE=1 it raises instead, so tests/dev catch a call site that
    accidentally binds a non-UUID string. The exception message never includes the
    offending value, only the field name.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if _TRACE_UUID_RE.fullmatch(text):
        return text
    if _strict_trace_enabled():
        raise TraceIdentifierError(f"{field} must be a UUID-shaped trace identifier")
    return None


def _safe_count(value: object) -> int | None:
    if value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if 0 <= count <= 1_000_000 else None


def progress_label(stage: str, current: int | None = None, total: int | None = None) -> str:
    if stage == "ocr_page":
        if current is not None and total is not None:
            return f"Running OCR page {current}/{total}..."
        if current is not None:
            return f"Running OCR page {current}..."
        return "Running OCR..."
    if stage == "rag_retrieved":
        return f"Retrieved {current or 0} snippets..."
    return _FIXED_LABELS.get(stage, "Working...")


class ProgressEmitter:
    """Write privacy-safe progress events without affecting the main operation."""

    def __init__(
        self,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
        message_id: str | None = None,
        artifact_id: str | None = None,
        source_id: str | None = None,
    ) -> None:
        self.started_at = int(time.time() * 1000)
        self._started_monotonic = time.perf_counter()
        self.operation_id = _safe_identifier(operation_id) or f"operation-{uuid.uuid4()}"
        self.session_id = _safe_trace_uuid(session_id, "session_id")
        self.message_id = _safe_trace_uuid(message_id, "message_id")
        self.artifact_id = _safe_trace_uuid(artifact_id, "artifact_id")
        self.source_id = _safe_trace_uuid(source_id, "source_id")

    def bind(
        self,
        *,
        session_id: str | None = None,
        message_id: str | None = None,
        artifact_id: str | None = None,
        source_id: str | None = None,
    ) -> None:
        try:
            if session_id is not None:
                self.session_id = _safe_trace_uuid(session_id, "session_id")
            if message_id is not None:
                self.message_id = _safe_trace_uuid(message_id, "message_id")
            if artifact_id is not None:
                self.artifact_id = _safe_trace_uuid(artifact_id, "artifact_id")
            if source_id is not None:
                self.source_id = _safe_trace_uuid(source_id, "source_id")
        except TraceIdentifierError:
            raise
        except Exception:
            return

    def emit(
        self,
        stage: str,
        *,
        status: str = "running",
        progress_current: int | None = None,
        progress_total: int | None = None,
        cache_status: str | None = None,
        detail: object | None = None,
    ) -> dict[str, Any] | None:
        del detail
        try:
            clean_stage = str(stage or "").strip()
            current = _safe_count(progress_current)
            total = _safe_count(progress_total)
            payload: dict[str, Any] = {
                PROGRESS_DISCRIMINATOR: True,
                "operation_id": self.operation_id,
                "session_id": self.session_id,
                "message_id": self.message_id,
                "artifact_id": self.artifact_id,
                "source_id": self.source_id,
                "stage": clean_stage if clean_stage in _FIXED_LABELS or clean_stage in {"ocr_page", "rag_retrieved"} else "working",
                "label": progress_label(clean_stage, current, total),
                "status": status if status in _SAFE_STATUSES else "running",
                "started_at": self.started_at,
                "elapsed_ms": max(0, int((time.perf_counter() - self._started_monotonic) * 1000)),
                "progress_current": current,
                "progress_total": total,
                "cache_status": cache_status if cache_status in _SAFE_CACHE_STATUSES else None,
                "detail": None,
            }
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), file=sys.stderr, flush=True)
            return payload
        except Exception:
            return None


_CURRENT_EMITTER: ContextVar[ProgressEmitter | None] = ContextVar("odysseus_progress_emitter", default=None)


@contextmanager
def progress_operation(**context: object) -> Iterator[ProgressEmitter]:
    emitter = ProgressEmitter(**context)
    token = _CURRENT_EMITTER.set(emitter)
    try:
        yield emitter
    except Exception:
        emitter.emit("operation_failed", status="error")
        raise
    else:
        emitter.emit("done", status="completed")
    finally:
        _CURRENT_EMITTER.reset(token)


def emit_progress(stage: str, **values: object) -> dict[str, Any] | None:
    emitter = _CURRENT_EMITTER.get()
    return emitter.emit(stage, **values) if emitter is not None else None


def bind_progress_context(**context: object) -> None:
    emitter = _CURRENT_EMITTER.get()
    if emitter is not None:
        emitter.bind(**context)
