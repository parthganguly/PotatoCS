"""Checked Job Object membership proof for Colibrì Stage 2.

Owning a native process tree means proving it is *gone*, not proving the root
process exited. A parent-PID descendant snapshot is the wrong instrument for
that, for two reasons:

- it is an absence-of-evidence argument, not a proof. "I walked the process
  list and found nothing descended from this PID" is not the same statement
  as "this tree holds zero processes";
- the ancestry link it walks is not reliable after the root exits. Windows
  does not clear a child's recorded parent PID when the parent dies, so the
  link survives as a *stale* number: it can be recycled onto an unrelated
  live process, and reachability from the root depends on which intermediate
  processes happen to still be alive when the snapshot is taken.

The Job Object answers directly. ``JOBOBJECT_BASIC_ACCOUNTING_INFORMATION``
carries ``ActiveProcesses`` for every process still in the job regardless of
parentage or of what has already exited, so a job reporting zero members is a
positive proof of an empty tree -- and it is the same object whose
kill-on-close semantics the teardown relies on. This module provides that
query, and the bounded teardown loop that waits on it under an absolute
deadline and fails closed when the answer is unavailable or never reaches
zero.

Nothing here launches a process, opens a file, or touches the network.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1

# Returns the number of processes still in the job, or ``None`` when the
# count could not be obtained. ``None`` is never treated as zero.
JobMemberProbe = Callable[[Any], "int | None"]

_JOB_EMPTY_POLL_SECONDS = 0.05


def _basic_accounting_information() -> Any:
    """A fresh ``JOBOBJECT_BASIC_ACCOUNTING_INFORMATION`` structure."""

    import ctypes
    from ctypes import wintypes

    class _LargeInteger(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_longlong)]

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", _LargeInteger),
            ("TotalKernelTime", _LargeInteger),
            ("ThisPeriodTotalUserTime", _LargeInteger),
            ("ThisPeriodTotalKernelTime", _LargeInteger),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    return _BasicAccountingInformation()


def job_active_process_count(job: Any) -> int | None:
    """Number of processes still in ``job``, or ``None`` if unobtainable.

    A checked query: the returned byte count is verified against the
    structure size, because a partially-filled structure would hand back a
    stale or zeroed ``ActiveProcesses`` that reads exactly like a proof of
    emptiness. ``None`` means "not known" and callers must fail closed on it
    rather than inferring zero.
    """

    if job is None or sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # argtypes are mandatory, not cosmetic: without them ctypes passes the
        # struct pointer as a 32-bit int, so the call can return TRUE while
        # having filled a truncated address.
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        information = _basic_accounting_information()
        returned = wintypes.DWORD(0)
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(int(job)),
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        ):
            return None
        if int(returned.value) != ctypes.sizeof(information):
            return None
        return int(information.ActiveProcesses)
    except (AttributeError, OSError, ValueError):
        return None


class _TerminationApi(Protocol):
    """The exact teardown surface the proof below needs."""

    def terminate_job(self, job: Any) -> None: ...
    def terminate_process(self, process: Any) -> None: ...
    def wait_process(self, process: Any, timeout_ms: int) -> bool: ...
    def descendant_process_ids(self, process_id: int) -> set[int]: ...


@dataclass(frozen=True, slots=True)
class JobTeardownEvidence:
    """Closed teardown evidence: booleans and small counts only."""

    job_terminated: bool
    job_member_count: int | None
    job_empty_proven: bool
    root_exit_confirmed: bool
    descendant_probe_conclusive: bool
    descendant_count: int | None
    orphan_detected: bool
    cleanup_failed: bool


def terminate_and_prove_job_empty(
    api: Any,
    *,
    job: Any,
    process: Any,
    job_assigned: bool,
    member_probe: JobMemberProbe,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    failure_types: tuple[type[BaseException], ...] = (),
) -> JobTeardownEvidence:
    """Terminate the whole job and prove, under ``deadline``, that it is empty.

    Order matters and is fixed:

    1. terminate the *complete* job whenever one exists and the child was
       assigned to it (falling back to terminating the lone process only when
       there is no job to terminate) -- this runs on every teardown path,
       success or failure;
    2. wait for the root process, bounded by the same absolute deadline;
    3. poll ``member_probe`` until the job reports **zero** members. This is
       the primary proof. An unavailable count, or a count that never reaches
       zero before the deadline, is a cleanup failure -- never an assumed
       zero;
    4. enumerate descendants as *supplementary* evidence only.

    The job handle is deliberately not closed here: the caller closes it only
    after this proof (and any resource measurement) has completed, since a
    closed handle can answer neither query.
    """

    caught = tuple(failure_types)
    job_terminated = False
    cleanup_failed = False

    try:
        if job is not None and job_assigned:
            api.terminate_job(job)
            job_terminated = True
        else:
            api.terminate_process(process)
    except caught:  # caller supplies its own closed failure types
        cleanup_failed = True

    root_exit_confirmed = False
    try:
        wait_ms = int(max(0.0, deadline - clock()) * 1000)
        root_exit_confirmed = bool(api.wait_process(process, min(5000, wait_ms)))
        if not root_exit_confirmed:
            cleanup_failed = True
    except caught:
        cleanup_failed = True

    # The zero-member proof. Polled rather than sampled once, because
    # termination is asynchronous: a job can still report members for a short
    # while after TerminateJobObject returns.
    member_count: int | None = None
    job_empty_proven = False
    while True:
        member_count = member_probe(job)
        if member_count == 0:
            job_empty_proven = True
            break
        if clock() >= deadline:
            break
        sleep(_JOB_EMPTY_POLL_SECONDS)
    if not job_empty_proven:
        cleanup_failed = True

    # Supplementary only: a descendant snapshot cannot prove emptiness, but a
    # descendant it *does* find is a real surviving process worth naming.
    descendant_count: int | None = None
    descendant_probe_conclusive = False
    orphan_detected = False
    try:
        descendants = api.descendant_process_ids(process.process_id)
        descendant_count = len(descendants)
        descendant_probe_conclusive = True
        orphan_detected = descendant_count > 0
    except caught:
        cleanup_failed = True

    return JobTeardownEvidence(
        job_terminated=job_terminated,
        job_member_count=member_count,
        job_empty_proven=job_empty_proven,
        root_exit_confirmed=root_exit_confirmed,
        descendant_probe_conclusive=descendant_probe_conclusive,
        descendant_count=descendant_count,
        orphan_detected=orphan_detected,
        cleanup_failed=cleanup_failed,
    )
