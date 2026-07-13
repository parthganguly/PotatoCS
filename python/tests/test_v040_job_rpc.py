"""Issue #16 — jobs.* JSON-RPC vertical slice over SidecarApp.dispatch.

Uses a synthetic executor swapped into the app's JobService so the full RPC
path (submit -> status/list -> cancel -> terminal state) is proven before any
real DocumentService integration.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from io import StringIO
from pathlib import Path
from typing import Any, Callable

import pytest

from odysseus_desktop_backend.cancellation import check_cancelled
from odysseus_desktop_backend.services.job_service import (
    TERMINAL_JOB_STATES,
    JobFailure,
    JobRecord,
    JobService,
)
from rpc_server import SidecarApp


PRIVATE_SENTINEL = "PRIVATE_RPC_JOB_SENTINEL_MUST_NOT_APPEAR"

SNAPSHOT_KEYS = {
    "job_id",
    "kind",
    "state",
    "message_code",
    "scope",
    "document_id",
    "artifact_id",
    "created_at",
    "started_at",
    "finished_at",
    "elapsed_ms",
    "queue_position",
}


class ScriptedExecutor:
    def __init__(self, work: Callable[[JobRecord, Callable[[], None]], None]):
        self.work = work

    def run(self, job: JobRecord, on_running: Callable[[], None]) -> None:
        self.work(job, on_running)

    def rollback(self, job: JobRecord) -> None:
        pass

    def close(self) -> None:
        pass


class GatedWork:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        self.started.set()
        while not self.release.wait(timeout=0.01):
            check_cancelled()


@pytest.fixture()
def app(tmp_path: Path):
    application = SidecarApp(tmp_path / "profile")
    yield application
    application.close()


def use_synthetic_jobs(app: SidecarApp, work: Callable[[JobRecord, Callable[[], None]], None]) -> None:
    app.jobs.shutdown()
    app.jobs = JobService(
        app.profile_dir, executor_factory=lambda: ScriptedExecutor(work)
    )


def wait_rpc_terminal(app: SidecarApp, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = app.dispatch("jobs.get", {"job_id": job_id})
        if snapshot["state"] in TERMINAL_JOB_STATES:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state within timeout")


def assert_snapshot_schema(snapshot: dict[str, Any]) -> None:
    assert set(snapshot.keys()) == SNAPSHOT_KEYS
    assert isinstance(snapshot["job_id"], str)
    assert snapshot["kind"] in {"import", "ocr"}
    assert snapshot["message_code"] is not None
    json.dumps(snapshot)  # payload must be JSON-serializable as-is


def test_rpc_submit_cancel_lifecycle(app: SidecarApp):
    gate = GatedWork()
    use_synthetic_jobs(app, gate)

    result = app.dispatch("jobs.submit_import", {"paths": ["synthetic.txt"]})
    assert isinstance(result["jobs"], list) and len(result["jobs"]) == 1
    submitted = result["jobs"][0]
    assert_snapshot_schema(submitted)
    assert submitted["state"] == "queued"

    gate.started.wait(timeout=5)
    running = app.dispatch("jobs.get", {"job_id": submitted["job_id"]})
    assert running["state"] == "running"

    cancelling = app.dispatch("jobs.cancel", {"job_id": submitted["job_id"]})
    assert cancelling["state"] == "cancel_requested"

    final = wait_rpc_terminal(app, submitted["job_id"])
    assert final["state"] == "cancelled"
    assert final["message_code"] == "cancelled_by_user"
    assert_snapshot_schema(final)

    # Idempotent cancel over RPC after terminal state.
    again = app.dispatch("jobs.cancel", {"job_id": submitted["job_id"]})
    assert again["state"] == "cancelled"

    listed = app.dispatch("jobs.list", {})
    assert [item["job_id"] for item in listed] == [submitted["job_id"]]
    assert_snapshot_schema(listed[0])


def test_rpc_completed_lifecycle(app: SidecarApp):
    use_synthetic_jobs(app, lambda job, on_running: on_running())
    submitted = app.dispatch("jobs.submit_import", {"paths": ["quick.txt"]})["jobs"][0]
    final = wait_rpc_terminal(app, submitted["job_id"])
    assert final["state"] == "completed"
    assert final["message_code"] == ""


def test_rpc_failed_lifecycle(app: SidecarApp):
    def work(job: JobRecord, on_running: Callable[[], None]) -> None:
        on_running()
        raise JobFailure("unsupported_file_type")

    use_synthetic_jobs(app, work)
    submitted = app.dispatch("jobs.submit_import", {"paths": ["bad.bin"]})["jobs"][0]
    final = wait_rpc_terminal(app, submitted["job_id"])
    assert final["state"] == "failed"
    assert final["message_code"] == "unsupported_file_type"


def test_rpc_param_validation(app: SidecarApp):
    from rpc_server import RpcError

    with pytest.raises(RpcError):
        app.dispatch("jobs.submit_import", {"paths": "not-a-list"})
    with pytest.raises(RpcError):
        app.dispatch("jobs.get", {})
    with pytest.raises(RpcError):
        app.dispatch("jobs.cancel", {"job_id": ""})
    with pytest.raises(KeyError):
        app.dispatch("jobs.get", {"job_id": "stale-id"})


def test_rpc_job_payloads_and_progress_never_leak_private_input(app: SidecarApp):
    """Hostile path submitted; RPC payloads and stderr progress stay clean."""
    gate = GatedWork()
    use_synthetic_jobs(app, gate)
    hostile = f"C:/Users/secret/{PRIVATE_SENTINEL}.txt"

    captured = StringIO()
    original_stderr = sys.stderr
    sys.stderr = captured
    try:
        submitted = app.dispatch("jobs.submit_import", {"paths": [hostile]})["jobs"][0]
        gate.started.wait(timeout=5)
        snapshot = app.dispatch("jobs.get", {"job_id": submitted["job_id"]})
        listed = app.dispatch("jobs.list", {})
        app.dispatch("jobs.cancel", {"job_id": submitted["job_id"]})
        final = wait_rpc_terminal(app, submitted["job_id"])
    finally:
        sys.stderr = original_stderr

    for payload in [submitted, snapshot, final, *listed]:
        rendered = json.dumps(payload)
        assert PRIVATE_SENTINEL not in rendered
        assert "secret" not in rendered

    stderr_text = captured.getvalue()
    assert PRIVATE_SENTINEL not in stderr_text
    # Progress events, if any were emitted, must be fixed-vocabulary JSON.
    for line in stderr_text.splitlines():
        if "__odysseus_progress__" in line:
            event = json.loads(line)
            assert event.get("detail") is None
            assert PRIVATE_SENTINEL not in json.dumps(event)


def test_rpc_jobs_submit_is_not_on_host_replay_allowlist():
    """jobs.submit_import must never be auto-replayed after sidecar loss."""
    lib_rs = (
        Path(__file__).resolve().parents[2] / "src-tauri" / "src" / "lib.rs"
    ).read_text(encoding="utf-8")
    start = lib_rs.index("fn can_restart_and_retry")
    end = lib_rs.index("}", lib_rs.index("matches!", start))
    allowlist = lib_rs[start:end]
    assert "jobs.submit_import" not in allowlist
    assert "jobs.cancel" not in allowlist


def test_rpc_app_close_shuts_down_job_worker(tmp_path: Path):
    application = SidecarApp(tmp_path / "profile")
    gate = GatedWork()
    use_synthetic_jobs(application, gate)
    submitted = application.dispatch("jobs.submit_import", {"paths": ["held.txt"]})["jobs"][0]
    gate.started.wait(timeout=5)
    application.close()  # must cancel the cooperative job and join the worker
    snapshot = application.jobs.get(submitted["job_id"])
    assert snapshot["state"] == "cancelled"
    with pytest.raises(ValueError, match="shutting down"):
        application.jobs.submit_import(["late.txt"])


def test_restart_behavior_is_honest(tmp_path: Path):
    """After a new SidecarApp boots, prior jobs are gone — by design.

    The registry is in-memory (semantics contract §A rule 7): restart never
    resurrects queued or cancelled work. Staged document repair is a separate
    DocumentService concern covered when real imports are integrated.
    """
    first = SidecarApp(tmp_path / "profile")
    use_synthetic_jobs(first, lambda job, on_running: on_running())
    submitted = first.dispatch("jobs.submit_import", {"paths": ["a.txt"]})["jobs"][0]
    wait_rpc_terminal(first, submitted["job_id"])
    first.close()

    second = SidecarApp(tmp_path / "profile")
    try:
        assert second.dispatch("jobs.list", {}) == []
        with pytest.raises(KeyError):
            second.dispatch("jobs.get", {"job_id": submitted["job_id"]})
    finally:
        second.close()
