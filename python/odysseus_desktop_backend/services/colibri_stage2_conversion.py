"""Download/conversion orchestration and evidence capture for Colibrì
Stage 2A.

This module never downloads model weights and never runs a converter with
its default (real) adapters unless the approved-execution gate passes --
which still requires explicit ``--approve``, interactive stdin/stdout, an
isolated Python environment with torch and safetensors already installed,
safe existing parents, an absent or empty output root, and at least 18
GiB free, none of which this module supplies on its own. It provides:

* a process-free, network-free dry-run plan describing the sequential
  download -> verify -> convert -> delete-source-shard sequence;
* the closed, immutable reviewed *source* manifest gate (basename, exact
  size, and SHA-256 for each of the four required upstream files), which
  fails closed with ``source_model_manifest_unreviewed`` unless exactly
  those four entries are present and self-consistent;
* the approved-execution precondition gate;
* path-safety-checked transactional per-shard and per-config primitives
  the real approved sequence calls, exercised only with injected fakes in
  tests;
* default real adapters (a pinned-revision single-file downloader and a
  converter invoker) used only once a reviewed manifest and explicit
  approval are both present;
* safe resumability: a run interrupted at any point continues from what
  was already *proven*, never from what merely exists. Every reused file
  -- source shard, config, or converted artifact -- must first pass a
  complete identity proof (pinned basename, exact size, exact SHA-256,
  ordinary regular file, direct child, non-reparse). A partial file
  always fails that proof, so it is never trusted; an existing converted
  artifact is never overwritten;
* closed, distinguished conversion-failure evidence: a converter that
  timed out, one that exited nonzero, and one the OS killed with a native
  exception are three separate categories carrying only bounded numbers
  (return code, elapsed time, peak memory) -- never raw output, an
  environment value, a username, or a path;
* a closed, privacy-safe conversion capture shape distinct from
  ``OlmoeModelManifest`` -- it can never itself authorize inference.

The default converter is the in-repo memory-bounded
``colibri_stage2_bounded_convert``, which reproduces the pinned upstream
converter's quantization arithmetic exactly while bounding peak memory by
a chunk budget rather than by shard size. The unmodified upstream script
remains available behind ``--converter pinned-script``, but needs roughly
10 GiB of resident memory per shard and cannot complete on a 16 GiB host.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat as stat_module
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded_convert
from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services.colibri_stage2_common import (
    ALLOWED_CONVERSION_DEPENDENCY_NAMES,
    APPROVAL_STATEMENT,
    APPROX_DOWNLOAD_BYTES,
    CONVERSION_CAPTURE_SCHEMA_VERSION,
    CONVERSION_CAPTURE_STATE,
    CONVERTER_KIND_BOUNDED,
    CONVERTER_KIND_PINNED_SCRIPT,
    CONVERTER_KINDS,
    DEVIATION_STATEMENT,
    EXPECTED_CONFIG_BASENAME,
    EXPECTED_CONVERTER_SCRIPT_BASENAME,
    EXPECTED_SHARD_BASENAMES,
    PINNED_COLIBRI_COMMIT,
    PINNED_LICENSE_IDENTIFIER,
    PINNED_MODEL_REPOSITORY,
    PINNED_MODEL_REVISION,
    REQUIRED_FREE_SPACE_BYTES,
    RESUME_LEDGER_BASENAME,
    RESUME_LEDGER_SCHEMA_VERSION,
    ColibriStage2Failure,
    is_hex64,
    is_safe_basename,
    is_simple_version,
    reviewed_identity_for_converter_kind,
)
from odysseus_desktop_backend.services.colibri_stage2_path_safety import (
    atomic_no_replace_move,
    require_direct_child_path,
    require_ordinary_directory,
)

REQUIRED_SOURCE_FILES = ("config.json", *EXPECTED_SHARD_BASENAMES)

_MAX_CONFIG_SOURCE_BYTES = 1 * 1024 * 1024
_MAX_SHARD_SOURCE_BYTES = 20 * 1024 * 1024 * 1024

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _max_source_bytes(basename: str) -> int:
    return _MAX_CONFIG_SOURCE_BYTES if basename == EXPECTED_CONFIG_BASENAME else _MAX_SHARD_SOURCE_BYTES


# ---------------------------------------------------------------------------
# Blocker 2: closed reviewed *source* manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceShardEntry:
    """One immutable, reviewed identity for a single required upstream
    source file: its exact approved basename, exact positive size, and
    full lowercase SHA-256. Never constructible from a caller-supplied
    override -- the basename must be one of ``REQUIRED_SOURCE_FILES``."""

    basename: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if self.basename not in REQUIRED_SOURCE_FILES or not is_safe_basename(self.basename):
            raise ValueError("source shard entry basename is not one of the required source files")
        maximum = _max_source_bytes(self.basename)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or not 0 < self.size_bytes <= maximum:
            raise ValueError("source shard entry size_bytes is out of bounds")
        if not is_hex64(self.sha256):
            raise ValueError("source shard entry sha256 is not a lowercase SHA-256")


# Immutable and, as of this commit, populated: the complete reviewed
# upstream identities (exact basename + exact size + SHA-256) for
# config.json and the three safetensors shards of
# allenai/OLMoE-1B-7B-0125-Instruct at the immutable, Apache-2.0-licensed
# revision b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e.
#
# Capture method: an ``olmoe_source_manifest_capture`` evidence capture
# (state ``unreviewed_source_manifest_capture``) confirmed the immutable
# revision matched, the exact required file set matched, and no
# safetensor body was requested -- only ``config.json`` content was
# fetched. The three safetensor identities (basename, exact size, and
# SHA-256) came from that same immutable revision's LFS metadata; no
# safetensor body was downloaded. Exact total source size across all
# four files: 13,838,722,788 bytes. Reviewed and committed 2026-07-24.
#
# Values below are hardcoded from that reviewed capture -- never taken
# from an environment variable, CLI argument, JSON file, network
# response, caller-provided mapping, regex, or alternate revision.
REVIEWED_SOURCE_SHARD_MANIFEST: Mapping[str, SourceShardEntry] = MappingProxyType(
    {
        "config.json": SourceShardEntry(
            basename="config.json",
            size_bytes=828,
            sha256="272998dd7ba4846dcc682f0b5a46144f4bcd9dde8e94d2f17bd8e5cf2f23d6ce",
        ),
        "model-00001-of-00003.safetensors": SourceShardEntry(
            basename="model-00001-of-00003.safetensors",
            size_bytes=4997744872,
            sha256="61874210ca7c360f43f8c622cecc12441083d40190eae3b56bc9d6e1c0a30c1e",
        ),
        "model-00002-of-00003.safetensors": SourceShardEntry(
            basename="model-00002-of-00003.safetensors",
            size_bytes=4997235176,
            sha256="c523a43b8a17269d5fab33395048a83633f4d1d89c1958570cea738e2bbe80c9",
        ),
        "model-00003-of-00003.safetensors": SourceShardEntry(
            basename="model-00003-of-00003.safetensors",
            size_bytes=3843741912,
            sha256="97ae01e3519c52e63a018bca96ab17a89c4cd5cab1c6d742efed0fa5c0e2bb17",
        ),
    }
)


def require_reviewed_source_manifest() -> Mapping[str, SourceShardEntry]:
    """Fail closed unless exactly the four required source files have a
    fully reviewed entry: exact basename, exact positive size, and a
    lowercase SHA-256 -- each entry keyed by (and matching) its own
    basename."""

    registry = REVIEWED_SOURCE_SHARD_MANIFEST
    if not isinstance(registry, Mapping) or set(registry) != set(REQUIRED_SOURCE_FILES):
        raise ColibriStage2Failure("source_model_manifest_unreviewed")
    for basename, entry in registry.items():
        if not isinstance(entry, SourceShardEntry) or entry.basename != basename:
            raise ColibriStage2Failure("source_model_manifest_unreviewed")
    return registry


# ---------------------------------------------------------------------------
# Dry-run: process-free, network-free
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DryRunPlan:
    """Everything a human operator must see before ever approving a run."""

    model_repository: str
    model_revision: str
    license_identifier: str
    required_source_files: tuple[str, ...]
    approx_download_bytes: int
    required_free_space_bytes: int
    destination: str
    steps: tuple[str, ...]
    deviation_statement: str
    approval_statement: str


def build_dry_run_plan(destination: Path) -> DryRunPlan:
    """Pure, process-free, network-free: touches no filesystem or socket."""

    return DryRunPlan(
        model_repository=PINNED_MODEL_REPOSITORY,
        model_revision=PINNED_MODEL_REVISION,
        license_identifier=PINNED_LICENSE_IDENTIFIER,
        required_source_files=REQUIRED_SOURCE_FILES,
        approx_download_bytes=APPROX_DOWNLOAD_BYTES,
        required_free_space_bytes=REQUIRED_FREE_SPACE_BYTES,
        destination=str(destination),
        steps=(
            "download config.json once from the immutable revision and keep it available",
            "reuse any already-present source or converted file only after its exact "
            "pinned basename, size, and SHA-256 all verify; never trust a partial file",
            "for each of the three shards, in order: download and verify only that shard",
            "create a new empty per-shard converter-output directory",
            "run the memory-bounded converter through the current venv Python as a child "
            "process, with peak memory bounded by a chunk budget rather than by shard size",
            "verify the temporary output contains exactly config.json and that one converted shard",
            "verify the converted config remains byte-identical, then hash and record the converted shard",
            "atomically move the converted shard into the final directory only if absent",
            "record the proven converted shard identity in the resume ledger immediately",
            "delete the corresponding source shard only after the above succeeds, and verify the deletion",
            "remove and verify removal of the per-shard temporary output",
            "copy the verified config into the final directory exactly once, after every shard succeeds",
        ),
        deviation_statement=DEVIATION_STATEMENT,
        approval_statement=APPROVAL_STATEMENT,
    )


# ---------------------------------------------------------------------------
# Approved-mode preconditions
# ---------------------------------------------------------------------------


def check_approved_preconditions(
    *,
    interactive_check: Callable[[], bool],
    approved: bool,
    destination_dir: Path,
    converted_dir: Path,
    free_bytes_probe: Callable[[Path], int],
    isolated_python_env_ready: bool,
    dependency_versions: Mapping[str, str],
    allow_resume: bool = False,
) -> Mapping[str, SourceShardEntry]:
    """The full approved-mode gate, checked before any network activity.

    Returns the reviewed source manifest on success. Every check here runs
    before a single byte is downloaded, and while the reviewed source
    manifest stays empty, no other check result can ever unblock a run.

    ``allow_resume`` relaxes exactly one check -- the "roots must be
    absent or empty" precondition -- and nothing else. It never weakens
    any identity proof: a resumed run still verifies every reused file's
    basename, exact size, and SHA-256 before touching it. Non-resume runs
    keep the original strict behaviour, so the default is unchanged.
    """

    reviewed = require_reviewed_source_manifest()
    if not approved or not interactive_check():
        raise ColibriStage2Failure("noninteractive_approval_rejected")
    if not allow_resume:
        for directory in (destination_dir, converted_dir):
            if directory.exists() and any(directory.iterdir()):
                raise ColibriStage2Failure("destination_not_empty")
    if free_bytes_probe(destination_dir) < REQUIRED_FREE_SPACE_BYTES:
        raise ColibriStage2Failure("insufficient_disk_space")
    if not isolated_python_env_ready:
        raise ColibriStage2Failure("python_environment_unavailable")
    missing_dependencies = {"torch", "safetensors"} - set(dependency_versions)
    if missing_dependencies:
        raise ColibriStage2Failure("dependency_unavailable")
    for name, version in dependency_versions.items():
        if not is_safe_basename(name) or not is_simple_version(version):
            raise ColibriStage2Failure("dependency_unavailable")
    return reviewed


# ---------------------------------------------------------------------------
# Real adapters (downloader / converter) -- default, reviewable
# ---------------------------------------------------------------------------


class Downloader(Protocol):
    def download(
        self, *, basename: str, expected_size_bytes: int, expected_sha256: str, destination: Path
    ) -> None: ...


class Converter(Protocol):
    """A converter may return bounded run evidence, or ``None`` when it has
    none to offer (every synthetic test fake takes the latter path)."""

    def convert(self, *, model_dir: Path, output_dir: Path) -> ConversionRunEvidence | None: ...


@dataclass(frozen=True, slots=True)
class PinnedRevisionFileDownloader:
    """Default real single-file downloader.

    Requests only the exact pinned repository, revision, and basename --
    there is no parameter anywhere on this class that could ever select a
    different repository or an unpinned "latest" revision. Streams to a
    direct-child ``<basename>.partial`` file while counting bytes and
    hashing, bounded by both a per-read socket timeout and one absolute
    file deadline, and atomically renames into place only after the exact
    reviewed size and SHA-256 both verify. A failed attempt always deletes
    its own partial file.
    """

    socket_timeout_seconds: float = 30.0
    absolute_deadline_seconds: float = 1800.0
    clock: Callable[[], float] = time.monotonic

    def download(
        self, *, basename: str, expected_size_bytes: int, expected_sha256: str, destination: Path
    ) -> None:
        import urllib.request

        if not is_safe_basename(basename):
            raise ColibriStage2Failure("unsafe_basename_rejected")

        partial_basename = f"{basename}.partial"
        if not is_safe_basename(partial_basename):
            raise ColibriStage2Failure("unsafe_basename_rejected")
        resolved_parent = require_ordinary_directory(
            destination.parent, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
        )
        partial_path = require_direct_child_path(
            resolved_parent, partial_basename, category="unsafe_directory_rejected"
        )

        url = (
            f"https://huggingface.co/{PINNED_MODEL_REPOSITORY}/resolve/"
            f"{PINNED_MODEL_REVISION}/{basename}"
        )
        digest = hashlib.sha256()
        observed = 0
        started = self.clock()

        # Exclusive creation ("xb", never "wb") is the whole safety
        # property here: it fails closed if a stale or race-created
        # partial already exists, and guarantees that any partial file
        # this method later deletes is one it created itself -- a partial
        # not owned by this run is never opened, truncated, or removed.
        try:
            handle = partial_path.open("xb")
        except FileExistsError as exc:
            raise ColibriStage2Failure("partial_already_exists") from exc
        except OSError as exc:
            raise ColibriStage2Failure("shard_download_failed") from exc

        try:
            with handle:
                request = urllib.request.Request(url, headers={"User-Agent": "odysseus-colibri-stage2"})
                with urllib.request.urlopen(request, timeout=self.socket_timeout_seconds) as response:
                    while True:
                        if self.clock() - started > self.absolute_deadline_seconds:
                            raise ColibriStage2Failure("shard_download_failed")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        observed += len(chunk)
                        if observed > expected_size_bytes:
                            raise ColibriStage2Failure("shard_verification_failed")
                        digest.update(chunk)
                        handle.write(chunk)
        except ColibriStage2Failure:
            partial_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            partial_path.unlink(missing_ok=True)
            raise ColibriStage2Failure("shard_download_failed") from exc

        if observed != expected_size_bytes or digest.hexdigest() != expected_sha256:
            partial_path.unlink(missing_ok=True)
            raise ColibriStage2Failure("shard_verification_failed")
        try:
            atomic_no_replace_move(partial_path, destination, exists_category="shard_download_failed")
        except ColibriStage2Failure:
            partial_path.unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Closed, privacy-safe converter-process evidence
# ---------------------------------------------------------------------------


MEMORY_ACCOUNTING_MEASURED = "measured"
MEMORY_ACCOUNTING_UNAVAILABLE = "unavailable"
MEMORY_ACCOUNTING_STATES = frozenset({MEMORY_ACCOUNTING_MEASURED, MEMORY_ACCOUNTING_UNAVAILABLE})


@dataclass(frozen=True, slots=True)
class ConversionRunEvidence:
    """Bounded, structured evidence about one converter child process.

    Only numbers: how long it ran and how much memory it peaked at. Never
    stdout, never stderr, never an environment value, never a path.

    ``peak_memory_state`` is the honesty boundary. Peak memory is
    whole-*tree* evidence only when the child was positively confirmed to
    have joined the accounting job: a job the process never joined still
    answers queries, with small plausible numbers describing an empty job
    rather than the converter. So the peaks are populated **only** in the
    ``measured`` state; in the ``unavailable`` state both are ``None`` and
    no memory claim is made at all. A number is never emitted as proven
    whole-tree evidence unless it actually is one.

    Neither field is ever a pass/fail input.
    """

    elapsed_ms: int
    peak_memory_bytes: int | None
    peak_commit_bytes: int | None
    peak_memory_state: str = MEMORY_ACCOUNTING_UNAVAILABLE
    # Which reviewed converter actually ran. Set by the adapter itself,
    # never by a caller of the adapter, and resolved to an identity only
    # through the closed ``REVIEWED_CONVERTER_IDENTITY_BY_KIND`` mapping.
    converter_kind: str | None = None

    def __post_init__(self) -> None:
        if self.peak_memory_state not in MEMORY_ACCOUNTING_STATES:
            raise ValueError("unknown conversion memory accounting state")
        if self.converter_kind is not None and self.converter_kind not in CONVERTER_KINDS:
            raise ValueError("unknown converter kind")
        if self.peak_memory_state == MEMORY_ACCOUNTING_UNAVAILABLE and not (
            self.peak_memory_bytes is None and self.peak_commit_bytes is None
        ):
            raise ValueError("unavailable memory accounting must not carry peak values")


def classify_process_exit(returncode: int) -> tuple[str, dict[str, int]]:
    """Turn a raw child return code into a closed category plus numbers.

    Three outcomes that the previous single ``conversion_failed`` category
    could not tell apart:

    * ``ok`` -- clean exit;
    * ``conversion_process_crashed`` -- the OS killed it. On Windows a
      process terminated by an unhandled native exception reports the
      NTSTATUS as its exit code with the severity bits set, so the real
      access violation observed on the target host surfaces as
      ``0xc0000005`` (3221225477), not as some ordinary small exit code.
      On POSIX a signalled child reports a negative return code.
    * ``conversion_nonzero_exit`` -- it exited under its own control with
      a nonzero status (a bad argument, a missing dependency, an
      unreadable shard).
    """

    if returncode == 0:
        return "ok", {}
    if returncode < 0:
        return "conversion_process_crashed", {"exit_code": -returncode}
    unsigned = returncode & 0xFFFFFFFF
    if unsigned & 0x80000000:
        return "conversion_process_crashed", {"win32_code": unsigned}
    return "conversion_nonzero_exit", {"exit_code": unsigned}


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


_CREATE_SUSPENDED = 0x00000004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_MAX_TRACKED_JOB_PROCESSES = 512


def _kernel32() -> Any:
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _create_owning_job() -> Any:
    """A Windows Job Object that *owns* the converter's whole process tree.

    Two jobs' worth of duty in one object:

    * **Ownership.** The converter is created suspended and assigned to
      this job before it is allowed to execute a single instruction, so
      every process it later spawns is born inside the job. That matters
      concretely here: a virtual environment's ``Scripts\\python.exe`` is
      a redirector stub that runs the real interpreter as a *grandchild*,
      so killing only the handle ``Popen`` returned would leave the actual
      converter alive -- still holding memory, still writing into the
      temporary output directory that cleanup is about to remove.
    * **Accounting.** A job's peak counters cover the whole tree and stay
      readable after every member has exited, which is exactly the case
      that matters when a converter is killed for exhausting memory.

    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` is configured as the final
    safeguard: if this process dies unexpectedly, or any teardown path is
    missed, closing the last handle to the job kills whatever is still
    inside it.

    Returns ``None`` on any failure. A ``None`` job means the run must not
    proceed on Windows -- an unowned converter is exactly what this
    exists to prevent.
    """

    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        information = _extended_limit_information()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(int(job)),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            # Without kill-on-close the job cannot guarantee cleanup, so
            # it is not fit for purpose -- discard it rather than proceed
            # with a weaker guarantee than advertised.
            _close_job(job)
            return None
        return job
    except (AttributeError, OSError, ValueError):
        return None


def _resume_process_tree(process_id: int) -> bool:
    """Resume every thread of the suspended process, returning success.

    ``subprocess.Popen`` closes the initial thread handle before it
    returns, so the thread is reached by enumerating the process's threads
    rather than by keeping that handle. A newly created suspended process
    has exactly one thread; resuming every thread it owns is therefore
    both sufficient and precise.
    """

    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        class _ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", ctypes.c_long),
                ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = _kernel32()
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE

        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return False
        resumed = 0
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
                return False
            while True:
                if int(entry.th32OwnerProcessID) == int(process_id):
                    thread = kernel32.OpenThread(
                        _THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                    )
                    if thread:
                        try:
                            if kernel32.ResumeThread(wintypes.HANDLE(int(thread))) != 0xFFFFFFFF:
                                resumed += 1
                        finally:
                            kernel32.CloseHandle(wintypes.HANDLE(int(thread)))
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(int(snapshot)))
        return resumed > 0
    except (AttributeError, OSError, ValueError):
        return False


def _job_process_count(job: Any) -> int | None:
    """How many processes the job still contains, or ``None`` if unknown."""

    if job is None or sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicProcessIdList(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", wintypes.DWORD),
                ("NumberOfProcessIdsInList", wintypes.DWORD),
                ("ProcessIdList", ctypes.c_size_t * _MAX_TRACKED_JOB_PROCESSES),
            ]

        kernel32 = _kernel32()
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        listing = _BasicProcessIdList()
        returned = wintypes.DWORD(0)
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(int(job)),
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(listing),
            ctypes.sizeof(listing),
            ctypes.byref(returned),
        ):
            return None
        return int(listing.NumberOfAssignedProcesses)
    except (AttributeError, OSError, ValueError):
        return None


def _terminate_job_tree(job: Any) -> None:
    if job is None or sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, ctypes.c_uint]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject(wintypes.HANDLE(int(job)), 1)
    except (AttributeError, OSError, ValueError):
        return


def _await_empty_job(
    job: Any, *, deadline_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic
) -> bool:
    """Wait until the job holds no processes, returning whether it emptied.

    This is the "verify no converter member survives" step: terminating a
    job is asynchronous, and returning before every member has actually
    gone would hand the caller a directory a dying grandchild can still
    write into.
    """

    if job is None or sys.platform != "win32":
        return True
    started = clock()
    while True:
        remaining = _job_process_count(job)
        if remaining == 0:
            return True
        if remaining is None:
            # The count is unknowable; do not claim the tree is gone.
            return False
        if clock() - started > deadline_seconds:
            return False
        time.sleep(0.05)


def _assign_process_to_job(job: Any, process: Any) -> bool:
    """Assign ``process`` to ``job``, returning whether that *succeeded*.

    The return value is the whole point. If assignment fails, the job
    contains nothing, and its peak counters describe an empty job -- they
    would read as small, plausible numbers that are not measurements of
    the converter at all. Reporting those as whole-tree evidence would be
    worse than reporting nothing, so every caller must gate on this.
    """

    if job is None or sys.platform != "win32":
        return False
    handle = getattr(process, "_handle", None)
    if handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        if not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(int(job)), wintypes.HANDLE(int(handle))
        ):
            return False

        # Assignment reporting success is not by itself proof of
        # membership -- confirm it with the kernel before the process is
        # ever allowed to run.
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_int),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        member = ctypes.c_int(0)
        if not kernel32.IsProcessInJob(
            wintypes.HANDLE(int(handle)), wintypes.HANDLE(int(job)), ctypes.byref(member)
        ):
            return False
        return bool(member.value)
    except (AttributeError, OSError, ValueError):
        return False


def _extended_limit_information() -> Any:
    """A fresh ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` structure."""

    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return _ExtendedLimitInformation()


def _peak_job_memory(job: Any) -> tuple[int | None, int | None]:
    """Best-effort ``(peak per-process, peak whole-job)`` memory in bytes.

    Both are plain byte counts; neither can carry a path, an environment
    value, or any converter output. Any failure yields ``(None, None)``.
    """

    if job is None or sys.platform != "win32":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        # argtypes are mandatory, not cosmetic: without them ctypes passes
        # the struct pointer as a 32-bit int, so the call still returns
        # TRUE while filling a truncated address and reporting nonsense.
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        information = _extended_limit_information()
        returned = wintypes.DWORD(0)
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(int(job)),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        ):
            return None, None
        return int(information.PeakProcessMemoryUsed), int(information.PeakJobMemoryUsed)
    except (AttributeError, OSError, ValueError):
        return None, None


def _close_job(job: Any) -> None:
    if job is None or sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(int(job)))
    except (AttributeError, OSError, ValueError):
        return


def _bounded_metadata(
    peak_memory_bytes: int | None, peak_commit_bytes: int | None
) -> dict[str, int]:
    metadata: dict[str, int] = {}
    if isinstance(peak_memory_bytes, int) and peak_memory_bytes >= 0:
        metadata["peak_memory_bytes"] = peak_memory_bytes
    if isinstance(peak_commit_bytes, int) and peak_commit_bytes >= 0:
        metadata["peak_commit_bytes"] = peak_commit_bytes
    return metadata


def run_converter_child(
    argv: Sequence[str],
    *,
    deadline_seconds: float,
    converter_kind: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ConversionRunEvidence:
    """Run one converter child process, owning its entire process tree.

    Explicit argv, ``shell=False``, and ``DEVNULL`` on all three standard
    streams: the converter's own text is never needed for pass/fail and
    must never reach an evidence capture. What is retained instead is the
    closed, numeric outcome -- category, return code, elapsed time, and
    peak memory.

    Ownership on Windows is established *before the converter is allowed
    to execute at all*:

    1. create the process suspended, so it has run no instruction yet;
    2. assign it to a kill-on-close Job Object and confirm membership with
       ``IsProcessInJob``;
    3. only then resume it.

    If assignment cannot be confirmed the process is killed while still
    suspended and the run fails closed -- an unowned converter is never
    permitted to run, because the thing that must be preventable is
    precisely a converter nobody can reliably kill. Because the assignment
    happens before the first instruction, every process the converter
    later spawns is born inside the job; this is what makes the venv
    launcher's grandchild interpreter owned rather than orphaned.

    On timeout the *whole job* is terminated, not just the launcher, and
    this waits for every member to actually exit before returning. That
    ordering is load-bearing: the caller removes the temporary output
    directory next, and a surviving grandchild would still be writing
    into it.

    On POSIX the equivalent guarantee comes from a dedicated process
    group: the child starts its own session and the timeout path signals
    the entire group.
    """

    import subprocess

    if converter_kind is not None and converter_kind not in CONVERTER_KINDS:
        raise ValueError("unknown converter kind")

    started = clock()
    on_windows = sys.platform == "win32"
    job = _create_owning_job()
    if on_windows and job is None:
        # No job means no ownership, and no ownership means a converter
        # that could survive its own timeout.
        raise ColibriStage2Failure("job_create_failed")

    popen_kwargs: dict[str, Any] = {
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if on_windows:
        popen_kwargs["creationflags"] = _CREATE_SUSPENDED
    else:
        # A new session gives the child its own process group, so the
        # timeout path can signal the whole tree rather than one process.
        popen_kwargs["start_new_session"] = True

    try:
        try:
            process = subprocess.Popen(list(argv), **popen_kwargs)
        except (OSError, ValueError) as exc:
            raise ColibriStage2Failure("conversion_failed") from exc

        assigned = True
        if on_windows:
            assigned = _assign_process_to_job(job, process)
            if not assigned:
                # Still suspended: it has executed nothing and spawned
                # nothing. Kill it before it ever can.
                _kill_unowned_process(process)
                raise ColibriStage2Failure("job_assignment_failed")
            if not _resume_process_tree(process.pid):
                _terminate_job_tree(job)
                _await_empty_job(job)
                raise ColibriStage2Failure("process_resume_failed")

        timed_out = False
        try:
            returncode = process.wait(timeout=deadline_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = _terminate_process_tree(process, job, on_windows=on_windows)
        except OSError as exc:
            _terminate_process_tree(process, job, on_windows=on_windows)
            raise ColibriStage2Failure("conversion_failed") from exc

        # Read the peaks before the job handle is closed, and only when
        # membership was confirmed: an unassigned job still answers
        # queries, with numbers describing an empty job rather than the
        # converter.
        if assigned:
            peak_memory_bytes, peak_commit_bytes = _peak_job_memory(job)
        else:
            peak_memory_bytes, peak_commit_bytes = None, None
    finally:
        # Closing the last handle to a kill-on-close job is the final
        # safeguard for anything still alive on any path above.
        _close_job(job)

    # The query itself can still fail after a confirmed assignment, so the
    # state is derived from what was actually obtained, never from intent.
    measured = peak_memory_bytes is not None or peak_commit_bytes is not None
    peak_memory_state = MEMORY_ACCOUNTING_MEASURED if measured else MEMORY_ACCOUNTING_UNAVAILABLE
    if not measured:
        peak_memory_bytes = peak_commit_bytes = None

    elapsed_ms = max(0, round((clock() - started) * 1000))
    metadata = _bounded_metadata(peak_memory_bytes, peak_commit_bytes)

    if timed_out:
        raise ColibriStage2Failure(
            "conversion_timeout",
            elapsed_ms=elapsed_ms,
            timeout_ms=max(0, round(deadline_seconds * 1000)),
            **metadata,
        )
    category, exit_metadata = classify_process_exit(returncode)
    if category != "ok":
        raise ColibriStage2Failure(category, elapsed_ms=elapsed_ms, **exit_metadata, **metadata)
    return ConversionRunEvidence(
        elapsed_ms=elapsed_ms,
        peak_memory_bytes=peak_memory_bytes,
        peak_commit_bytes=peak_commit_bytes,
        peak_memory_state=peak_memory_state,
        converter_kind=converter_kind,
    )


def _kill_unowned_process(process: Any) -> None:
    """Destroy a process that could not be brought under ownership."""

    try:
        process.kill()
        process.wait()
    except OSError:
        return


def _terminate_process_tree(process: Any, job: Any, *, on_windows: bool) -> int:
    """Terminate the converter's whole tree and wait for it to be gone.

    Returns the launcher's return code. On Windows the job is terminated
    (covering the grandchild interpreter) and this blocks until the job
    reports no remaining members; on POSIX the child's process group is
    signalled.
    """

    if on_windows:
        _terminate_job_tree(job)
    else:
        import os
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                process.kill()
            except OSError:
                pass

    try:
        returncode = process.wait()
    except OSError:
        returncode = -1

    if on_windows and not _await_empty_job(job):
        # The tree could not be proven gone, so the caller must not go on
        # to delete a directory something may still be writing into.
        raise ColibriStage2Failure("cleanup_failed")
    return returncode


def require_reviewed_converter_identity(script_path: Path) -> None:
    """Fail closed unless ``script_path`` is an absolute path to exactly
    the one reviewed converter: an ordinary, non-reparse regular file,
    inside a directory chain proven free of symlinks/junctions/reparse
    points all the way to its drive/root anchor, with the exact reviewed
    basename, exact size, and exact SHA-256 -- compared only against the
    fixed ``common.REVIEWED_CONVERTER_IDENTITY``. A caller-supplied
    expected hash is never trusted; there is no parameter anywhere that
    could substitute one. Called both as a general precondition and
    again, immediately before every subprocess creation, by
    ``PinnedScriptConverter.convert``.
    """

    identity = common.REVIEWED_CONVERTER_IDENTITY
    if not script_path.is_absolute():
        raise ColibriStage2Failure("unsafe_directory_rejected")
    if script_path.name != identity.basename:
        raise ColibriStage2Failure("conversion_failed")

    resolved_parent = require_ordinary_directory(
        script_path.parent, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    resolved_script_path = require_direct_child_path(
        resolved_parent, script_path.name, category="unsafe_directory_rejected"
    )
    if not _is_regular_no_reparse(resolved_script_path):
        raise ColibriStage2Failure("conversion_failed")

    try:
        data = resolved_script_path.read_bytes()
    except OSError as exc:
        raise ColibriStage2Failure("conversion_failed") from exc
    if len(data) != identity.size_bytes or hashlib.sha256(data).hexdigest() != identity.sha256:
        raise ColibriStage2Failure("conversion_failed")


@dataclass(frozen=True, slots=True)
class PinnedScriptConverter:
    """Default real converter adapter.

    Invokes the exact pinned ``convert_olmoe.py`` through the *current*
    venv's Python interpreter (``sys.executable``), with explicit argv and
    ``shell=False``, under one absolute deadline. stdout/stderr are
    discarded rather than retained -- their content is never needed for
    pass/fail and must never appear in any evidence capture. The exact
    upstream argument grammar is ``--model <source> --out <output>`` --
    never ``--output`` and never ``--repo``.
    """

    converter_script_path: Path
    absolute_deadline_seconds: float = 1800.0

    # Fixed on the class: this adapter can only ever run the pinned
    # upstream script, so it can only ever report that identity.
    converter_kind: str = CONVERTER_KIND_PINNED_SCRIPT

    def convert(self, *, model_dir: Path, output_dir: Path) -> ConversionRunEvidence:
        require_reviewed_converter_identity(self.converter_script_path)
        argv = [
            sys.executable,
            str(self.converter_script_path),
            "--model",
            str(model_dir),
            "--out",
            str(output_dir),
        ]
        return run_converter_child(
            argv,
            deadline_seconds=self.absolute_deadline_seconds,
            converter_kind=CONVERTER_KIND_PINNED_SCRIPT,
        )


def bounded_converter_script_path() -> Path:
    """The one in-repo bounded converter, located from the imported module
    itself, and proven to be exactly the reviewed file.

    There is deliberately no parameter, environment variable, or argument
    anywhere that could point this at a different script: it is whatever
    ``colibri_stage2_bounded_convert`` this process already imported.

    Locating it safely is necessary but *not* sufficient. A path-safety
    proof establishes only where a file is, never what it contains -- a
    working tree can be edited and a reviewed file can be patched after
    review. So the located file is then held to
    ``common.REVIEWED_BOUNDED_CONVERTER_IDENTITY``: exact basename, exact
    size, exact SHA-256 over its full bytes. A caller-supplied expected
    hash is never accepted; there is no parameter that could supply one.
    """

    module_file = getattr(bounded_convert, "__file__", None)
    if not module_file:
        raise ColibriStage2Failure("conversion_failed")
    return require_reviewed_bounded_converter_identity(Path(module_file).resolve())


def require_reviewed_bounded_converter_identity(script_path: Path) -> Path:
    """Fail closed unless ``script_path`` is exactly the one reviewed
    bounded converter.

    The same proof ``require_reviewed_converter_identity`` applies to the
    pinned upstream script: an absolute path to an ordinary, non-reparse
    regular file, inside a directory chain proven free of
    symlinks/junctions/reparse points down to its drive/root anchor, with
    the exact reviewed basename, exact reviewed size, and exact reviewed
    SHA-256 -- compared only against the fixed
    ``common.REVIEWED_BOUNDED_CONVERTER_IDENTITY``.

    Called both as a CLI precondition and again, immediately before every
    subprocess creation, by ``BoundedScriptConverter.convert``, so a file
    edited between two launches is rejected at the second one rather than
    silently trusted from a first-call result.

    Returns the resolved, verified path.
    """

    identity = common.REVIEWED_BOUNDED_CONVERTER_IDENTITY
    if not script_path.is_absolute():
        raise ColibriStage2Failure("unsafe_directory_rejected")
    if script_path.name != identity.basename:
        raise ColibriStage2Failure("conversion_failed")

    resolved_parent = require_ordinary_directory(
        script_path.parent,
        missing_category="unsafe_directory_rejected",
        reparse_category="unsafe_directory_rejected",
    )
    resolved_script = require_direct_child_path(
        resolved_parent, script_path.name, category="unsafe_directory_rejected"
    )
    if not _is_regular_no_reparse(resolved_script):
        raise ColibriStage2Failure("conversion_failed")

    # Size is checked before the bytes are read, so a wildly oversized
    # file is rejected without being pulled into memory.
    try:
        if resolved_script.stat().st_size != identity.size_bytes:
            raise ColibriStage2Failure("conversion_failed")
        data = resolved_script.read_bytes()
    except OSError as exc:
        raise ColibriStage2Failure("conversion_failed") from exc
    if len(data) != identity.size_bytes or hashlib.sha256(data).hexdigest() != identity.sha256:
        raise ColibriStage2Failure("conversion_failed")
    return resolved_script


@dataclass(frozen=True, slots=True)
class BoundedScriptConverter:
    """The default real converter for a 16 GiB host.

    Runs the in-repo ``colibri_stage2_bounded_convert`` script -- which
    reproduces the pinned converter's quantization arithmetic exactly and
    writes a byte-identical safetensors artifact, but with peak memory
    bounded by ``chunk_target_bytes`` instead of by the shard size -- in a
    child process, through the same explicit-argv, ``shell=False``,
    deadlined, output-discarding path as ``PinnedScriptConverter``.

    Running it as a child rather than in-process is deliberate: the
    failure being fixed here killed its process with a native access
    violation inside ``torch_cpu.dll``, and an orchestrator that dies with
    its converter cannot record why it died or leave a resumable state
    behind.

    The reviewed-identity proof is re-run immediately before every launch,
    never cached from a previous call.
    """

    chunk_target_bytes: int = bounded_convert.DEFAULT_CHUNK_TARGET_BYTES
    absolute_deadline_seconds: float = 1800.0

    # Fixed on the class: this adapter can only ever run the in-repo
    # bounded converter, so it can only ever report that identity.
    converter_kind: str = CONVERTER_KIND_BOUNDED

    def convert(self, *, model_dir: Path, output_dir: Path) -> ConversionRunEvidence:
        # Re-verified here, immediately before argv is built and the
        # subprocess is created -- a converter edited between two launches
        # must be rejected at the second one.
        script_path = bounded_converter_script_path()
        argv = [
            sys.executable,
            str(script_path),
            "--model",
            str(model_dir),
            "--out",
            str(output_dir),
            "--chunk-bytes",
            str(int(self.chunk_target_bytes)),
        ]
        return run_converter_child(
            argv,
            deadline_seconds=self.absolute_deadline_seconds,
            converter_kind=CONVERTER_KIND_BOUNDED,
        )


# ---------------------------------------------------------------------------
# Resume ledger: the recorded identity of already-converted artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConvertedShardRecord:
    """The recorded identity of one already-converted shard.

    ``converter_kind`` records *which* reviewed converter produced the
    artifact, so a resumed run reports the identity of the converter that
    actually created it rather than whichever converter this run happens
    to be configured with. A resumed run may legitimately use a different
    converter than the interrupted one -- switching from the upstream
    script to the bounded converter is exactly why this PR exists -- and
    the capture has to stay truthful about both.
    """

    basename: str
    size_bytes: int
    sha256: str
    converter_kind: str

    def __post_init__(self) -> None:
        if self.basename not in EXPECTED_SHARD_BASENAMES:
            raise ValueError("converted shard record basename is not a pinned shard")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 < self.size_bytes <= _MAX_SHARD_SOURCE_BYTES
        ):
            raise ValueError("converted shard record size_bytes is out of bounds")
        if not is_hex64(self.sha256):
            raise ValueError("converted shard record sha256 is not a lowercase SHA-256")
        if self.converter_kind not in CONVERTER_KINDS:
            raise ValueError("converted shard record converter_kind is not a reviewed converter")


def read_resume_ledger(converted_dir: Path) -> dict[str, ConvertedShardRecord]:
    """Read the resume ledger, or return an empty mapping when absent.

    The ledger records nothing but pinned basenames, sizes, and digests --
    no path, no username, no environment value, no timing. It is treated
    as a *hint*, never as authority: a recorded shard is still re-verified
    against the file on disk before anything is reused, so a tampered or
    stale ledger can at worst cause redundant work, never a wrong reuse. A
    malformed ledger fails closed rather than being silently discarded.
    """

    resolved_dir = require_ordinary_directory(
        converted_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    ledger_path = require_direct_child_path(
        resolved_dir, RESUME_LEDGER_BASENAME, category="unsafe_directory_rejected"
    )
    if not ledger_path.exists():
        return {}
    if not _is_regular_no_reparse(ledger_path):
        raise ColibriStage2Failure("resume_state_invalid")
    try:
        document = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ColibriStage2Failure("resume_state_invalid") from exc

    if not isinstance(document, dict):
        raise ColibriStage2Failure("resume_state_invalid")
    if document.get("schema_version") != RESUME_LEDGER_SCHEMA_VERSION:
        raise ColibriStage2Failure("resume_state_invalid")
    if (
        document.get("model_repository") != PINNED_MODEL_REPOSITORY
        or document.get("model_revision") != PINNED_MODEL_REVISION
        or document.get("colibri_commit") != PINNED_COLIBRI_COMMIT
    ):
        # A ledger written for a different model, revision, or Colibrì
        # commit describes artifacts this run must never reuse.
        raise ColibriStage2Failure("resume_state_invalid")

    shards = document.get("converted_shards")
    if not isinstance(shards, list):
        raise ColibriStage2Failure("resume_state_invalid")
    records: dict[str, ConvertedShardRecord] = {}
    for item in shards:
        if not isinstance(item, dict):
            raise ColibriStage2Failure("resume_state_invalid")
        try:
            record = ConvertedShardRecord(
                basename=item.get("basename"),
                size_bytes=item.get("size_bytes"),
                sha256=item.get("sha256"),
                converter_kind=item.get("converter_kind"),
            )
        except (TypeError, ValueError) as exc:
            raise ColibriStage2Failure("resume_state_invalid") from exc
        if record.basename in records:
            raise ColibriStage2Failure("resume_state_invalid")
        records[record.basename] = record
    return records


def write_resume_ledger(converted_dir: Path, records: Mapping[str, ConvertedShardRecord]) -> None:
    """Rewrite the resume ledger atomically.

    Written to a direct-child temporary file and then ``os.replace``d into
    position -- the one place in this module that deliberately replaces an
    existing file, because the ledger is bookkeeping *about* artifacts and
    never an artifact itself. No converted shard, source shard, or config
    is ever replaced anywhere.
    """

    resolved_dir = require_ordinary_directory(
        converted_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    ledger_path = require_direct_child_path(
        resolved_dir, RESUME_LEDGER_BASENAME, category="unsafe_directory_rejected"
    )
    temporary_path = require_direct_child_path(
        resolved_dir, f"{RESUME_LEDGER_BASENAME}.tmp", category="unsafe_directory_rejected"
    )
    document = {
        "schema_version": RESUME_LEDGER_SCHEMA_VERSION,
        "model_repository": PINNED_MODEL_REPOSITORY,
        "model_revision": PINNED_MODEL_REVISION,
        "colibri_commit": PINNED_COLIBRI_COMMIT,
        "converted_shards": [
            {
                "basename": records[basename].basename,
                "size_bytes": records[basename].size_bytes,
                "sha256": records[basename].sha256,
                "converter_kind": records[basename].converter_kind,
            }
            for basename in EXPECTED_SHARD_BASENAMES
            if basename in records
        ],
    }
    try:
        temporary_path.write_text(
            json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary_path, ledger_path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise ColibriStage2Failure("resume_state_invalid") from exc


def verify_existing_converted_shard(path: Path, record: ConvertedShardRecord) -> bool:
    """Prove an already-converted artifact matches its recorded identity.

    Same shape as ``verify_existing_source_file``: ordinary regular file,
    exact recorded size, exact recorded SHA-256. Absent returns ``False``
    (convert it). Present-but-not-matching raises, because a converted
    artifact that does not match its own record is exactly the situation
    in which overwriting would destroy evidence.
    """

    if path.name != record.basename:
        raise ColibriStage2Failure("unsafe_basename_rejected")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ColibriStage2Failure("resume_state_invalid") from exc
    attrs = getattr(info, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not stat_module.S_ISREG(info.st_mode):
        raise ColibriStage2Failure("resume_state_invalid")
    if info.st_size != record.size_bytes:
        raise ColibriStage2Failure("resume_state_invalid")
    try:
        digest = _sha256_file(path)
    except OSError as exc:
        raise ColibriStage2Failure("resume_state_invalid") from exc
    if digest != record.sha256:
        raise ColibriStage2Failure("resume_state_invalid")
    return True


# ---------------------------------------------------------------------------
# Per-file / per-shard transactional primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShardTransactionResult:
    """The complete source-to-converted evidence for one shard.

    ``source_basename``/``source_size_bytes``/``source_sha256`` always
    come from the reviewed ``SourceShardEntry`` used by the transaction --
    never recomputed from caller-provided values -- so the capture's
    source identity is anchored to what was actually reviewed, not to
    whatever bytes happened to be downloaded.
    """

    source_basename: str
    source_size_bytes: int
    source_sha256: str
    source_verified: bool
    source_deleted: bool
    converted_basename: str
    converted_size_bytes: int
    converted_sha256: str
    partial_cleanup_complete: bool
    temporary_output_cleanup_complete: bool
    elapsed_ms: int
    # Resume evidence. ``source_reused`` means the source shard was already
    # on disk and passed the full pinned-identity proof, so no byte was
    # downloaded again. ``converted_reused`` means the converted artifact
    # was already present *and* matched a recorded, re-verified identity,
    # so it was left exactly as it was.
    source_reused: bool = False
    converted_reused: bool = False
    # Peak memory is populated only when whole-tree accounting was
    # positively confirmed; ``conversion_peak_memory_state`` says which.
    conversion_peak_memory_bytes: int | None = None
    conversion_peak_commit_bytes: int | None = None
    conversion_peak_memory_state: str = MEMORY_ACCOUNTING_UNAVAILABLE
    # Which reviewed converter produced this shard's artifact. For a
    # reused shard this is the kind recorded when the artifact was
    # originally created, not the converter configured for this run.
    converter_kind: str | None = None

    def __post_init__(self) -> None:
        # Rejected at construction, not merely at capture time, so a
        # forged converter kind cannot exist in a result object at all.
        if self.converter_kind is not None and self.converter_kind not in CONVERTER_KINDS:
            raise ValueError("shard result converter_kind is not a reviewed converter")
        if self.conversion_peak_memory_state not in MEMORY_ACCOUNTING_STATES:
            raise ValueError("shard result has an unknown memory accounting state")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_regular_no_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return stat_module.S_ISREG(info.st_mode)


def verify_existing_source_file(path: Path, expected: SourceShardEntry) -> bool:
    """The single, complete reuse proof for one already-present source file.

    Returns ``True`` only when *every* one of these holds, in this order:

    * the basename is exactly the pinned reviewed basename;
    * the file is an ordinary regular file, not a directory, device,
      symlink, junction, or any other reparse point;
    * ``st_size`` equals the reviewed size *exactly* -- so a partially
      written file is rejected here, before a single byte is hashed;
    * the full SHA-256 over the whole file equals the reviewed digest.

    Anything absent returns ``False`` (nothing to reuse -- download it).
    Anything *present but not matching* raises, because silently deleting
    and re-fetching a file the operator did not expect to be wrong would
    hide a real problem -- and on this machine it would also throw away a
    5 GB download. Callers therefore never reuse a partial file: a partial
    always fails the exact-size check, and a truncated-then-padded file
    fails the digest.

    ``require_direct_child_path`` is applied to the caller's approved
    parent before this is ever called, so a reparse point cannot smuggle
    the read outside the approved directory.
    """

    if path.name != expected.basename or not is_safe_basename(expected.basename):
        raise ColibriStage2Failure("unsafe_basename_rejected")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ColibriStage2Failure("stale_source_file_rejected") from exc

    attrs = getattr(info, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not stat_module.S_ISREG(info.st_mode):
        raise ColibriStage2Failure("stale_source_file_rejected")
    if info.st_size != expected.size_bytes:
        # A partial download, a truncated file, or a different file
        # altogether. Never trusted, and never silently repaired.
        raise ColibriStage2Failure("stale_source_file_rejected")
    try:
        digest = _sha256_file(path)
    except OSError as exc:
        raise ColibriStage2Failure("stale_source_file_rejected") from exc
    if digest != expected.sha256:
        raise ColibriStage2Failure("shard_verification_failed")
    return True


def download_and_verify_config(
    *,
    expected_config: SourceShardEntry,
    destination_dir: Path,
    downloader: Downloader,
    allow_resume: bool = True,
) -> tuple[Path, bool]:
    """Step 1 of the approved sequence: make a fully verified config.json
    available. The returned path stays available (never deleted here) for
    every subsequent converter call.

    Returns ``(path, reused)``. When ``allow_resume`` and an existing
    config passes the complete ``verify_existing_source_file`` proof, it is
    reused as-is and nothing is downloaded; an existing file that fails
    that proof always raises rather than being overwritten.
    """

    if expected_config.basename != EXPECTED_CONFIG_BASENAME:
        raise ColibriStage2Failure("unsafe_basename_rejected")
    resolved_destination_dir = require_ordinary_directory(
        destination_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    config_path = require_direct_child_path(
        resolved_destination_dir, expected_config.basename, category="unsafe_directory_rejected"
    )
    if config_path.exists():
        if not allow_resume:
            raise ColibriStage2Failure("destination_not_empty")
        if verify_existing_source_file(config_path, expected_config):
            return config_path, True
        raise ColibriStage2Failure("stale_source_file_rejected")

    try:
        downloader.download(
            basename=expected_config.basename,
            expected_size_bytes=expected_config.size_bytes,
            expected_sha256=expected_config.sha256,
            destination=config_path,
        )
    except OSError as exc:
        raise ColibriStage2Failure("shard_download_failed") from exc
    if not _is_regular_no_reparse(config_path):
        raise ColibriStage2Failure("shard_download_failed")
    if (
        config_path.name != expected_config.basename
        or config_path.stat().st_size != expected_config.size_bytes
        or _sha256_file(config_path) != expected_config.sha256
    ):
        raise ColibriStage2Failure("shard_verification_failed")
    return config_path, False


def run_shard_transaction(
    *,
    expected_source: SourceShardEntry,
    destination_dir: Path,
    config_path: Path,
    final_converted_dir: Path,
    temp_output_parent: Path,
    downloader: Downloader,
    converter: Converter,
    clock: Callable[[], float] = time.monotonic,
    allow_resume: bool = True,
    converted_record: ConvertedShardRecord | None = None,
) -> ShardTransactionResult:
    """One fully transactional shard, matching the approved sequence 3a-3j:

    download -> verify -> convert into a fresh empty temp output dir ->
    verify the temp output is exactly {config.json, this shard} and the
    config is byte-identical -> hash the converted shard -> atomically
    move it into the final directory only if absent -> only then delete
    the source shard and verify deletion -> remove and verify removal of
    the temp output.

    A failure at any point before the converted shard is moved into the
    final directory leaves the source shard untouched. An existing
    converted shard is never overwritten. ``converted_basename`` is always
    exactly ``expected_source.basename`` -- Stage 2A never renames a
    shard during conversion.

    Resume behaviour (``allow_resume``, the default):

    * If ``converted_record`` is supplied and the converted artifact on
      disk re-verifies against it, the shard is already done: nothing is
      downloaded, nothing is converted, and the existing artifact is left
      byte-for-byte untouched.
    * Otherwise, an already-present source shard that passes the complete
      pinned-identity proof is reused, so a conversion that crashed does
      not cost another 5 GB download. A present source shard that fails
      that proof -- including any partial file -- always raises.
    """

    source_basename = expected_source.basename
    if source_basename == EXPECTED_CONFIG_BASENAME:
        raise ColibriStage2Failure("unsafe_basename_rejected")

    resolved_destination_dir = require_ordinary_directory(
        destination_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    resolved_final_dir = require_ordinary_directory(
        final_converted_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    resolved_temp_parent = require_ordinary_directory(
        temp_output_parent, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    source_path = require_direct_child_path(
        resolved_destination_dir, source_basename, category="unsafe_directory_rejected"
    )
    final_converted_path = require_direct_child_path(
        resolved_final_dir, source_basename, category="unsafe_directory_rejected"
    )
    resolved_config_path = require_direct_child_path(
        resolved_destination_dir, EXPECTED_CONFIG_BASENAME, category="unsafe_directory_rejected"
    )
    try:
        config_path_resolved = config_path.resolve(strict=True)
    except OSError as exc:
        raise ColibriStage2Failure("unsafe_directory_rejected") from exc
    if resolved_config_path != config_path_resolved:
        raise ColibriStage2Failure("unsafe_directory_rejected")

    started = clock()

    if final_converted_path.exists():
        # Already converted. With a recorded identity that re-verifies,
        # this is a completed step being resumed -- report it as reused
        # and leave the artifact exactly as it is. Without one, fail
        # closed: an unrecognised artifact is never overwritten and never
        # trusted.
        if allow_resume and converted_record is not None:
            if verify_existing_converted_shard(final_converted_path, converted_record):
                # A previous run may have crashed between placing the
                # converted artifact (3h) and deleting its source shard
                # (3i). The conversion for this shard is proven complete,
                # so finish that interrupted transaction rather than
                # leaving it half-done -- but only after the leftover
                # source re-verifies against its pinned identity, so a
                # file that is not the shard we converted is never
                # deleted. A leftover that fails that proof raises.
                if source_path.exists():
                    verify_existing_source_file(source_path, expected_source)
                    try:
                        source_path.unlink()
                    except OSError as exc:
                        raise ColibriStage2Failure("source_shard_deletion_failed") from exc
                    if source_path.exists():
                        raise ColibriStage2Failure("source_shard_deletion_unverified")
                return ShardTransactionResult(
                    source_basename=source_basename,
                    source_size_bytes=expected_source.size_bytes,
                    source_sha256=expected_source.sha256,
                    source_verified=True,
                    source_deleted=True,
                    converted_basename=source_basename,
                    converted_size_bytes=converted_record.size_bytes,
                    converted_sha256=converted_record.sha256,
                    partial_cleanup_complete=not (
                        source_path.parent / f"{source_basename}.partial"
                    ).exists(),
                    temporary_output_cleanup_complete=True,
                    elapsed_ms=max(0, round((clock() - started) * 1000)),
                    source_reused=True,
                    converted_reused=True,
                    # The converter that actually made this artifact,
                    # taken from its record -- never this run's converter.
                    converter_kind=converted_record.converter_kind,
                )
        raise ColibriStage2Failure("converted_shard_already_exists")

    # 3a: make a fully verified source shard available. An existing shard
    # is reused only after the complete pinned-identity proof (basename,
    # exact size, SHA-256, ordinary file, direct child, non-reparse);
    # anything present that fails that proof raises rather than being
    # overwritten or silently re-fetched.
    source_reused = False
    if source_path.exists():
        if not allow_resume:
            raise ColibriStage2Failure("destination_not_empty")
        source_reused = verify_existing_source_file(source_path, expected_source)
        if not source_reused:
            raise ColibriStage2Failure("stale_source_file_rejected")
    else:
        try:
            downloader.download(
                basename=source_basename,
                expected_size_bytes=expected_source.size_bytes,
                expected_sha256=expected_source.sha256,
                destination=source_path,
            )
        except OSError as exc:
            raise ColibriStage2Failure("shard_download_failed") from exc
        if not _is_regular_no_reparse(source_path):
            raise ColibriStage2Failure("shard_download_failed")
        if (
            source_path.name != source_basename
            or source_path.stat().st_size != expected_source.size_bytes
            or _sha256_file(source_path) != expected_source.sha256
        ):
            raise ColibriStage2Failure("shard_verification_failed")

    # 3b: a fresh, empty per-shard converter-output directory -- validated
    # immediately, since a newly created private directory is not exempt
    # from the same ordinary-directory proof as any other.
    temp_output_dir = Path(tempfile.mkdtemp(prefix="colibri-stage2-shard-", dir=resolved_temp_parent))
    temp_output_dir = require_ordinary_directory(
        temp_output_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )

    primary_failure: ColibriStage2Failure | None = None
    converted_sha256 = ""
    converted_size_bytes = 0
    run_evidence: ConversionRunEvidence | None = None
    try:
        # 3c/3d: the exact pinned converter, explicit argv, shell=False,
        # bounded/deadlined -- enforced by the adapter itself.
        try:
            run_evidence = converter.convert(
                model_dir=resolved_destination_dir, output_dir=temp_output_dir
            )
        except OSError as exc:
            raise ColibriStage2Failure("conversion_failed") from exc

        # 3e: the temp output must contain exactly config.json and this
        # one converted shard -- nothing else.
        try:
            entries = {entry.name for entry in temp_output_dir.iterdir()}
        except OSError as exc:
            raise ColibriStage2Failure("conversion_output_unexpected") from exc
        if entries != {EXPECTED_CONFIG_BASENAME, source_basename}:
            raise ColibriStage2Failure("conversion_output_unexpected")

        temp_config_path = temp_output_dir / EXPECTED_CONFIG_BASENAME
        temp_shard_path = temp_output_dir / source_basename
        if not _is_regular_no_reparse(temp_config_path) or not _is_regular_no_reparse(temp_shard_path):
            raise ColibriStage2Failure("converted_shard_missing")

        # 3f: the converted config must remain byte-identical.
        if temp_config_path.read_bytes() != config_path_resolved.read_bytes():
            raise ColibriStage2Failure("conversion_output_unexpected")

        # 3g: hash and size the converted shard.
        converted_sha256 = _sha256_file(temp_shard_path)
        converted_size_bytes = temp_shard_path.stat().st_size

        # 3h: atomically move into the final directory, only if absent --
        # one no-replace primitive, not a check-then-replace race.
        atomic_no_replace_move(
            temp_shard_path, final_converted_path, exists_category="converted_shard_already_exists"
        )
    except ColibriStage2Failure as exc:
        primary_failure = exc
    finally:
        # 3j: remove and verify removal of the per-shard temporary output,
        # regardless of whether the conversion itself succeeded.
        cleanup_ok = True
        if temp_output_dir.exists():
            try:
                shutil.rmtree(temp_output_dir, ignore_errors=False)
            except OSError:
                cleanup_ok = False
            if temp_output_dir.exists():
                cleanup_ok = False

    if primary_failure is not None:
        raise primary_failure
    if not cleanup_ok:
        raise ColibriStage2Failure("temporary_output_cleanup_failed")

    # 3i: only now delete the source shard, and verify the deletion.
    try:
        source_path.unlink()
    except OSError as exc:
        raise ColibriStage2Failure("source_shard_deletion_failed") from exc
    if source_path.exists():
        raise ColibriStage2Failure("source_shard_deletion_unverified")

    # Prove no stray download-partial artifact was left behind next to
    # the (now-deleted) source shard, regardless of which Downloader
    # implementation was used.
    partial_marker = source_path.parent / f"{source_basename}.partial"
    partial_cleanup_complete = not partial_marker.exists()

    elapsed_ms = round((clock() - started) * 1000)
    return ShardTransactionResult(
        source_basename=source_basename,
        source_size_bytes=expected_source.size_bytes,
        source_sha256=expected_source.sha256,
        source_verified=True,
        source_deleted=True,
        converted_basename=source_basename,
        converted_size_bytes=converted_size_bytes,
        converted_sha256=converted_sha256,
        partial_cleanup_complete=partial_cleanup_complete,
        temporary_output_cleanup_complete=cleanup_ok,
        elapsed_ms=max(0, elapsed_ms),
        source_reused=source_reused,
        converted_reused=False,
        conversion_peak_memory_bytes=(
            run_evidence.peak_memory_bytes if run_evidence is not None else None
        ),
        conversion_peak_commit_bytes=(
            run_evidence.peak_commit_bytes if run_evidence is not None else None
        ),
        conversion_peak_memory_state=(
            run_evidence.peak_memory_state
            if run_evidence is not None
            else MEMORY_ACCOUNTING_UNAVAILABLE
        ),
        # Taken from the adapter that actually ran. A converter reporting
        # no kind (every synthetic test fake) leaves this ``None``, and a
        # capture cannot then be built -- which is the intended outcome:
        # an unattributable conversion must not claim any identity.
        converter_kind=(
            run_evidence.converter_kind
            if run_evidence is not None
            else getattr(converter, "converter_kind", None)
        ),
    )


# ---------------------------------------------------------------------------
# The full approved sequence
# ---------------------------------------------------------------------------


def run_approved_conversion(
    *,
    interactive_check: Callable[[], bool],
    approved: bool,
    destination_dir: Path,
    final_converted_dir: Path,
    temp_output_parent: Path,
    free_bytes_probe: Callable[[Path], int],
    isolated_python_env_ready: bool,
    dependency_versions: Mapping[str, str],
    downloader: Downloader,
    converter: Converter,
    clock: Callable[[], float] = time.monotonic,
    allow_resume: bool = True,
) -> dict[str, Any]:
    """The complete approved download/conversion sequence.

    ``check_approved_preconditions`` (via ``require_reviewed_source_manifest``)
    is the very first thing this calls, before any directory is even
    validated. With the reviewed source manifest now committed, this is the
    complete, real, executable path -- still gated behind every other
    approved-execution precondition (explicit approval, interactive
    stdin/stdout, isolated environment, disk space, safe paths) checked by
    ``check_approved_preconditions``.

    With ``allow_resume`` (the default), a previously interrupted run
    continues from whatever was already *proven*: each shard's recorded
    converted artifact is re-verified before reuse, and each source file
    is re-verified against its pinned identity before reuse. Every shard
    that completes is recorded in the resume ledger immediately, so a
    crash on shard 2 never costs shard 1's work or shard 2's download.
    """

    reviewed = check_approved_preconditions(
        interactive_check=interactive_check,
        approved=approved,
        destination_dir=destination_dir,
        converted_dir=final_converted_dir,
        free_bytes_probe=free_bytes_probe,
        isolated_python_env_ready=isolated_python_env_ready,
        dependency_versions=dependency_versions,
        allow_resume=allow_resume,
    )

    total_started = clock()

    ledger = read_resume_ledger(final_converted_dir) if allow_resume else {}

    config_entry = reviewed[EXPECTED_CONFIG_BASENAME]
    config_path, config_reused = download_and_verify_config(
        expected_config=config_entry,
        destination_dir=destination_dir,
        downloader=downloader,
        allow_resume=allow_resume,
    )

    shard_results: list[ShardTransactionResult] = []
    for basename in EXPECTED_SHARD_BASENAMES:
        entry = reviewed[basename]
        result = run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=downloader,
            converter=converter,
            clock=clock,
            allow_resume=allow_resume,
            converted_record=ledger.get(basename),
        )
        shard_results.append(result)
        if allow_resume and not result.converted_reused:
            # Record the shard the moment it is proven complete, so a
            # crash on a later shard never costs this one.
            if result.converter_kind is None:
                # An artifact whose producing converter cannot be named
                # must not be recorded as resumable -- a later run would
                # have no truthful identity to report for it.
                raise ColibriStage2Failure("resume_state_invalid")
            ledger[basename] = ConvertedShardRecord(
                basename=result.converted_basename,
                size_bytes=result.converted_size_bytes,
                sha256=result.converted_sha256,
                converter_kind=result.converter_kind,
            )
            write_resume_ledger(final_converted_dir, ledger)

    # Step 6: copy/move the verified config into the final directory,
    # exactly once, only after every shard has succeeded.
    resolved_final_dir = require_ordinary_directory(
        final_converted_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    final_config_path = require_direct_child_path(
        resolved_final_dir, EXPECTED_CONFIG_BASENAME, category="unsafe_directory_rejected"
    )
    if final_config_path.exists():
        # On a resumed run the final config may already be in place from
        # the previous attempt. Reuse it only after it re-verifies against
        # the reviewed source identity; never overwrite it either way.
        if not allow_resume or not verify_existing_source_file(final_config_path, config_entry):
            raise ColibriStage2Failure("converted_shard_already_exists")
        config_path.unlink(missing_ok=True)
    else:
        atomic_no_replace_move(
            config_path, final_config_path, exists_category="converted_shard_already_exists"
        )

    converted_config_size_bytes = final_config_path.stat().st_size
    converted_config_sha256 = _sha256_file(final_config_path)
    total_elapsed_ms = round((clock() - total_started) * 1000)

    return build_conversion_capture(
        source_config=config_entry,
        source_config_verified=True,
        source_config_moved_to_final=True,
        converted_config_sha256=converted_config_sha256,
        converted_config_size_bytes=converted_config_size_bytes,
        shard_results=shard_results,
        dependency_versions=dependency_versions,
        total_elapsed_ms=max(0, total_elapsed_ms),
        cleanup_complete=True,
        source_config_reused=config_reused,
    )


# ---------------------------------------------------------------------------
# Privacy-safe conversion capture
# ---------------------------------------------------------------------------


def build_conversion_capture(
    *,
    source_config: SourceShardEntry,
    source_config_verified: bool,
    source_config_moved_to_final: bool,
    converted_config_sha256: str,
    converted_config_size_bytes: int,
    shard_results: Sequence[ShardTransactionResult],
    dependency_versions: Mapping[str, str],
    total_elapsed_ms: int,
    cleanup_complete: bool,
    source_config_reused: bool = False,
) -> dict[str, Any]:
    """A closed, privacy-safe capture with the complete reviewable identity
    set the next tiny registry-pinning commit needs.

    The converter identity is never a caller-supplied parameter. Each
    shard names the *kind* of converter that produced it, and the identity
    is resolved from that through the closed
    ``common.REVIEWED_CONVERTER_IDENTITY_BY_KIND`` mapping -- so a capture
    can never claim an identity nobody reviewed, and can never attribute
    a bounded conversion to the upstream script. A shard that cannot say
    which converter produced it is rejected rather than defaulted.

    The three shard records are required to be in exactly
    ``EXPECTED_SHARD_BASENAMES`` order -- this simultaneously rejects
    duplicates, missing entries, and wrong ordering, since only one
    3-tuple of distinct basenames can ever equal that fixed order. Every
    proof boolean (source_verified, source_deleted,
    partial_cleanup_complete, temporary_output_cleanup_complete,
    source_config_verified, source_config_moved_to_final) must be true.

    Never contains a path, username, environment value, or raw tool
    output, and never validates as an ``OlmoeModelManifest`` -- its keys
    never match that dataclass's constructor, its state is always
    ``unreviewed_conversion_capture``, and it can never itself authorize a
    real run.
    """

    if not isinstance(source_config, SourceShardEntry) or source_config.basename != EXPECTED_CONFIG_BASENAME:
        raise ValueError("invalid source config identity")
    if not source_config_verified or not source_config_moved_to_final:
        raise ValueError("source config proof booleans must both be true")
    if not is_hex64(converted_config_sha256):
        raise ValueError("invalid converted config sha256")
    if (
        isinstance(converted_config_size_bytes, bool)
        or not isinstance(converted_config_size_bytes, int)
        or converted_config_size_bytes <= 0
    ):
        raise ValueError("invalid converted config size_bytes")

    if len(shard_results) != 3:
        raise ValueError("conversion capture requires exactly three shard results")
    if tuple(result.source_basename for result in shard_results) != EXPECTED_SHARD_BASENAMES:
        raise ValueError("conversion capture shard results must be in exactly the pinned shard order")
    if tuple(result.converted_basename for result in shard_results) != EXPECTED_SHARD_BASENAMES:
        raise ValueError("conversion capture converted basenames must match the pinned shard order")

    unknown = set(dependency_versions) - ALLOWED_CONVERSION_DEPENDENCY_NAMES
    if unknown:
        raise ValueError(f"unknown conversion dependency names: {sorted(unknown)}")
    for name, version in dependency_versions.items():
        if not is_safe_basename(name) or not is_simple_version(version):
            raise ValueError(f"invalid conversion dependency version for {name!r}")

    shards: list[dict[str, Any]] = []
    for result in shard_results:
        if not is_hex64(result.source_sha256) or not is_hex64(result.converted_sha256):
            raise ValueError("shard result has an invalid SHA-256")
        if (
            isinstance(result.source_size_bytes, bool)
            or not isinstance(result.source_size_bytes, int)
            or result.source_size_bytes <= 0
        ):
            raise ValueError("shard result has a nonpositive source size")
        if (
            isinstance(result.converted_size_bytes, bool)
            or not isinstance(result.converted_size_bytes, int)
            or result.converted_size_bytes <= 0
        ):
            raise ValueError("shard result has a nonpositive converted size")
        if not (
            result.source_verified
            and result.source_deleted
            and result.partial_cleanup_complete
            and result.temporary_output_cleanup_complete
        ):
            raise ValueError("shard result proof booleans must all be true")
        for peak in (result.conversion_peak_memory_bytes, result.conversion_peak_commit_bytes):
            if peak is not None and (
                isinstance(peak, bool) or not isinstance(peak, int) or peak < 0
            ):
                raise ValueError("shard result peak memory must be a non-negative integer or None")
        if result.conversion_peak_memory_state not in MEMORY_ACCOUNTING_STATES:
            raise ValueError("shard result has an unknown memory accounting state")
        if result.conversion_peak_memory_state == MEMORY_ACCOUNTING_UNAVAILABLE and (
            result.conversion_peak_memory_bytes is not None
            or result.conversion_peak_commit_bytes is not None
        ):
            # A capture may never present an unconfirmed number as if it
            # were a measurement.
            raise ValueError("unavailable memory accounting must not carry peak values")
        # Resolve this shard's producing converter to its reviewed
        # identity. ``reviewed_identity_for_converter_kind`` is the only
        # route by which any identity reaches a capture, and it accepts a
        # closed kind rather than a basename, size, hash, or identity
        # object -- so a bounded conversion can never be recorded as the
        # upstream script, and neither can be caller-substituted.
        if result.converter_kind is None:
            raise ValueError("shard result does not record which converter produced it")
        shard_identity = reviewed_identity_for_converter_kind(result.converter_kind)
        shards.append(
            {
                "source_basename": result.source_basename,
                "source_size_bytes": int(result.source_size_bytes),
                "source_sha256": result.source_sha256,
                "source_verified": bool(result.source_verified),
                "source_deleted": bool(result.source_deleted),
                "source_reused": bool(result.source_reused),
                "converted_basename": result.converted_basename,
                "converted_size_bytes": int(result.converted_size_bytes),
                "converted_sha256": result.converted_sha256,
                "converted_reused": bool(result.converted_reused),
                "partial_cleanup_complete": bool(result.partial_cleanup_complete),
                "temporary_output_cleanup_complete": bool(result.temporary_output_cleanup_complete),
                "elapsed_ms": max(0, int(result.elapsed_ms)),
                # Bounded numeric resource evidence only -- never a path,
                # an environment value, or any converter output.
                "conversion_peak_memory_bytes": (
                    None
                    if result.conversion_peak_memory_bytes is None
                    else int(result.conversion_peak_memory_bytes)
                ),
                "conversion_peak_commit_bytes": (
                    None
                    if result.conversion_peak_commit_bytes is None
                    else int(result.conversion_peak_commit_bytes)
                ),
                # Says whether the two peaks above are a confirmed
                # whole-tree measurement or simply absent.
                "conversion_peak_memory_state": result.conversion_peak_memory_state,
                # The reviewed identity of the converter that actually
                # produced this shard.
                "converter_kind": result.converter_kind,
                "converter_basename": shard_identity.basename,
                "converter_size_bytes": shard_identity.size_bytes,
                "converter_sha256": shard_identity.sha256,
            }
        )

    return {
        "schema_version": CONVERSION_CAPTURE_SCHEMA_VERSION,
        "state": CONVERSION_CAPTURE_STATE,
        "model_repository": PINNED_MODEL_REPOSITORY,
        "model_revision": PINNED_MODEL_REVISION,
        "license_identifier": PINNED_LICENSE_IDENTIFIER,
        "colibri_commit": PINNED_COLIBRI_COMMIT,
        # Every reviewed converter that contributed to this model, derived
        # from the shards themselves. A resumed run may legitimately mix
        # the two (that is the upstream-script -> bounded migration this
        # PR enables), so this is a list rather than one identity, and it
        # never names a converter that did not actually run.
        "converters": [
            {
                "converter_kind": kind,
                "basename": reviewed_identity_for_converter_kind(kind).basename,
                "size_bytes": reviewed_identity_for_converter_kind(kind).size_bytes,
                "sha256": reviewed_identity_for_converter_kind(kind).sha256,
            }
            for kind in sorted({result.converter_kind for result in shard_results})
        ],
        "source_config_basename": source_config.basename,
        "source_config_size_bytes": source_config.size_bytes,
        "source_config_sha256": source_config.sha256,
        "source_config_verified": bool(source_config_verified),
        "source_config_moved_to_final": bool(source_config_moved_to_final),
        "source_config_reused": bool(source_config_reused),
        "converted_config_size_bytes": max(0, int(converted_config_size_bytes)),
        "converted_config_sha256": converted_config_sha256,
        "shards": shards,
        "dependency_versions": dict(dependency_versions),
        "total_elapsed_ms": max(0, int(total_elapsed_ms)),
        "cleanup_complete": bool(cleanup_complete),
    }


def _default_free_bytes_probe(path: Path) -> int:
    probe_path = path if path.exists() else path.parent
    return shutil.disk_usage(probe_path).free


def _default_interactive_check() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _default_isolated_python_env_ready() -> bool:
    """A real (best-effort) venv/virtualenv detection: true whenever the
    running interpreter's ``sys.prefix`` differs from its base install
    prefix, which every standard ``venv``/``virtualenv`` arranges."""

    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _default_dependency_versions() -> dict[str, str]:
    """Real, best-effort dependency capture: only records a dependency
    that is actually importable right now, with a version string that
    passes ``is_simple_version``. Never installs anything."""

    versions: dict[str, str] = {"python": "%d.%d.%d" % sys.version_info[:3]}
    for module_name in ("torch", "safetensors"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        version = str(getattr(module, "__version__", ""))
        if is_simple_version(version):
            versions[module_name] = version
    return versions


def _print_json(payload: Mapping[str, Any]) -> None:
    import json

    print(json.dumps(dict(payload), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    """Developer-only CLI. Defaults to dry-run (network-free, process-free).

    ``--approve`` requests the complete, real approved sequence: validate
    the converter identity, safely create/validate the source/converted/
    private-temp-output roots, construct the default real adapters, and
    run ``run_approved_conversion`` -- printing only the closed conversion
    capture on success. The reviewed source manifest gate is always the
    very first thing checked; the remaining approved-execution
    preconditions (explicit approval, interactive stdin/stdout, isolated
    environment with torch/safetensors, disk space, safe paths) are
    checked next, before any directory is created, any converter file
    opened, any dependency probed, or any network/process call made.
    """

    import argparse

    parser = argparse.ArgumentParser(description="Colibrì Stage 2A OLMoE download/conversion plan")
    parser.add_argument("--destination", required=True, help="Target directory for the source download")
    parser.add_argument(
        "--converted-destination", required=True, help="Target directory for the final converted model"
    )
    parser.add_argument(
        "--converter-script",
        help=(
            "Path to the reviewed convert_olmoe.py. Only needed with "
            "--converter pinned-script; the default bounded converter "
            "takes no path at all."
        ),
    )
    parser.add_argument(
        "--converter",
        choices=("bounded", "pinned-script"),
        default="bounded",
        help=(
            "Which converter to run. 'bounded' (default) is the in-repo "
            "memory-bounded converter, the only one that fits a 16 GiB "
            "host; 'pinned-script' is the unmodified upstream "
            "convert_olmoe.py, which needs roughly 10 GiB per shard."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Continue a previously interrupted run: reuse only files that "
            "pass their full pinned identity proof (exact basename, size, "
            "and SHA-256). Never reuses a partial file and never "
            "overwrites an existing converted artifact."
        ),
    )
    parser.add_argument(
        "--temp-output-parent",
        help=(
            "Private scratch root for per-shard temporary converter output "
            "(defaults to a sibling of --destination; must not be nested "
            "inside --destination or --converted-destination)"
        ),
    )
    parser.add_argument("--approve", action="store_true", help="Request approved execution")
    args = parser.parse_args(argv)
    destination = Path(args.destination)
    converted_destination = Path(args.converted_destination)

    plan = build_dry_run_plan(destination)
    output: dict[str, Any] = {
        "model_repository": plan.model_repository,
        "model_revision": plan.model_revision,
        "license_identifier": plan.license_identifier,
        "required_source_files": list(plan.required_source_files),
        "approx_download_bytes": plan.approx_download_bytes,
        "required_free_space_bytes": plan.required_free_space_bytes,
        "destination": plan.destination,
        "steps": list(plan.steps),
        "deviation_statement": plan.deviation_statement,
        "approval_statement": plan.approval_statement,
        "mode": "dry_run",
    }

    if not args.approve:
        _print_json(output)
        return 0

    def _reject(category: str) -> int:
        _print_json({"mode": "approved_rejected", "rejection_category": category})
        return 1

    # 1. The reviewed source manifest gate -- always first. No directory
    # is created, no converter file opened, no dependency probed, and no
    # network/process call made until this passes.
    try:
        require_reviewed_source_manifest()
    except ColibriStage2Failure as exc:
        output["mode"] = "approved_rejected"
        output["rejection_category"] = exc.category
        _print_json(output)
        return 1

    # 2. --approve was already required to reach this branch at all.

    # 3. Both stdin and stdout must be interactive -- checked before any
    # dependency import, converter read, directory validation, disk
    # probe, or network/process call.
    if not _default_interactive_check():
        return _reject("noninteractive_approval_rejected")

    # 4. Detect the isolated virtual environment.
    isolated_python_env_ready = _default_isolated_python_env_ready()
    if not isolated_python_env_ready:
        return _reject("python_environment_unavailable")

    # 5. Collect and validate dependency versions. This is the first
    # point at which torch/safetensors are imported -- never before the
    # interactive check above.
    dependency_versions = _default_dependency_versions()
    missing_dependencies = {"torch", "safetensors"} - set(dependency_versions)
    if missing_dependencies:
        return _reject("dependency_unavailable")
    for name, version in dependency_versions.items():
        if not is_safe_basename(name) or not is_simple_version(version):
            return _reject("dependency_unavailable")

    # 6. Validate the converter. The bounded converter is in-repo and
    # located from the already-imported module, so it takes no path from
    # the caller at all; the pinned upstream script must be given as an
    # absolute path and must match its reviewed identity exactly.
    converter_script_path: Path | None = None
    if args.converter == "pinned-script":
        converter_script_path = Path(args.converter_script) if args.converter_script else None
        if converter_script_path is None or not converter_script_path.is_absolute():
            return _reject("conversion_failed")
        try:
            require_reviewed_converter_identity(converter_script_path)
        except ColibriStage2Failure as exc:
            return _reject(exc.category)
    else:
        try:
            bounded_converter_script_path()
        except ColibriStage2Failure as exc:
            return _reject(exc.category)

    # 7. Validate the existing PARENT directory of every leaf this run
    # might create -- never mkdir(parents=True). Every parent must
    # already exist and pass ordinary-directory/path-chain validation
    # before any leaf is created. The temp root is always a sibling of
    # --destination, never nested inside it or --converted-destination --
    # nesting it there would make step 8's "absent or empty" check fail
    # against our own scratch directory.
    temp_output_parent = (
        Path(args.temp_output_parent)
        if args.temp_output_parent
        else destination.parent / f"{destination.name}-stage2-temp"
    )
    leaves = (destination, converted_destination, temp_output_parent)
    try:
        for leaf in leaves:
            require_ordinary_directory(
                leaf.parent,
                missing_category="unsafe_directory_rejected",
                reparse_category="unsafe_directory_rejected",
            )
    except ColibriStage2Failure as exc:
        return _reject(exc.category)

    # 8. The source and converted roots must be absent or empty -- unless
    # --resume was given, in which case leftover files are exactly the
    # point, and each one is instead held to its full identity proof
    # before any reuse.
    if not args.resume:
        for directory in (destination, converted_destination):
            if directory.exists() and any(directory.iterdir()):
                return _reject("destination_not_empty")

    # 9. Check free space.
    if _default_free_bytes_probe(destination) < REQUIRED_FREE_SPACE_BYTES:
        return _reject("insufficient_disk_space")

    # 10. Only now create the intended leaf directories -- each parent
    # was already proven to exist above, so no mkdir(parents=True) is
    # ever needed. Every newly-created (or pre-existing, already-proven-
    # empty) leaf is re-validated immediately.
    try:
        for leaf in leaves:
            leaf.mkdir(exist_ok=True)
            require_ordinary_directory(
                leaf, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
            )
    except OSError:
        return _reject("unsafe_directory_rejected")
    except ColibriStage2Failure as exc:
        return _reject(exc.category)

    # 11. Only now construct the default real adapters and perform any
    # network/process work. The venv/dependency values already computed
    # in steps 4-5 are reused as-is -- never re-evaluated as call
    # arguments here.
    downloader = PinnedRevisionFileDownloader()
    converter: Converter = (
        PinnedScriptConverter(converter_script_path=converter_script_path)
        if converter_script_path is not None
        else BoundedScriptConverter()
    )

    try:
        capture = run_approved_conversion(
            interactive_check=_default_interactive_check,
            approved=True,
            destination_dir=destination,
            final_converted_dir=converted_destination,
            temp_output_parent=temp_output_parent,
            free_bytes_probe=_default_free_bytes_probe,
            isolated_python_env_ready=isolated_python_env_ready,
            dependency_versions=dependency_versions,
            downloader=downloader,
            converter=converter,
            allow_resume=bool(args.resume),
        )
    except ColibriStage2Failure as exc:
        # The closed category plus its bounded numeric evidence -- return
        # code, elapsed time, peak memory. Never raw converter output.
        payload: dict[str, Any] = {
            "mode": "approved_rejected",
            "rejection_category": exc.category,
        }
        if exc.numeric_metadata:
            payload["numeric_metadata"] = dict(exc.numeric_metadata)
        _print_json(payload)
        return 1

    # 12. Print only the closed conversion capture on success.
    _print_json(capture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
