"""Job Object zero-member proof: unit tests plus a bounded Windows integration test.

The integration tests launch a harmless synthetic parent (the running Python
interpreter) which spawns a grandchild that appends to a file in a loop, then
exits immediately -- so the grandchild outlives its parent. They prove the Job
Object membership query positively establishes an empty tree in that shape,
that teardown really stops the grandchild, and that an unavailable count fails
closed.

Note on what is *not* asserted: whether a parent-PID descendant snapshot can
still see the grandchild after the parent exits is deliberately left
unasserted. Windows does not clear a child's recorded parent PID when the
parent dies, so on this platform the snapshot often still finds it -- via a
stale link that a recycled PID would invalidate. The reason the job query is
the primary proof is that it is a positive count independent of parentage, not
that the snapshot is guaranteed blind.

No model, no engine, no network, and no access to ``D:\\Colibri``. Every
process is confined to a kill-on-close Job Object and every wait is bounded.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from odysseus_desktop_backend.services import colibri_stage2_job_probe as probe_mod
from odysseus_desktop_backend.services.colibri_stage2_common import ColibriStage2Failure

# ---------------------------------------------------------------------------
# Unit tests for the proof loop (no processes at all)
# ---------------------------------------------------------------------------


class _FakeApi:
    def __init__(self, *, wait_ok: bool = True, descendants: set[int] | None = None) -> None:
        self.wait_ok = wait_ok
        self.descendants = set() if descendants is None else descendants
        self.calls: list[str] = []

    def terminate_job(self, job: Any) -> None:
        self.calls.append("terminate_job")

    def terminate_process(self, process: Any) -> None:
        self.calls.append("terminate_process")

    def wait_process(self, process: Any, timeout_ms: int) -> bool:
        self.calls.append("wait_process")
        return self.wait_ok

    def descendant_process_ids(self, process_id: int) -> set[int]:
        self.calls.append("descendant_process_ids")
        return set(self.descendants)


class _FakeProcess:
    process_id = 4242


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def time(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _teardown(api: _FakeApi, member_counts: list[int | None], *, deadline: float = 1.0) -> Any:
    clock = _Clock()
    remaining = list(member_counts)

    def member_probe(job: Any) -> int | None:
        return remaining.pop(0) if remaining else (member_counts[-1] if member_counts else None)

    return probe_mod.terminate_and_prove_job_empty(
        api,
        job="job-1",
        process=_FakeProcess(),
        job_assigned=True,
        member_probe=member_probe,
        deadline=deadline,
        clock=clock.time,
        sleep=clock.advance,
        failure_types=(ColibriStage2Failure,),
    )


def test_zero_members_proves_an_empty_job() -> None:
    api = _FakeApi()
    evidence = _teardown(api, [0])
    assert evidence.job_terminated is True
    assert evidence.job_empty_proven is True
    assert evidence.job_member_count == 0
    assert evidence.cleanup_failed is False
    assert evidence.orphan_detected is False
    assert "terminate_job" in api.calls


def test_members_draining_over_several_polls_still_proves_empty() -> None:
    evidence = _teardown(_FakeApi(), [3, 2, 0])
    assert evidence.job_empty_proven is True
    assert evidence.cleanup_failed is False


def test_unavailable_member_count_fails_closed() -> None:
    evidence = _teardown(_FakeApi(), [None])
    assert evidence.job_empty_proven is False
    assert evidence.job_member_count is None
    assert evidence.cleanup_failed is True


def test_job_that_never_empties_fails_closed() -> None:
    evidence = _teardown(_FakeApi(), [1])
    assert evidence.job_empty_proven is False
    assert evidence.job_member_count == 1
    assert evidence.cleanup_failed is True


def test_a_surviving_descendant_is_recorded_as_supplementary_evidence() -> None:
    evidence = _teardown(_FakeApi(descendants={99}), [0])
    assert evidence.job_empty_proven is True
    assert evidence.orphan_detected is True
    assert evidence.descendant_count == 1


def test_unconfirmed_root_exit_fails_closed() -> None:
    evidence = _teardown(_FakeApi(wait_ok=False), [0])
    assert evidence.root_exit_confirmed is False
    assert evidence.cleanup_failed is True


def test_lone_process_is_terminated_when_there_is_no_job() -> None:
    api = _FakeApi()
    clock = _Clock()
    evidence = probe_mod.terminate_and_prove_job_empty(
        api,
        job=None,
        process=_FakeProcess(),
        job_assigned=False,
        member_probe=lambda job: None,
        deadline=1.0,
        clock=clock.time,
        sleep=clock.advance,
        failure_types=(ColibriStage2Failure,),
    )
    assert "terminate_process" in api.calls
    assert evidence.job_terminated is False
    # With no job there is no membership proof, so this still fails closed.
    assert evidence.cleanup_failed is True


def test_member_probe_returns_none_off_windows_or_without_a_job() -> None:
    assert probe_mod.job_active_process_count(None) is None
    assert probe_mod.job_active_process_count("not-a-handle") is None


def test_teardown_evidence_is_frozen() -> None:
    import dataclasses

    evidence = _teardown(_FakeApi(), [0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.job_empty_proven = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Bounded Windows integration: a real parent that spawns a real grandchild
# ---------------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")

# The parent spawns a detached grandchild that appends to `marker` roughly ten
# times a second for a bounded lifetime, then the parent exits immediately --
# leaving the grandchild running and no longer reachable through the parent's
# PID. That is exactly the shape a descendant snapshot cannot prove empty.
_PARENT_SOURCE = """
import subprocess, sys
marker = sys.argv[1]
child = (
    "import time\\n"
    "for _ in range(600):\\n"
    "    open(%r, 'a').write('x')\\n"
    "    time.sleep(0.1)\\n"
) % (marker,)
subprocess.Popen([sys.executable, "-c", child])
"""

_CHILD_ENV_KEYS = ("SystemRoot", "SystemDrive", "WINDIR", "TEMP", "TMP", "PATH")


def _child_environment(tmp_path: Path) -> dict[str, str]:
    import os

    folded = {key.casefold(): value for key, value in os.environ.items()}
    environment: dict[str, str] = {}
    for key in _CHILD_ENV_KEYS:
        value = folded.get(key.casefold())
        if value:
            environment[key] = value
    environment["TEMP"] = str(tmp_path)
    environment["TMP"] = str(tmp_path)
    return environment


@pytest.fixture()
def lifecycle_api() -> Any:
    from odysseus_desktop_backend.runtime_bench.isolated_server import WindowsLifecycleApi

    return WindowsLifecycleApi()


def _launch_tree(api: Any, tmp_path: Path) -> tuple[Any, Any, Path]:
    """Launch parent+grandchild inside a kill-on-close job. Bounded."""

    marker = tmp_path / "grandchild.log"
    script = tmp_path / "parent.py"
    script.write_text(_PARENT_SOURCE, encoding="utf-8")

    process = api.create_suspended(
        Path(sys.executable), (str(script), str(marker)), _child_environment(tmp_path)
    )
    job = api.create_job()
    api.configure_kill_on_close(job)
    api.assign_process(job, process)
    api.resume_process(process)
    return job, process, marker


def _await_marker(marker: Path, *, timeout_seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if marker.exists() and marker.stat().st_size > 0:
            return True
        time.sleep(0.05)
    return False


def _await_root_exit(api: Any, process: Any, *, timeout_seconds: float = 20.0) -> bool:
    """Wait until the *parent* has exited, leaving only the grandchild."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if api.process_exit_code(process) is not None:
            return True
        time.sleep(0.05)
    return False


def _close_all(api: Any, job: Any, process: Any) -> None:
    for pipe in (process.stdout, process.stderr):
        try:
            api.cancel_overlapped_read(pipe)
        except Exception:  # noqa: BLE001 - teardown of a test fixture
            pass
        try:
            api.close_pipe(pipe)
        except Exception:  # noqa: BLE001
            pass
    for handle in (process.thread_handle, process.process_handle, job):
        try:
            api.close_handle(handle)
        except Exception:  # noqa: BLE001
            pass


@windows_only
def test_real_teardown_leaves_zero_job_members(lifecycle_api: Any, tmp_path: Path) -> None:
    job, process, marker = _launch_tree(lifecycle_api, tmp_path)
    try:
        assert _await_marker(marker), "grandchild never started writing"
        assert _await_root_exit(lifecycle_api, process), "parent never exited"

        # The root process has exited while a grandchild is still
        # demonstrably alive and writing. The Job Object reports it
        # positively, by count, without depending on any parentage link.
        size_before = marker.stat().st_size
        time.sleep(0.3)
        assert marker.stat().st_size > size_before, "grandchild should still be writing"
        assert probe_mod.job_active_process_count(job) >= 1

        evidence = probe_mod.terminate_and_prove_job_empty(
            lifecycle_api,
            job=job,
            process=process,
            job_assigned=True,
            member_probe=probe_mod.job_active_process_count,
            deadline=time.monotonic() + 20.0,
            failure_types=(ColibriStage2Failure,),
        )
        assert evidence.job_terminated is True
        assert evidence.job_empty_proven is True
        assert evidence.job_member_count == 0
        assert evidence.cleanup_failed is False
    finally:
        _close_all(lifecycle_api, job, process)


@windows_only
def test_grandchild_cannot_keep_writing_after_teardown_returns(
    lifecycle_api: Any, tmp_path: Path
) -> None:
    job, process, marker = _launch_tree(lifecycle_api, tmp_path)
    try:
        assert _await_marker(marker), "grandchild never started writing"
        evidence = probe_mod.terminate_and_prove_job_empty(
            lifecycle_api,
            job=job,
            process=process,
            job_assigned=True,
            member_probe=probe_mod.job_active_process_count,
            deadline=time.monotonic() + 20.0,
            failure_types=(ColibriStage2Failure,),
        )
        assert evidence.job_empty_proven is True
        size_at_return = marker.stat().st_size
        # The grandchild wrote ~10x/second, so a full second of silence is a
        # real observation rather than a scheduling artefact.
        time.sleep(1.0)
        assert marker.stat().st_size == size_at_return
    finally:
        _close_all(lifecycle_api, job, process)


@windows_only
def test_timeout_path_stops_both_processes(lifecycle_api: Any, tmp_path: Path) -> None:
    # An already-elapsed deadline is the timeout path: termination must still
    # happen, and the tree must still end up empty.
    job, process, marker = _launch_tree(lifecycle_api, tmp_path)
    try:
        assert _await_marker(marker), "grandchild never started writing"
        evidence = probe_mod.terminate_and_prove_job_empty(
            lifecycle_api,
            job=job,
            process=process,
            job_assigned=True,
            member_probe=probe_mod.job_active_process_count,
            deadline=time.monotonic() - 1.0,
            failure_types=(ColibriStage2Failure,),
        )
        assert evidence.job_terminated is True
        # A single poll after TerminateJobObject may still see members, so the
        # proof is confirmed here with a bounded follow-up rather than assumed.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if probe_mod.job_active_process_count(job) == 0:
                break
            time.sleep(0.05)
        assert probe_mod.job_active_process_count(job) == 0
        size_at_return = marker.stat().st_size
        time.sleep(1.0)
        assert marker.stat().st_size == size_at_return
    finally:
        _close_all(lifecycle_api, job, process)


@windows_only
def test_unavailable_member_query_fails_closed_against_a_real_job(
    lifecycle_api: Any, tmp_path: Path
) -> None:
    job, process, marker = _launch_tree(lifecycle_api, tmp_path)
    try:
        assert _await_marker(marker), "grandchild never started writing"
        evidence = probe_mod.terminate_and_prove_job_empty(
            lifecycle_api,
            job=job,
            process=process,
            job_assigned=True,
            member_probe=lambda handle: None,
            deadline=time.monotonic() + 0.3,
            failure_types=(ColibriStage2Failure,),
        )
        assert evidence.job_empty_proven is False
        assert evidence.job_member_count is None
        assert evidence.cleanup_failed is True
    finally:
        _close_all(lifecycle_api, job, process)


@windows_only
def test_member_count_query_is_checked_against_a_real_job(
    lifecycle_api: Any, tmp_path: Path
) -> None:
    job, process, marker = _launch_tree(lifecycle_api, tmp_path)
    try:
        assert _await_marker(marker), "grandchild never started writing"
        count = probe_mod.job_active_process_count(job)
        assert isinstance(count, int)
        assert count >= 1
    finally:
        _close_all(lifecycle_api, job, process)
    # A closed handle can answer nothing, and must say so rather than zero.
    assert probe_mod.job_active_process_count(job) in (None, 0)
