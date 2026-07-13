"""Issue #16 — job state machine core (V04_ESSENTIAL_SEMANTICS.md §A).

These tests drive JobService with tiny synthetic executors only; no
DocumentService, OCR, or RAG involvement. Real-work integration is covered
separately once the state machine core is proven.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pytest

from odysseus_desktop_backend.cancellation import check_cancelled
from odysseus_desktop_backend.services.job_service import (
    _LEGAL_TRANSITIONS,
    JOB_MESSAGE_CODES,
    JOB_STATES,
    MAX_QUEUED_JOBS,
    TERMINAL_JOB_STATES,
    JobFailure,
    JobRecord,
    JobService,
)


PRIVATE_SENTINEL = "PRIVATE_JOB_SENTINEL_MUST_NOT_APPEAR"


class SyntheticExecutor:
    """Scriptable executor: runs a callable per job, records rollbacks."""

    instances: list["SyntheticExecutor"] = []

    def __init__(self, work: Callable[[JobRecord, Callable[[], None]], None]):
        self.work = work
        self.rolled_back: list[str] = []
        self.closed = False
        SyntheticExecutor.instances.append(self)

    def run(self, job: JobRecord, on_running: Callable[[], None]) -> None:
        self.work(job, on_running)

    def rollback(self, job: JobRecord) -> None:
        self.rolled_back.append(job.id)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_executor_registry():
    SyntheticExecutor.instances = []
    yield
    SyntheticExecutor.instances = []


def make_service(tmp_path, work: Callable[[JobRecord, Callable[[], None]], None]) -> JobService:
    return JobService(tmp_path, executor_factory=lambda: SyntheticExecutor(work))


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached within timeout")


def wait_terminal(service: JobService, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    wait_for(lambda: service.get(job_id)["state"] in TERMINAL_JOB_STATES, timeout)
    return service.get(job_id)


def instant_work(job: JobRecord, on_running: Callable[[], None]) -> None:
    on_running()


def failing_work(job: JobRecord, on_running: Callable[[], None]) -> None:
    on_running()
    raise JobFailure("file_not_found")


class GatedWork:
    """Work that signals when running and blocks until released, checking
    cancellation at each poll — a controllable cooperative job."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        self.started.set()
        while not self.release.wait(timeout=0.01):
            check_cancelled()


# ------------------------------------------------------------ state machine


def test_transition_table_matches_contract():
    assert JOB_STATES == {
        "queued",
        "preflighting",
        "running",
        "cancel_requested",
        "cancelled",
        "completed",
        "failed",
    }
    assert TERMINAL_JOB_STATES == {"cancelled", "completed", "failed"}
    # No transition leaves a terminal state.
    for state in TERMINAL_JOB_STATES:
        assert state not in _LEGAL_TRANSITIONS
    # cancel_requested must be able to resolve as completed (post-commit cancel).
    assert "completed" in _LEGAL_TRANSITIONS["cancel_requested"]


def test_illegal_transitions_rejected(tmp_path):
    service = make_service(tmp_path, instant_work)
    job = JobRecord(id="j", kind="import")
    with pytest.raises(ValueError, match="illegal job transition"):
        service._set_state_locked(job, "completed")  # queued -> completed
    job.state = "completed"
    with pytest.raises(ValueError, match="illegal job transition"):
        service._set_state_locked(job, "running")
    with pytest.raises(ValueError, match="unknown job state"):
        service._set_state_locked(job, "paused")


def test_fifo_ordering(tmp_path):
    order: list[str] = []
    lock = threading.Lock()

    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        with lock:
            order.append(job.path)

    service = make_service(tmp_path, work)
    snapshots = service.submit_import(["a.txt", "b.txt", "c.txt"])
    for snapshot in snapshots:
        wait_terminal(service, snapshot["job_id"])
    assert order == ["a.txt", "b.txt", "c.txt"]
    service.shutdown()


def test_single_worker_no_concurrent_jobs(tmp_path):
    active = []
    max_active = []
    lock = threading.Lock()

    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        with lock:
            active.append(job.id)
            max_active.append(len(active))
        time.sleep(0.02)
        with lock:
            active.remove(job.id)

    service = make_service(tmp_path, work)
    snapshots = service.submit_import(["a.txt", "b.txt", "c.txt", "d.txt"])
    for snapshot in snapshots:
        wait_terminal(service, snapshot["job_id"])
    assert max(max_active) == 1
    service.shutdown()


def test_queue_full_rejected(tmp_path):
    gate = GatedWork()
    service = make_service(tmp_path, gate)
    service.submit_import(["hold.txt"])
    gate.started.wait(timeout=5)
    service.submit_import([f"f{i}.txt" for i in range(MAX_QUEUED_JOBS)])
    with pytest.raises(ValueError, match="queue is full"):
        service.submit_import(["overflow.txt"])
    gate.release.set()
    service.shutdown()


def test_cancel_while_queued_never_runs(tmp_path):
    ran: list[str] = []
    gate = GatedWork()

    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        if job.path == "hold.txt":
            gate(job, on_running)
            return
        on_running()
        ran.append(job.path)

    service = make_service(tmp_path, work)
    service.submit_import(["hold.txt"])
    gate.started.wait(timeout=5)
    queued = service.submit_import(["victim.txt"])[0]
    assert queued["state"] == "queued"
    assert queued["queue_position"] == 1

    cancelled = service.cancel(queued["job_id"])
    assert cancelled["state"] == "cancel_requested"
    gate.release.set()
    final = wait_terminal(service, queued["job_id"])
    assert final["state"] == "cancelled"
    assert final["message_code"] == "cancelled_by_user"
    assert ran == []
    # A job cancelled before starting was never given an executor to roll back.
    assert all(not ex.rolled_back for ex in SyntheticExecutor.instances)
    service.shutdown()


def test_cancel_during_running_rolls_back(tmp_path):
    gate = GatedWork()
    service = make_service(tmp_path, gate)
    job = service.submit_import(["work.txt"])[0]
    gate.started.wait(timeout=5)
    assert service.get(job["job_id"])["state"] == "running"

    snapshot = service.cancel(job["job_id"])
    assert snapshot["state"] == "cancel_requested"
    final = wait_terminal(service, job["job_id"])
    assert final["state"] == "cancelled"
    assert final["message_code"] == "cancelled_by_user"
    executor = SyntheticExecutor.instances[0]
    assert executor.rolled_back == [job["job_id"]]
    assert executor.closed is True
    service.shutdown()


def test_repeated_cancel_is_idempotent(tmp_path):
    gate = GatedWork()
    service = make_service(tmp_path, gate)
    job = service.submit_import(["work.txt"])[0]
    gate.started.wait(timeout=5)
    first = service.cancel(job["job_id"])
    second = service.cancel(job["job_id"])
    assert first["state"] == second["state"] == "cancel_requested"
    final = wait_terminal(service, job["job_id"])
    third = service.cancel(job["job_id"])
    assert third["state"] == "cancelled"
    assert third["finished_at"] == final["finished_at"]
    service.shutdown()


def test_cancel_after_completion_stays_completed(tmp_path):
    service = make_service(tmp_path, instant_work)
    job = service.submit_import(["done.txt"])[0]
    final = wait_terminal(service, job["job_id"])
    assert final["state"] == "completed"
    after = service.cancel(job["job_id"])
    assert after["state"] == "completed"
    assert after["message_code"] == ""
    service.shutdown()


def test_cancel_after_failure_stays_failed(tmp_path):
    service = make_service(tmp_path, failing_work)
    job = service.submit_import(["bad.txt"])[0]
    final = wait_terminal(service, job["job_id"])
    assert final["state"] == "failed"
    assert final["message_code"] == "file_not_found"
    after = service.cancel(job["job_id"])
    assert after["state"] == "failed"
    assert after["message_code"] == "file_not_found"
    service.shutdown()


def test_cancel_at_commit_boundary_resolves_completed(tmp_path):
    """A cancel that lands after the last safe point must not un-commit."""
    committed = threading.Event()
    proceed = threading.Event()
    service_holder: list[JobService] = []
    job_holder: list[str] = []

    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        check_cancelled()  # last safe point
        # Commit happens here; cancel arrives concurrently, after the check.
        committed.set()
        proceed.wait(timeout=5)

    service = JobService(tmp_path, executor_factory=lambda: SyntheticExecutor(work))
    service_holder.append(service)
    job = service.submit_import(["commit.txt"])[0]
    job_holder.append(job["job_id"])
    committed.wait(timeout=5)
    snapshot = service.cancel(job["job_id"])  # after the last check_cancelled
    assert snapshot["state"] == "cancel_requested"
    proceed.set()
    final = wait_terminal(service, job["job_id"])
    assert final["state"] == "completed"
    assert final["message_code"] == ""
    # Completed work is never rolled back.
    assert all(not ex.rolled_back for ex in SyntheticExecutor.instances)
    service.shutdown()


def test_cancelled_and_failed_remain_distinct(tmp_path):
    gate = GatedWork()

    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        if job.path == "fails.txt":
            on_running()
            raise JobFailure("unsupported_file_type")
        gate(job, on_running)

    service = make_service(tmp_path, work)
    cancelled_job = service.submit_import(["held.txt"])[0]
    failed_job = service.submit_import(["fails.txt"])[0]
    gate.started.wait(timeout=5)
    service.cancel(cancelled_job["job_id"])
    first = wait_terminal(service, cancelled_job["job_id"])
    second = wait_terminal(service, failed_job["job_id"])
    assert first["state"] == "cancelled"
    assert first["message_code"] == "cancelled_by_user"
    assert second["state"] == "failed"
    assert second["message_code"] == "unsupported_file_type"
    service.shutdown()


def test_unexpected_exception_becomes_fixed_code_failure(tmp_path):
    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        raise RuntimeError(PRIVATE_SENTINEL)

    service = make_service(tmp_path, work)
    job = service.submit_import(["boom.txt"])[0]
    final = wait_terminal(service, job["job_id"])
    assert final["state"] == "failed"
    assert final["message_code"] == "job_failed"
    executor = SyntheticExecutor.instances[0]
    assert executor.rolled_back == [job["job_id"]]
    service.shutdown()


# ----------------------------------------------------------------- shutdown


def test_shutdown_with_empty_queue_returns_quickly(tmp_path):
    service = make_service(tmp_path, instant_work)
    job = service.submit_import(["a.txt"])[0]
    wait_terminal(service, job["job_id"])
    started = time.monotonic()
    service.shutdown()
    assert time.monotonic() - started < 2.0
    with pytest.raises(ValueError, match="shutting down"):
        service.submit_import(["late.txt"])


def test_shutdown_with_queued_jobs_cancels_them(tmp_path):
    gate = GatedWork()
    service = make_service(tmp_path, gate)
    running = service.submit_import(["running.txt"])[0]
    queued = service.submit_import(["queued.txt"])[0]
    gate.started.wait(timeout=5)
    gate.release.set()
    service.shutdown()
    assert service.get(queued["job_id"])["state"] in {"cancelled", "cancel_requested"}
    assert service.get(running["job_id"])["state"] in TERMINAL_JOB_STATES


def test_shutdown_with_cooperative_running_job_finishes_cancelled(tmp_path):
    gate = GatedWork()  # never released; relies on cancellation polling
    service = make_service(tmp_path, gate)
    job = service.submit_import(["coop.txt"])[0]
    gate.started.wait(timeout=5)
    service.shutdown(timeout=5.0)
    assert service.get(job["job_id"])["state"] == "cancelled"


# ------------------------------------------------------------------ privacy


def test_snapshots_never_contain_paths_or_private_input(tmp_path):
    gate = GatedWork()
    service = make_service(tmp_path, gate)
    hostile_path = f"C:/Users/secret/{PRIVATE_SENTINEL}.txt"
    job = service.submit_import([hostile_path])[0]
    gate.started.wait(timeout=5)
    for snapshot in [service.get(job["job_id"]), *service.list()]:
        rendered = repr(snapshot)
        assert PRIVATE_SENTINEL not in rendered
        assert "secret" not in rendered
        assert hostile_path not in rendered
    gate.release.set()
    final = wait_terminal(service, job["job_id"])
    assert PRIVATE_SENTINEL not in repr(final)
    service.shutdown()


def test_failure_codes_are_a_closed_set(tmp_path):
    assert JobFailure(PRIVATE_SENTINEL).code == "job_failed"
    for code in JOB_MESSAGE_CODES:
        assert PRIVATE_SENTINEL not in code
    service = make_service(tmp_path, failing_work)
    job = service.submit_import(["x.txt"])[0]
    final = wait_terminal(service, job["job_id"])
    assert final["message_code"] in JOB_MESSAGE_CODES
    service.shutdown()


def test_worker_logs_use_fixed_labels_only(tmp_path, caplog):
    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        raise RuntimeError(PRIVATE_SENTINEL)

    service = make_service(tmp_path, work)
    with caplog.at_level("DEBUG"):
        job = service.submit_import([f"{PRIVATE_SENTINEL}.txt"])[0]
        wait_terminal(service, job["job_id"])
    assert PRIVATE_SENTINEL not in caplog.text
    service.shutdown()


# ------------------------------------------------------------- concurrency


def test_status_and_list_are_thread_safe_under_load(tmp_path):
    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        time.sleep(0.002)

    service = make_service(tmp_path, work)
    snapshots = service.submit_import([f"f{i}.txt" for i in range(10)])
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            for _ in range(200):
                service.list()
                for snapshot in snapshots:
                    service.get(snapshot["job_id"])
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    readers = [threading.Thread(target=hammer) for _ in range(4)]
    for reader in readers:
        reader.start()
    for snapshot in snapshots:
        wait_terminal(service, snapshot["job_id"])
    for reader in readers:
        reader.join(timeout=10)
    assert errors == []
    service.shutdown()


def test_stale_job_id_raises_key_error(tmp_path):
    service = make_service(tmp_path, instant_work)
    with pytest.raises(KeyError):
        service.get("no-such-job")
    with pytest.raises(KeyError):
        service.cancel("no-such-job")
    service.shutdown()


def test_ocr_submission_dedupes_active_document(tmp_path):
    gate = GatedWork()
    service = make_service(tmp_path, gate)
    first = service.submit_ocr("11111111-1111-1111-1111-111111111111")
    assert first["kind"] == "ocr"
    with pytest.raises(ValueError, match="already running"):
        service.submit_ocr("11111111-1111-1111-1111-111111111111")
    gate.started.wait(timeout=5)
    service.cancel(first["job_id"])
    wait_terminal(service, first["job_id"])
    second = service.submit_ocr("11111111-1111-1111-1111-111111111111")
    assert second["job_id"] != first["job_id"]
    service.shutdown()
