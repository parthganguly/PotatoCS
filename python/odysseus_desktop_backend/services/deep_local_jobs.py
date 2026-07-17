from __future__ import annotations

import json
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.cancellation import (
    JobCancelledError,
    cancellation_scope,
    check_cancelled,
)
from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.progress import progress_operation
from odysseus_desktop_backend.services.providers.base import (
    ModelInterruptedError,
    ModelQueueSaturatedError,
    ModelServiceError,
)
from odysseus_desktop_backend.services.providers.colibri import (
    COLIBRI_API_KEY_ENV,
    DEFAULT_COLIBRI_ENDPOINT,
    DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS,
    ColibriProvider,
    RequestCancelHandle,
)
from odysseus_desktop_backend.storage import Database, utc_ms


logger = get_logger("deep_local_jobs")

# Persisted Deep Local job states (V04 heavy-job semantics extended for slow,
# restart-surviving inference; see COLIBRI_PROVIDER_RFC.md §6/§8).
TERMINAL_STATES = {"completed", "failed", "cancelled_before_start", "interrupted"}
JOB_STATES = {
    "queued",
    "checking_runtime",
    "waiting_for_provider",
    "running",
    "cancel_requested",
} | TERMINAL_STATES
# `running` means "the completion request is in flight"; with a non-streaming
# server we cannot distinguish in-server queueing from generation, so
# `waiting_for_provider` is entered only for client-side 429 backoff.
# `cancel_requested` may still reach `completed`: a cancel that lands after the
# response arrived must never un-report finished work.
_LEGAL_TRANSITIONS = {
    "queued": {"checking_runtime", "cancel_requested"},
    "checking_runtime": {"waiting_for_provider", "running", "failed", "cancel_requested"},
    "waiting_for_provider": {"running", "failed", "cancel_requested"},
    "running": {"waiting_for_provider", "completed", "failed", "interrupted", "cancel_requested"},
    "cancel_requested": {"cancelled_before_start", "interrupted", "completed", "failed"},
}

# Fixed message codes; the frontend maps these to fixed plain-language copy.
# Payloads and logs never carry prompts, evidence text, results, paths, or keys.
MESSAGE_CODES = {
    "",
    "cancelled_before_start",
    "stopped_waiting",
    "interrupted_by_restart",
    "deep_local_failed",
}

# Structural safety ceilings — bounded by design, NOT measured Potato Mode
# defaults (deliberately generous; evidence stays far below the server's 4 MB
# request-body cap).
MAX_QUEUED_JOBS = 8
MAX_RETAINED_TERMINAL_JOBS = 100
MAX_QUESTION_CHARS = 8_000
MAX_EVIDENCE_ITEMS = 32
MAX_SNIPPET_CHARS = 4_000
MAX_EVIDENCE_TOTAL_CHARS = 64_000
MAX_SOURCE_ID_CHARS = 128
MAX_OUTPUT_TOKENS_CEILING = 4_096
DEFAULT_MAX_OUTPUT_TOKENS = 512
DEFAULT_QUEUE_WAIT_SECONDS = 600.0
QUEUE_RETRY_FALLBACK_SECONDS = 15.0
SHUTDOWN_GRACE_SECONDS = 2.0

_SYSTEM_PROMPT = (
    "You are Deep Local, an offline assistant running entirely on this "
    "computer. Answer the question using only the provided source excerpts. "
    "Cite excerpts as [S1], [S2], ... If the excerpts do not contain the "
    "answer, say that plainly."
)


@dataclass
class DeepLocalJobRecord:
    """In-memory execution record for a live job.

    `question`/`evidence`/`params` are private inputs: they are persisted to
    the profile database and returned by user-initiated `get` calls, but are
    never serialized into list snapshots, logs, traces, or diagnostics.
    """

    id: str
    state: str = "queued"
    message_code: str = ""
    error_category: str = ""
    endpoint: str = ""
    model_id: str = ""
    question: str = ""
    evidence: list[dict[str, str]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    attempt_count: int = 1
    retry_of: str = ""
    created_at: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    state_history: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    result_text: str = ""
    thinking_text: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_handle: RequestCancelHandle | None = None
    request_started: bool = False


class DeepLocalJobService:
    """Persisted, single-worker FIFO runner for Deep Local inference jobs.

    Built on the v0.4 heavy-job substrate: the same cooperative-cancellation
    primitive (`cancellation.py`), the same single daemon-worker FIFO
    discipline, and the same fixed-code snapshot rules as `JobService`. It is
    a separate worker *instance* (not a second framework) because an
    hours-long generation must never queue document imports behind it, and
    one worker inherently enforces the one-generation-at-a-time reality of
    `coli serve`.

    Unlike `JobService`, rows are persisted before inference begins, so a
    restart can honestly repair in-flight work to `interrupted` instead of
    silently forgetting it.
    """

    def __init__(self, profile_dir: str | Path, db: Database):
        self.profile_dir = Path(profile_dir)
        # RPC-thread connection (submit/get/list/cancel/retry + repair). The
        # worker thread opens its own Database; WAL + busy_timeout handle the
        # cross-connection contention, and self._condition's lock serializes
        # every state transition in-process.
        self._db = db
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._records: dict[str, DeepLocalJobRecord] = {}
        self._queue: deque[str] = deque()
        self._worker: threading.Thread | None = None
        self._shutdown = False

    # ------------------------------------------------------------------ API

    def repair_startup_state(self) -> int:
        """Mark every non-terminal persisted job `interrupted`.

        Called once at sidecar startup before any submit. In-flight rows from
        a dead process cannot be resumed safely (the HTTP request is gone and
        auto-restarting hours of inference at boot would be silent heavy
        work), so they become `interrupted` with retry offered.
        """
        now = utc_ms()
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        rows = self._db.conn.execute(
            f"SELECT id, state_history_json FROM deep_local_jobs WHERE state NOT IN ({placeholders})",
            tuple(TERMINAL_STATES),
        ).fetchall()
        for row in rows:
            # The persisted audit trail must match the repaired state: append
            # the interrupted transition rather than leaving the history
            # ending on the pre-crash state.
            history = _load_json(row["state_history_json"], [])
            history.append({"state": "interrupted", "at": now})
            self._db.conn.execute(
                """
                UPDATE deep_local_jobs
                SET state = 'interrupted',
                    message_code = 'interrupted_by_restart',
                    error_category = 'interrupted',
                    state_history_json = ?,
                    finished_at = COALESCE(finished_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(history), now, now, str(row["id"])),
            )
        self._db.conn.commit()
        repaired = len(rows)
        if repaired:
            logger.warning("deep_local repaired interrupted jobs count=%s", repaired)
        return repaired

    def submit(
        self,
        *,
        question: str,
        evidence: list[dict[str, Any]] | None = None,
        model: str = "",
        request_id: str = "",
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        top_p: float | None = None,
        thinking: str = "off",
    ) -> dict[str, Any]:
        clean_question = str(question or "").strip()
        if not clean_question:
            raise ValueError("question is required")
        if len(clean_question) > MAX_QUESTION_CHARS:
            raise ValueError(f"question is longer than {MAX_QUESTION_CHARS} characters")
        clean_evidence = self._validate_evidence(evidence)
        if not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        if max_output_tokens > MAX_OUTPUT_TOKENS_CEILING:
            raise ValueError(f"max_output_tokens must be at most {MAX_OUTPUT_TOKENS_CEILING}")
        normalized_thinking = str(thinking or "off").strip().lower()
        if normalized_thinking not in {"auto", "off", "on"}:
            raise ValueError("thinking mode must be one of: auto, off, on")
        clean_request_id = str(request_id or "").strip()

        config = self._read_config(self._db)
        if not config["enabled"]:
            return _structured_error(
                "disabled",
                "Deep Local (experimental) is not enabled. It is optional, "
                "text-only, and requires a Colibri server that you run yourself.",
            )
        if not config["endpoint_is_loopback"]:
            return _structured_error(
                "disabled",
                "Deep Local endpoints must stay on this computer (127.0.0.1). "
                "Remote endpoints are not supported.",
            )

        with self._condition:
            if clean_request_id:
                existing = self._find_by_request_id(clean_request_id)
                if existing is not None:
                    return {"ok": True, "job": existing, "duplicate": True}
            if self._shutdown:
                raise ValueError("Deep Local job queue is shutting down")
            if self._active_count_locked() + 1 > MAX_QUEUED_JOBS:
                raise ValueError("Deep Local job queue is full; wait for current jobs to finish")
            record = DeepLocalJobRecord(
                id=str(uuid.uuid4()),
                endpoint=config["endpoint"],
                model_id=str(model or "").strip(),
                question=clean_question,
                evidence=clean_evidence,
                params={
                    "max_output_tokens": max_output_tokens,
                    "temperature": float(temperature),
                    "top_p": float(top_p) if top_p is not None else None,
                    "thinking": normalized_thinking,
                },
                request_id=clean_request_id,
                created_at=utc_ms(),
            )
            record.state_history.append({"state": "queued", "at": record.created_at})
            # Persist before inference: the row exists (and survives restart)
            # before the worker is even notified.
            self._insert_row(self._db, record)
            self._records[record.id] = record
            self._queue.append(record.id)
            self._ensure_worker_locked()
            self._condition.notify_all()
            snapshot = self._snapshot_locked(record)
        logger.info(
            "deep_local job submitted evidence_count=%s question_chars=%s",
            len(clean_evidence),
            len(clean_question),
        )
        return {"ok": True, "job": snapshot, "duplicate": False}

    def get(self, job_id: str) -> dict[str, Any]:
        """Full job view including content (user-initiated read of own data)."""
        clean_id = str(job_id or "").strip()
        with self._lock:
            record = self._records.get(clean_id)
            if record is not None:
                snapshot = self._snapshot_locked(record)
                snapshot.update(
                    question=record.question,
                    evidence=[dict(item) for item in record.evidence],
                    result_text=record.result_text,
                    thinking_text=record.thinking_text,
                )
                return snapshot
        row = self._fetch_row(clean_id)
        if row is None:
            raise KeyError(f"deep local job not found: {job_id}")
        snapshot = self._row_snapshot(row)
        snapshot.update(
            question=str(row["question"]),
            evidence=_load_json(row["evidence_json"], []),
            result_text=str(row["result_text"]),
            thinking_text=str(row["thinking_text"]),
        )
        return snapshot

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Light snapshots, newest first. Content fields are excluded; use
        `get` for the question/evidence/result of one job."""
        clean_limit = max(1, min(int(limit), 200))
        with self._lock:
            live = {job_id: self._snapshot_locked(record) for job_id, record in self._records.items()}
        rows = self._db.conn.execute(
            "SELECT * FROM deep_local_jobs ORDER BY created_at DESC, id LIMIT ?",
            (clean_limit,),
        ).fetchall()
        snapshots = []
        for row in rows:
            snapshot = live.get(str(row["id"])) or self._row_snapshot(row)
            snapshots.append(snapshot)
        return snapshots

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Idempotent cooperative cancel.

        Before the completion request is sent the job can still become
        `cancelled_before_start`. Once the request is in flight, cancelling
        only stops PotatoCs from waiting: the job terminates as
        `interrupted` and the Colibri server may keep generating until it
        notices the disconnect. This method never claims otherwise.
        """
        clean_id = str(job_id or "").strip()
        with self._lock:
            record = self._records.get(clean_id)
            if record is None:
                row = self._fetch_row(clean_id)
                if row is None:
                    raise KeyError(f"deep local job not found: {job_id}")
                # Persisted-only rows are terminal (startup repair guarantees it).
                return self._row_snapshot(row)
            if record.state in TERMINAL_STATES:
                return self._snapshot_locked(record)
            record.cancel_event.set()
            if record.request_started and record.cancel_handle is not None:
                record.cancel_handle.cancel()
            if record.state != "cancel_requested":
                self._set_state_locked(record, "cancel_requested", self._db)
            return self._snapshot_locked(record)

    def retry(self, job_id: str) -> dict[str, Any]:
        """Explicit retry: clone a terminal failed/interrupted/cancelled job
        into a new queued job. Never automatic, never silent — a repeated
        retry call while the previous attempt is still live returns that
        attempt instead of stacking another one."""
        clean_id = str(job_id or "").strip()
        with self._condition:
            origin = self._records.get(clean_id)
            if origin is not None:
                origin_snapshot = self._snapshot_locked(origin)
                origin_question = origin.question
                origin_evidence = [dict(item) for item in origin.evidence]
                origin_params = dict(origin.params)
                origin_model = origin.model_id
                origin_attempts = origin.attempt_count
            else:
                row = self._fetch_row(clean_id)
                if row is None:
                    raise KeyError(f"deep local job not found: {job_id}")
                origin_snapshot = self._row_snapshot(row)
                origin_question = str(row["question"])
                origin_evidence = [
                    {"source_id": str(item.get("source_id") or ""), "snippet": str(item.get("snippet") or "")}
                    for item in _load_json(row["evidence_json"], [])
                    if isinstance(item, dict)
                ]
                origin_params = _load_json(row["params_json"], {})
                origin_model = str(row["model_id"])
                origin_attempts = int(row["attempt_count"] or 1)
            if origin_snapshot["state"] == "completed":
                raise ValueError("this Deep Local job already completed; submit a new job instead")
            if origin_snapshot["state"] not in TERMINAL_STATES:
                raise ValueError("this Deep Local job is still active; cancel it before retrying")
            for record in self._records.values():
                if record.retry_of == clean_id and record.state not in TERMINAL_STATES:
                    return {"ok": True, "job": self._snapshot_locked(record), "duplicate": True}
            if self._shutdown:
                raise ValueError("Deep Local job queue is shutting down")
            if self._active_count_locked() + 1 > MAX_QUEUED_JOBS:
                raise ValueError("Deep Local job queue is full; wait for current jobs to finish")

            config = self._read_config(self._db)
            if not config["enabled"] or not config["endpoint_is_loopback"]:
                return _structured_error(
                    "disabled",
                    "Deep Local (experimental) is not enabled, so this job cannot be retried.",
                )
            record = DeepLocalJobRecord(
                id=str(uuid.uuid4()),
                endpoint=config["endpoint"],
                model_id=origin_model,
                question=origin_question,
                evidence=origin_evidence,
                params=origin_params,
                attempt_count=origin_attempts + 1,
                retry_of=clean_id,
                created_at=utc_ms(),
            )
            record.state_history.append({"state": "queued", "at": record.created_at})
            self._insert_row(self._db, record)
            self._records[record.id] = record
            self._queue.append(record.id)
            self._ensure_worker_locked()
            self._condition.notify_all()
            snapshot = self._snapshot_locked(record)
        logger.info("deep_local job retried attempt=%s", snapshot["attempt_count"])
        return {"ok": True, "job": snapshot, "duplicate": False}

    def has_active_jobs(self) -> bool:
        with self._lock:
            return any(record.state not in TERMINAL_STATES for record in self._records.values())

    def shutdown(self, timeout: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Stop accepting jobs and let the process exit.

        Deliberately does NOT rewrite job states: a generation blocked in an
        HTTP read cannot stop quickly, the worker is a daemon thread, and
        startup repair will honestly mark whatever was in flight as
        `interrupted` on the next boot.
        """
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)

    # ------------------------------------------------------------- internals

    def _validate_evidence(self, evidence: list[dict[str, Any]] | None) -> list[dict[str, str]]:
        if evidence is None:
            return []
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list of {source_id, snippet} objects")
        if len(evidence) > MAX_EVIDENCE_ITEMS:
            raise ValueError(f"evidence must contain at most {MAX_EVIDENCE_ITEMS} snippets")
        clean: list[dict[str, str]] = []
        total_chars = 0
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("each evidence item must be an object")
            source_id = str(item.get("source_id") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            if not source_id or not snippet:
                raise ValueError("each evidence item needs a source_id and a snippet")
            if len(source_id) > MAX_SOURCE_ID_CHARS:
                raise ValueError(f"evidence source_id is longer than {MAX_SOURCE_ID_CHARS} characters")
            if len(snippet) > MAX_SNIPPET_CHARS:
                raise ValueError(f"an evidence snippet is longer than {MAX_SNIPPET_CHARS} characters")
            total_chars += len(snippet)
            clean.append({"source_id": source_id, "snippet": snippet})
        if total_chars > MAX_EVIDENCE_TOTAL_CHARS:
            raise ValueError(f"evidence is larger than {MAX_EVIDENCE_TOTAL_CHARS} characters in total")
        return clean

    @staticmethod
    def _read_config(db: Database) -> dict[str, Any]:
        from odysseus_desktop_backend.services.providers.colibri import is_loopback_endpoint

        endpoint = str(db.get_setting("deep_local_endpoint") or DEFAULT_COLIBRI_ENDPOINT)
        timeout = db.get_setting("deep_local_timeout_seconds")
        try:
            timeout_seconds = float(timeout) if timeout is not None else DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS
        except (TypeError, ValueError):
            timeout_seconds = DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS
        if timeout_seconds <= 0:
            timeout_seconds = DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS
        queue_wait = db.get_setting("deep_local_queue_wait_seconds")
        try:
            queue_wait_seconds = float(queue_wait) if queue_wait is not None else DEFAULT_QUEUE_WAIT_SECONDS
        except (TypeError, ValueError):
            queue_wait_seconds = DEFAULT_QUEUE_WAIT_SECONDS
        if queue_wait_seconds < 0:
            queue_wait_seconds = DEFAULT_QUEUE_WAIT_SECONDS
        return {
            "enabled": db.get_setting("deep_local_enabled") is True,
            "endpoint": endpoint.strip().rstrip("/"),
            "endpoint_is_loopback": is_loopback_endpoint(endpoint.strip().rstrip("/")),
            "timeout_seconds": timeout_seconds,
            "queue_wait_seconds": queue_wait_seconds,
        }

    def _active_count_locked(self) -> int:
        return sum(1 for record in self._records.values() if record.state not in TERMINAL_STATES)

    def _find_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        for record in self._records.values():
            if record.request_id == request_id:
                return self._snapshot_locked(record)
        row = self._db.conn.execute(
            "SELECT * FROM deep_local_jobs WHERE request_id = ?", (request_id,)
        ).fetchone()
        return self._row_snapshot(row) if row is not None else None

    def _fetch_row(self, job_id: str) -> Any:
        return self._db.conn.execute(
            "SELECT * FROM deep_local_jobs WHERE id = ?", (job_id,)
        ).fetchone()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="odysseus-deep-local-worker",
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        worker_db = Database(self.profile_dir)
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._shutdown:
                        self._condition.wait()
                    if self._shutdown:
                        # Never start new inference during shutdown; still-
                        # queued rows are repaired to `interrupted` next boot.
                        return
                    record = self._records[self._queue.popleft()]
                try:
                    self._run_job(record, worker_db)
                except Exception as exc:  # noqa: BLE001 - worker must survive any job
                    logger.error(
                        "deep_local worker unexpected error error_type=%s",
                        type(exc).__name__,
                    )
                    self._finalize(record, "failed", "deep_local_failed", worker_db,
                                   error_category="runtime_error")
        finally:
            worker_db.close()

    def _run_job(self, record: DeepLocalJobRecord, db: Database) -> None:
        if record.cancel_event.is_set():
            self._finalize(record, "cancelled_before_start", "cancelled_before_start", db)
            return
        with self._lock:
            record.started_at = utc_ms()
            if record.state == "queued":
                self._set_state_locked(record, "checking_runtime", db)
        config = self._read_config(db)
        try:
            with cancellation_scope(record.cancel_event), progress_operation(
                operation_id=f"deep-local-job-{record.id}"
            ):
                self._execute(record, db, config)
        except JobCancelledError:
            # Raised only at pre-request safe points: the completion request
            # never left this process.
            self._finalize(record, "cancelled_before_start", "cancelled_before_start", db)
        except ModelInterruptedError:
            self._finalize(record, "interrupted", "stopped_waiting", db,
                           error_category="interrupted")
        except ModelServiceError as exc:
            self._finalize(record, "failed", "deep_local_failed", db,
                           error_category=exc.category)
        except Exception as exc:  # noqa: BLE001 - job errors become fixed-code failures
            logger.warning("deep_local job failed error_type=%s", type(exc).__name__)
            self._finalize(record, "failed", "deep_local_failed", db,
                           error_category="runtime_error")

    def _execute(self, record: DeepLocalJobRecord, db: Database, config: dict[str, Any]) -> None:
        check_cancelled()
        if not config["enabled"]:
            self._finalize(record, "failed", "deep_local_failed", db, error_category="disabled")
            return
        try:
            provider = ColibriProvider(
                config["endpoint"],
                api_key=os.environ.get(COLIBRI_API_KEY_ENV) or None,
                timeout=config["timeout_seconds"],
            )
        except ValueError:
            self._finalize(record, "failed", "deep_local_failed", db, error_category="disabled")
            return

        # checking_runtime: reachability + model resolution, no tensor loading.
        health = provider.health()
        if not health.healthy:
            self._finalize(
                record, "failed", "deep_local_failed", db,
                error_category=health.error_category or "connection_failure",
            )
            return
        check_cancelled()
        if not record.model_id:
            models = provider.list_models()
            if len(models) != 1:
                self._finalize(record, "failed", "deep_local_failed", db,
                               error_category="invalid_model")
                return
            with self._lock:
                record.model_id = models[0].model_id
                self._persist(record, db)
        check_cancelled()

        messages = _build_messages(record.question, record.evidence)
        queue_deadline = utc_ms() + int(config["queue_wait_seconds"] * 1000)
        while True:
            # Last pre-request safe point. After this block the request is in
            # flight and cancellation can only mean "stop waiting".
            with self._lock:
                if record.cancel_event.is_set():
                    raise JobCancelledError("job cancelled")
                if record.state != "running":
                    self._set_state_locked(record, "running", db)
                record.cancel_handle = RequestCancelHandle()
                record.request_started = True
                handle = record.cancel_handle
            try:
                result = provider.chat_once(
                    record.model_id,
                    messages,
                    temperature=record.params.get("temperature"),
                    top_p=record.params.get("top_p"),
                    max_output_tokens=record.params.get("max_output_tokens"),
                    thinking=record.params.get("thinking") or "off",
                    cancel_handle=handle,
                )
            except ModelQueueSaturatedError as exc:
                with self._lock:
                    record.request_started = False
                    record.cancel_handle = None
                retry_after = exc.retry_after_seconds or QUEUE_RETRY_FALLBACK_SECONDS
                if utc_ms() + int(retry_after * 1000) > queue_deadline:
                    self._finalize(record, "failed", "deep_local_failed", db,
                                   error_category=exc.category)
                    return
                with self._lock:
                    if record.state == "running":
                        self._set_state_locked(record, "waiting_for_provider", db)
                # cancel-aware backoff: waking on the event is a cancellation.
                if record.cancel_event.wait(timeout=retry_after):
                    raise JobCancelledError("job cancelled") from exc
                continue
            finally:
                with self._lock:
                    if record.cancel_handle is not None and not record.cancel_handle.cancelled:
                        record.request_started = False
                        record.cancel_handle = None
            break

        with self._lock:
            record.result_text = result.content
            record.thinking_text = result.thinking
            record.usage = {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "elapsed_ms": result.elapsed_ms,
                "queue_wait_ms": result.queue_wait_ms,
                "tokens_per_second": result.tokens_per_second,
            }
            record.warnings = list(result.warnings)
            record.model_id = result.model_id
        self._finalize(record, "completed", "", db)

    def _set_state_locked(self, record: DeepLocalJobRecord, state: str, db: Database) -> None:
        if state not in JOB_STATES:
            raise ValueError(f"unknown deep local job state: {state}")
        allowed = _LEGAL_TRANSITIONS.get(record.state, set())
        if state not in allowed:
            raise ValueError(f"illegal deep local job transition: {record.state} -> {state}")
        record.state = state
        record.state_history.append({"state": state, "at": utc_ms()})
        self._persist(record, db)

    def _finalize(
        self,
        record: DeepLocalJobRecord,
        state: str,
        message_code: str,
        db: Database,
        *,
        error_category: str = "",
    ) -> None:
        with self._lock:
            if record.state in TERMINAL_STATES:
                return
            record.message_code = message_code if message_code in MESSAGE_CODES else "deep_local_failed"
            record.error_category = error_category
            record.finished_at = utc_ms()
            self._set_state_locked(record, state, db)
            self._prune_locked(db)
        logger.info(
            "deep_local job finished state=%s code=%s category=%s",
            state,
            record.message_code or "none",
            error_category or "none",
        )

    def _prune_locked(self, db: Database) -> None:
        rows = db.conn.execute(
            "SELECT id FROM deep_local_jobs WHERE state IN ({placeholders}) "
            "ORDER BY created_at DESC, id".format(
                placeholders=",".join("?" for _ in TERMINAL_STATES)
            ),
            tuple(TERMINAL_STATES),
        ).fetchall()
        for row in rows[MAX_RETAINED_TERMINAL_JOBS:]:
            stale_id = str(row["id"])
            db.conn.execute("DELETE FROM deep_local_jobs WHERE id = ?", (stale_id,))
            self._records.pop(stale_id, None)
        db.conn.commit()

    # ------------------------------------------------------- persistence

    def _insert_row(self, db: Database, record: DeepLocalJobRecord) -> None:
        db.conn.execute(
            """
            INSERT INTO deep_local_jobs (
                id, request_id, state, message_code, error_category, provider,
                endpoint, model_id, question, evidence_json, params_json,
                result_text, thinking_text, usage_json, warnings_json,
                state_history_json, attempt_count, retry_of,
                created_at, updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.request_id,
                record.state,
                record.message_code,
                record.error_category,
                "colibri",
                record.endpoint,
                record.model_id,
                record.question,
                json.dumps(record.evidence),
                json.dumps(record.params),
                record.result_text,
                record.thinking_text,
                json.dumps(record.usage),
                json.dumps(record.warnings),
                json.dumps(record.state_history),
                record.attempt_count,
                record.retry_of,
                record.created_at,
                utc_ms(),
                record.started_at,
                record.finished_at,
            ),
        )
        db.conn.commit()

    def _persist(self, record: DeepLocalJobRecord, db: Database) -> None:
        db.conn.execute(
            """
            UPDATE deep_local_jobs
            SET state = ?, message_code = ?, error_category = ?, model_id = ?,
                result_text = ?, thinking_text = ?, usage_json = ?,
                warnings_json = ?, state_history_json = ?, updated_at = ?,
                started_at = ?, finished_at = ?
            WHERE id = ?
            """,
            (
                record.state,
                record.message_code,
                record.error_category,
                record.model_id,
                record.result_text,
                record.thinking_text,
                json.dumps(record.usage),
                json.dumps(record.warnings),
                json.dumps(record.state_history),
                utc_ms(),
                record.started_at,
                record.finished_at,
                record.id,
            ),
        )
        db.conn.commit()

    # --------------------------------------------------------- snapshots

    def _snapshot_locked(self, record: DeepLocalJobRecord) -> dict[str, Any]:
        queue_position = None
        if record.state in {"queued", "cancel_requested"} and record.started_at is None:
            try:
                queue_position = list(self._queue).index(record.id) + 1
            except ValueError:
                queue_position = None
        end = record.finished_at or utc_ms()
        elapsed_ms = max(0, end - record.started_at) if record.started_at else 0
        return {
            "job_id": record.id,
            "provider": "colibri",
            "state": record.state,
            "message_code": record.message_code,
            "error_category": record.error_category,
            "endpoint": record.endpoint,
            "model_id": record.model_id,
            "question_chars": len(record.question),
            "evidence_count": len(record.evidence),
            "result_chars": len(record.result_text),
            "usage": dict(record.usage),
            "warnings": list(record.warnings),
            "state_history": [dict(item) for item in record.state_history],
            "attempt_count": record.attempt_count,
            "retry_of": record.retry_of,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "elapsed_ms": elapsed_ms,
            "queue_position": queue_position,
        }

    def _row_snapshot(self, row: Any) -> dict[str, Any]:
        finished_at = row["finished_at"]
        started_at = row["started_at"]
        elapsed_ms = 0
        if started_at:
            elapsed_ms = max(0, (finished_at or utc_ms()) - started_at)
        return {
            "job_id": str(row["id"]),
            "provider": str(row["provider"]),
            "state": str(row["state"]),
            "message_code": str(row["message_code"]),
            "error_category": str(row["error_category"]),
            "endpoint": str(row["endpoint"]),
            "model_id": str(row["model_id"]),
            "question_chars": len(str(row["question"])),
            "evidence_count": len(_load_json(row["evidence_json"], [])),
            "result_chars": len(str(row["result_text"])),
            "usage": _load_json(row["usage_json"], {}),
            "warnings": _load_json(row["warnings_json"], []),
            "state_history": _load_json(row["state_history_json"], []),
            "attempt_count": int(row["attempt_count"] or 1),
            "retry_of": str(row["retry_of"]),
            "created_at": int(row["created_at"]),
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_ms": elapsed_ms,
            "queue_position": None,
        }


def _build_messages(question: str, evidence: list[dict[str, str]]) -> list[dict[str, str]]:
    if not evidence:
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
    blocks = [
        f"[S{index}] (source {item['source_id']})\n{item['snippet']}"
        for index, item in enumerate(evidence, start=1)
    ]
    user = "Source excerpts:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _structured_error(category: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_category": category, "error": message}


def _load_json(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default
