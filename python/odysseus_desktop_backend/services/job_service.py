from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from odysseus_desktop_backend.cancellation import (
    JobCancelledError,
    cancellation_scope,
)
from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.progress import progress_operation
from odysseus_desktop_backend.storage import utc_ms


logger = get_logger("jobs")

TERMINAL_JOB_STATES = {"cancelled", "completed", "failed"}
JOB_STATES = {"queued", "preflighting", "running", "cancel_requested"} | TERMINAL_JOB_STATES
# Legal transitions per V04_ESSENTIAL_SEMANTICS.md §A. `cancel_requested` may
# still reach `completed`: a cancel that lands after the final commit boundary
# must never un-commit work.
_LEGAL_TRANSITIONS = {
    "queued": {"preflighting", "cancel_requested"},
    "preflighting": {"running", "failed", "cancel_requested"},
    "running": {"completed", "failed", "cancel_requested"},
    "cancel_requested": {"cancelled", "completed", "failed"},
}
# Structural safety ceilings — bounded by design, NOT measured Potato Mode
# defaults (Issue #14/#20 own tuning; see V04_ESSENTIAL_SEMANTICS.md §F).
MAX_QUEUED_JOBS = 32
MAX_RETAINED_TERMINAL_JOBS = 100
SHUTDOWN_GRACE_SECONDS = 2.0
# Bounded cancel-first wait before a Source deletion gives up with a fixed
# "busy" failure (V04_ESSENTIAL_SEMANTICS.md §D; V04_STORAGE_CLEANUP_DESIGN.md §8).
DELETE_CANCEL_WAIT_SECONDS = 5.0

# Fixed message codes; the frontend maps these to fixed plain-language copy.
# Job payloads never carry raw errors, paths, filenames, or document text.
JOB_MESSAGE_CODES = {
    "",
    "cancelled_by_user",
    "job_failed",
    "file_not_found",
    "unsupported_file_type",
    "document_not_found",
    "document_busy",
    "ocr_unavailable",
    "ocr_no_text",
    "ocr_page_too_large",
    "ocr_too_many_pages",
}


class JobFailure(Exception):
    """Fail the current job with a fixed message code (never raw error text)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code if code in JOB_MESSAGE_CODES else "job_failed"


@dataclass
class JobRecord:
    id: str
    kind: str
    path: str = ""  # private input; never serialized into snapshots or logs
    scope: str = "library"
    document_id: str = ""
    artifact_id: str = ""
    state: str = "queued"
    message_code: str = ""
    created_at: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class JobExecutor(Protocol):
    """Domain work for one job. Runs on the worker thread inside an active
    cancellation scope; must raise JobCancelledError (via check_cancelled) at
    safe points and JobFailure for expected failures."""

    def run(self, job: JobRecord, on_running: Callable[[], None]) -> None: ...

    def rollback(self, job: JobRecord) -> None: ...

    def close(self) -> None: ...


ExecutorFactory = Callable[[], JobExecutor]


class JobService:
    """Single-worker FIFO registry/queue for cancellable heavy jobs.

    Registry, queue, and the state machine only — domain work is delegated to
    an executor created per job. The registry is in-memory by design: restart
    never resurrects queued or running work (semantics contract §A rule 7).
    Partial staged state left by a dead process is removed at startup by
    DocumentService.repair_startup_state().
    """

    def __init__(self, profile_dir: str | Path, executor_factory: ExecutorFactory | None = None):
        self.profile_dir = Path(profile_dir)
        self._executor_factory = executor_factory or (
            lambda: DocumentJobExecutor(self.profile_dir)
        )
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._queue: deque[str] = deque()
        self._worker: threading.Thread | None = None
        self._shutdown = False

    # ------------------------------------------------------------------ API

    def submit_import(self, paths: list[str], *, scope: str = "library") -> list[dict[str, Any]]:
        clean_paths = [str(path) for path in paths if str(path).strip()]
        if not clean_paths:
            raise ValueError("at least one path is required")
        snapshots: list[dict[str, Any]] = []
        with self._condition:
            self._require_capacity_locked(len(clean_paths))
            for path in clean_paths:
                job = JobRecord(
                    id=str(uuid.uuid4()),
                    kind="import",
                    path=path,
                    scope=scope,
                    created_at=utc_ms(),
                )
                self._register_locked(job)
                snapshots.append(self._snapshot_locked(job))
            self._condition.notify_all()
        logger.info("jobs submitted kind=import count=%s", len(snapshots))
        return snapshots

    def submit_ocr(self, document_id: str) -> dict[str, Any]:
        clean_id = str(document_id or "").strip()
        if not clean_id:
            raise ValueError("document_id is required")
        with self._condition:
            self._require_capacity_locked(1)
            for existing in self._jobs.values():
                if existing.state not in TERMINAL_JOB_STATES and existing.document_id == clean_id:
                    raise ValueError("a job is already running for this source")
            job = JobRecord(
                id=str(uuid.uuid4()),
                kind="ocr",
                document_id=clean_id,
                created_at=utc_ms(),
            )
            self._register_locked(job)
            self._condition.notify_all()
            snapshot = self._snapshot_locked(job)
        logger.info("jobs submitted kind=ocr count=1")
        return snapshot

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            return self._snapshot_locked(job)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._snapshot_locked(self._jobs[job_id]) for job_id in self._order]

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Idempotent cooperative cancel. Terminal jobs are returned unchanged."""
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            if job.state in TERMINAL_JOB_STATES:
                return self._snapshot_locked(job)
            job.cancel_event.set()
            if job.state != "cancel_requested":
                self._set_state_locked(job, "cancel_requested")
            return self._snapshot_locked(job)

    def active_document_ids(self) -> set[str]:
        with self._lock:
            return {
                job.document_id
                for job in self._jobs.values()
                if job.state not in TERMINAL_JOB_STATES and job.document_id
            }

    def has_active_jobs(self) -> bool:
        with self._lock:
            return any(job.state not in TERMINAL_JOB_STATES for job in self._jobs.values())

    def release_source(
        self,
        *,
        document_id: str = "",
        artifact_id: str = "",
        timeout: float = DELETE_CANCEL_WAIT_SECONDS,
    ) -> bool:
        """Cancel-first, bounded-wait release of a source held by active jobs.

        Requests cooperative cancel on every non-terminal job owning the
        source, then waits up to `timeout` for them to reach a terminal
        state. Returns True when no active job holds the source; False means
        the caller must fail with a fixed "busy" message and stay retryable.
        """

        def _matching_locked() -> list[JobRecord]:
            return [
                job
                for job in self._jobs.values()
                if job.state not in TERMINAL_JOB_STATES
                and (
                    (document_id and job.document_id == document_id)
                    or (artifact_id and job.artifact_id == artifact_id)
                )
            ]

        if not document_id and not artifact_id:
            return True
        with self._lock:
            matching = _matching_locked()
            if not matching:
                return True
            for job in matching:
                job.cancel_event.set()
                if job.state != "cancel_requested":
                    self._set_state_locked(job, "cancel_requested")
        logger.info("source release requested cancelled_jobs=%s", len(matching))
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if not _matching_locked():
                    return True
            time.sleep(0.05)
        with self._lock:
            return not _matching_locked()

    def shutdown(self, timeout: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Request cancel on every non-terminal job and wait briefly.

        The worker is a daemon thread: if a job cannot reach a safe point
        within the grace period (e.g. an OCR subprocess mid-run), process
        exit still proceeds and startup repair removes any staged remains.
        """
        with self._condition:
            self._shutdown = True
            for job in self._jobs.values():
                if job.state not in TERMINAL_JOB_STATES:
                    job.cancel_event.set()
                    if job.state != "cancel_requested":
                        self._set_state_locked(job, "cancel_requested")
            self._condition.notify_all()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)

    # ------------------------------------------------------------- internals

    def _require_capacity_locked(self, new_jobs: int) -> None:
        if self._shutdown:
            raise ValueError("job queue is shutting down")
        if len(self._queue) + new_jobs > MAX_QUEUED_JOBS:
            raise ValueError("job queue is full; wait for current jobs to finish")

    def _register_locked(self, job: JobRecord) -> None:
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._queue.append(job.id)
        self._ensure_worker_locked()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="odysseus-job-worker",
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._shutdown:
                    self._condition.wait()
                if not self._queue and self._shutdown:
                    return
                job = self._jobs[self._queue.popleft()]
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001 - worker must survive any job
                logger.error(
                    "job worker unexpected error kind=%s error_type=%s",
                    job.kind,
                    type(exc).__name__,
                )
                self._finalize(job, "failed", "job_failed")

    def _run_job(self, job: JobRecord) -> None:
        if job.cancel_event.is_set():
            self._finalize(job, "cancelled", "cancelled_by_user")
            return
        with self._lock:
            job.started_at = utc_ms()
            if job.state == "queued":
                self._set_state_locked(job, "preflighting")
        executor: JobExecutor | None = None
        try:
            executor = self._executor_factory()
            try:
                with cancellation_scope(job.cancel_event), progress_operation(
                    operation_id=f"{job.kind}-job-{job.id}"
                ):
                    executor.run(job, lambda: self._set_state_if_active(job, "running"))
                self._finalize(job, "completed", "")
            except JobCancelledError:
                self._rollback(executor, job)
                self._finalize(job, "cancelled", "cancelled_by_user")
            except JobFailure as failure:
                self._rollback(executor, job)
                self._finalize(job, "failed", failure.code)
            except Exception as exc:  # noqa: BLE001 - job errors become fixed-code failures
                self._rollback(executor, job)
                logger.warning(
                    "job failed kind=%s error_type=%s", job.kind, type(exc).__name__
                )
                self._finalize(job, "failed", "job_failed")
        finally:
            if executor is not None:
                try:
                    executor.close()
                except Exception as exc:  # noqa: BLE001 - close must not mask the job outcome
                    logger.error(
                        "job executor close failed kind=%s error_type=%s",
                        job.kind,
                        type(exc).__name__,
                    )

    def _rollback(self, executor: JobExecutor | None, job: JobRecord) -> None:
        if executor is None:
            return
        # Rollback runs outside the cancellation scope (the `with` block has
        # exited), so check_cancelled() inside cleanup code is a no-op here.
        try:
            executor.rollback(job)
        except Exception as exc:  # noqa: BLE001 - rollback failure must not mask outcome
            logger.error(
                "job rollback failed kind=%s error_type=%s", job.kind, type(exc).__name__
            )

    def _set_state_if_active(self, job: JobRecord, state: str) -> None:
        with self._lock:
            if job.state in TERMINAL_JOB_STATES or job.state == "cancel_requested":
                return
            self._set_state_locked(job, state)

    def _set_state_locked(self, job: JobRecord, state: str) -> None:
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state: {state}")
        allowed = _LEGAL_TRANSITIONS.get(job.state, set())
        if state not in allowed:
            raise ValueError(f"illegal job transition: {job.state} -> {state}")
        job.state = state

    def _finalize(self, job: JobRecord, state: str, message_code: str) -> None:
        with self._lock:
            if job.state in TERMINAL_JOB_STATES:
                return
            self._set_state_locked(job, state)
            job.message_code = message_code if message_code in JOB_MESSAGE_CODES else "job_failed"
            job.finished_at = utc_ms()
            self._prune_locked()
        logger.info(
            "job finished kind=%s state=%s code=%s",
            job.kind,
            state,
            job.message_code or "none",
        )

    def _prune_locked(self) -> None:
        terminal_ids = [
            job_id
            for job_id in self._order
            if self._jobs[job_id].state in TERMINAL_JOB_STATES
        ]
        excess = len(terminal_ids) - MAX_RETAINED_TERMINAL_JOBS
        for job_id in terminal_ids[: max(0, excess)]:
            self._jobs.pop(job_id, None)
            self._order.remove(job_id)

    def _snapshot_locked(self, job: JobRecord) -> dict[str, Any]:
        queue_position = None
        if job.state in {"queued", "cancel_requested"} and job.started_at is None:
            try:
                queue_position = list(self._queue).index(job.id) + 1
            except ValueError:
                queue_position = None
        end = job.finished_at or utc_ms()
        elapsed_ms = max(0, end - job.started_at) if job.started_at else 0
        return {
            "job_id": job.id,
            "kind": job.kind,
            "state": job.state,
            "message_code": job.message_code,
            "scope": job.scope,
            "document_id": job.document_id,
            "artifact_id": job.artifact_id,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_ms": elapsed_ms,
            "queue_position": queue_position,
        }


class DocumentJobExecutor:
    """Real import/OCR work for one job, on its own Database connection.

    Created per job on the worker thread (same pattern as the campaign
    worker); WAL + busy_timeout handle write contention with the main RPC
    thread. Lazy service imports keep JobService importable in isolation.
    """

    def __init__(self, profile_dir: Path):
        from odysseus_desktop_backend.storage import Database

        self.profile_dir = profile_dir
        self.db = Database(profile_dir)
        self.services = self._build_services()
        self._prior_document_fields: dict[str, Any] = {}

    def run(self, job: JobRecord, on_running: Callable[[], None]) -> None:
        if job.kind == "import":
            self._run_import(job, on_running)
        elif job.kind == "ocr":
            self._run_ocr(job, on_running)
        else:
            raise JobFailure("job_failed")

    def rollback(self, job: JobRecord) -> None:
        if job.kind == "import" and job.document_id:
            self.services.documents.purge_document(job.document_id)
        elif job.kind == "ocr" and job.document_id and self._prior_document_fields:
            # The OCR commit sequence is shielded from cancellation and no
            # destructive DB write happens before it, so restoring the status
            # fields rolls a cancelled OCR job fully back. After a mid-commit
            # *failure* this leaves ocr_status='needed' with partial OCR rows
            # present, which a later OCR run replaces idempotently.
            self.services.documents.restore_status_fields(
                job.document_id, self._prior_document_fields
            )

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- internals

    def _run_import(self, job: JobRecord, on_running: Callable[[], None]) -> None:
        from odysseus_desktop_backend.cancellation import check_cancelled
        from odysseus_desktop_backend.services.artifact_service import (
            SUPPORTED_IMAGE_EXTENSIONS,
        )
        from odysseus_desktop_backend.services.document_service import (
            SUPPORTED_EXTENSIONS,
        )

        check_cancelled()
        path = Path(job.path)
        if not path.exists() or not path.is_file():
            raise JobFailure("file_not_found")
        extension = path.suffix.lower()
        if extension in SUPPORTED_IMAGE_EXTENSIONS:
            # Image imports are fast and have no staged intermediate state;
            # cancel is honored only before the copy starts.
            on_running()
            artifact = self.services.artifacts.import_path(
                job.path, source_kind="file", scope=job.scope
            )
            job.artifact_id = str(artifact["id"])
            return
        if extension not in SUPPORTED_EXTENSIONS:
            raise JobFailure("unsupported_file_type")

        document = self.services.documents.import_document(
            job.path, scope=job.scope, staging=True
        )
        job.document_id = str(document["id"])
        on_running()
        check_cancelled()
        self.services.sources.index_document(job.document_id)
        # Final commit boundary: last cancellation check before the source
        # becomes visible. After commit_staging, cancel resolves as completed.
        check_cancelled()
        self.services.documents.commit_staging(job.document_id)

    def _run_ocr(self, job: JobRecord, on_running: Callable[[], None]) -> None:
        from odysseus_desktop_backend.cancellation import check_cancelled
        from odysseus_desktop_backend.services.ocr_service import OCRGuardrailError

        check_cancelled()
        try:
            document = self.services.documents.get(job.document_id)
        except KeyError as exc:
            raise JobFailure("document_not_found") from exc
        if str(document.get("file_type") or "") != "pdf":
            raise JobFailure("unsupported_file_type")
        self._prior_document_fields = {
            "status": str(document.get("status") or ""),
            "index_status": str(document.get("index_status") or ""),
            "ocr_status": str(document.get("ocr_status") or ""),
            "ocr_engine": str(document.get("ocr_engine") or ""),
            "ocr_error": str(document.get("ocr_error") or ""),
            "error": str(document.get("error") or ""),
        }
        on_running()
        check_cancelled()
        try:
            self.services.ocr.run_document_ocr(job.document_id)
        except OCRGuardrailError as exc:
            raise JobFailure(exc.code) from exc

    def _build_services(self) -> Any:
        from types import SimpleNamespace

        from odysseus_desktop_backend.services.artifact_service import ArtifactService
        from odysseus_desktop_backend.services.document_service import DocumentService
        from odysseus_desktop_backend.services.embedding_service import EmbeddingService
        from odysseus_desktop_backend.services.model_service import ModelService
        from odysseus_desktop_backend.services.ocr_service import (
            LocalVLMTextExtractor,
            OCRService,
        )
        from odysseus_desktop_backend.services.rag_service import RAGService
        from odysseus_desktop_backend.services.source_service import SourceService
        from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore

        documents = DocumentService(self.db)
        embeddings = EmbeddingService(self.db)
        vector_store = SQLiteNumPyVectorStore(self.db)
        rag = RAGService(documents, embeddings, vector_store)
        ocr = OCRService(documents, rag)
        # Florence is deliberately not wired into the job worker (its thread
        # safety is unproven); the Ollama VLM text fallback is preserved.
        ocr.set_vlm_text_extractor(LocalVLMTextExtractor(ModelService(self.db), None))
        artifacts = ArtifactService(self.db, documents, rag)
        sources = SourceService(documents, artifacts, rag, ocr=ocr)
        return SimpleNamespace(
            documents=documents,
            embeddings=embeddings,
            rag=rag,
            ocr=ocr,
            artifacts=artifacts,
            sources=sources,
        )
