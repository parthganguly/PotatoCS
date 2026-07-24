"""Developer-only real one-token runner for Colibrì Stage 2A (OLMoE).

This module launches the pinned ``olmoe.exe`` exactly once against a
private, embedded-token derived reference, and proves ``Matching tokens:
1/1`` for the single reviewed generated token id (7785). It reuses the
suspended-process, kill-on-close-Job-Object, and bounded-overlapped-pipe
primitives already reviewed in the PR #40 isolated-server stack rather than
reimplementing Windows process lifecycle handling.

Launching is impossible while
``colibri_stage2_manifest.REVIEWED_OLMOE_MODEL_REGISTRY`` is empty: the
manifest gate is checked first, before any file is opened or any process is
created. This commit ships with that registry empty, so this module cannot
be driven to a real launch today.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from odysseus_desktop_backend.runtime_bench.isolated_server import (
    CreatedProcess,
    IsolatedServerFailure,
    cancel_pending_pipe_io,
)
from odysseus_desktop_backend.services.colibri_stage2_common import (
    BITS_ARGUMENT,
    CAP_ARGUMENT,
    EXPECTED_GENERATED_TOKEN_ID,
    PINNED_COLIBRI_COMMIT,
    PINNED_MODEL_REVISION,
    ColibriStage2Failure,
)
from odysseus_desktop_backend.services.colibri_stage2_manifest import (
    OlmoeModelManifest,
    require_reviewed_manifest,
)
from odysseus_desktop_backend.services.colibri_stage2_path_safety import (
    require_direct_child_path,
    require_ordinary_directory,
)
from odysseus_desktop_backend.services.colibri_stage2_reference import (
    ReferenceArtifact,
    canonical_reference_sha256,
    create_private_reference_session,
    delete_private_reference,
    teardown_private_reference_session,
    write_private_reference,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MATCH_LINE = re.compile(r"^Matching tokens: (\d+)/(\d+)$")
_MAX_STREAM_BYTES = 4096
_SETUP_DEADLINE_SECONDS = 30.0
_TOTAL_RUN_DEADLINE_SECONDS = 900.0
_CLEANUP_DEADLINE_SECONDS = 30.0
_WAIT_SLICE_MS = 50

CHILD_ENV_FIXED_KEYS = frozenset({"SNAP", "OMP_NUM_THREADS", "SystemRoot", "SystemDrive", "WINDIR", "TEMP", "TMP"})

# A truthful translation from the PR #40 isolated-server lifecycle failure
# vocabulary into this runner's own closed Stage 2 categories -- never a
# single blanket category regardless of what actually failed.
_ISOLATED_SERVER_FAILURE_CATEGORY_MAP: Mapping[str, str] = {
    "process_create_failed": "process_create_failed",
    "process_attribute_list_failed": "process_create_failed",
    "process_attribute_list_cleanup_failed": "process_create_failed",
    "job_create_failed": "job_create_failed",
    "job_limit_configuration_failed": "job_create_failed",
    "job_assignment_failed": "job_assignment_failed",
    "process_resume_failed": "process_resume_failed",
    "io_cancellation_failed": "cleanup_failed",
    "pending_io_cleanup_timeout": "cleanup_failed",
}
_DEFAULT_ISOLATED_SERVER_FAILURE_CATEGORY = "process_create_failed"


class LifecycleApi(Protocol):
    """The exact subset of the PR #40 ``WindowsLifecycleApi`` surface this
    runner uses. A real ``WindowsLifecycleApi`` instance satisfies this
    structurally; tests inject a synthetic fake."""

    def create_suspended(
        self, executable: Path, arguments: tuple[str, ...], environment: Mapping[str, str]
    ) -> CreatedProcess: ...
    def create_job(self) -> Any: ...
    def configure_kill_on_close(self, job: Any) -> None: ...
    def assign_process(self, job: Any, process: CreatedProcess) -> None: ...
    def verify_job_assignment(self, job: Any, process: CreatedProcess) -> bool: ...
    def process_image_matches(self, process: CreatedProcess, executable: Path) -> bool: ...
    def resume_process(self, process: CreatedProcess) -> None: ...
    def process_exit_code(self, process: CreatedProcess) -> int | None: ...
    def terminate_job(self, job: Any) -> None: ...
    def terminate_process(self, process: CreatedProcess) -> None: ...
    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool: ...
    def descendant_process_ids(self, process_id: int) -> set[int]: ...
    def post_overlapped_read(self, pipe: Any) -> None: ...
    def finish_overlapped_read(self, pipe: Any) -> tuple[str, bytes]: ...
    def cancel_overlapped_read(self, pipe: Any) -> None: ...
    def wait_for_completion(
        self, pipes: tuple[Any, ...], process: CreatedProcess | None, timeout_ms: int
    ) -> None: ...
    def close_pipe(self, pipe: Any) -> None: ...
    def close_handle(self, handle: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class ResourceEvidence:
    """Best-effort, bounded resource evidence. Never required for pass/fail."""

    cpu_time_ms: int | None
    cpu_time_state: str
    process_memory_bytes: int | None
    process_memory_state: str
    disk_read_bytes: int | None
    disk_read_state: str


_UNAVAILABLE_RESOURCE_EVIDENCE = ResourceEvidence(
    cpu_time_ms=None,
    cpu_time_state="unavailable",
    process_memory_bytes=None,
    process_memory_state="unavailable",
    disk_read_bytes=None,
    disk_read_state="unavailable",
)

ResourceProbe = Callable[[Any, CreatedProcess], ResourceEvidence]


@dataclass(frozen=True, slots=True)
class OneTokenRunResult:
    """The only fields a caller may ever observe from a run attempt."""

    category: str
    ok: bool
    matched_count: int | None
    expected_count: int
    token_id: int | None
    evidence_sha256: str | None
    elapsed_ms: int
    exit_code: int | None
    cleanup_complete: bool
    orphan_free: bool
    reference_removed: bool
    resources: ResourceEvidence
    vram_state: str = "not_applicable"


class _BoundedStreamCapture:
    """Per-stream capture bounded to exactly 4096 bytes retained."""

    def __init__(self, cap: int = _MAX_STREAM_BYTES) -> None:
        self._buf = bytearray()
        self._observed = 0
        self._cap = cap

    def feed(self, data: bytes) -> None:
        self._observed += len(data)
        remaining = self._cap - len(self._buf)
        if remaining > 0:
            self._buf.extend(data[:remaining])

    @property
    def overflowed(self) -> bool:
        return self._observed > self._cap

    def bytes_value(self) -> bytes:
        return bytes(self._buf)


class _SplitStreamPump:
    """Drains stdout/stderr into two independent bounded captures.

    Uses exactly the reviewed overlapped-read primitives
    (``post_overlapped_read`` / ``finish_overlapped_read`` /
    ``wait_for_completion``) from the PR #40 lifecycle API, routed per pipe
    index instead of into one shared buffer, since success here depends on
    stderr being provably empty independent of stdout content.
    """

    def __init__(self, api: LifecycleApi, process: CreatedProcess) -> None:
        self.api = api
        self.pipes: tuple[Any, Any] = (process.stdout, process.stderr)
        self.stdout = _BoundedStreamCapture()
        self.stderr = _BoundedStreamCapture()
        self._captures = (self.stdout, self.stderr)
        self._finished: set[int] = set()

    def post_initial(self) -> None:
        for pipe in self.pipes:
            self.api.post_overlapped_read(pipe)

    def service(self) -> None:
        for index, pipe in enumerate(self.pipes):
            if index in self._finished:
                continue
            while True:
                status, data = self.api.finish_overlapped_read(pipe)
                if status == "data":
                    if data:
                        self._captures[index].feed(data)
                    self.api.post_overlapped_read(pipe)
                    continue
                if status in ("eof", "aborted"):
                    self._finished.add(index)
                break

    @property
    def all_finished(self) -> bool:
        return len(self._finished) == len(self.pipes)

    def wait(self, timeout_ms: int) -> None:
        pending = tuple(pipe for index, pipe in enumerate(self.pipes) if index not in self._finished)
        self.api.wait_for_completion(pending, None, max(0, int(timeout_ms)))


def _sha256_file(path: Path, missing_category: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ColibriStage2Failure(missing_category) from exc
    return digest.hexdigest()


def _assert_regular_no_reparse(path: Path, missing_category: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ColibriStage2Failure(missing_category) from exc
    attrs = getattr(info, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ColibriStage2Failure("reparse_point_rejected")
    if not stat_module.S_ISREG(info.st_mode):
        raise ColibriStage2Failure("reparse_point_rejected")


def _verify_identity(
    path: Path,
    *,
    expected_basename: str,
    expected_size: int,
    expected_sha256: str,
    missing_category: str,
    mismatch_category: str,
) -> None:
    _assert_regular_no_reparse(path, missing_category)
    if path.name != expected_basename:
        raise ColibriStage2Failure(missing_category)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ColibriStage2Failure(missing_category) from exc
    if size != expected_size:
        raise ColibriStage2Failure(mismatch_category)
    if _sha256_file(path, missing_category) != expected_sha256:
        raise ColibriStage2Failure(mismatch_category)


def _verify_no_unknown_shards(converted_model_dir: Path, manifest: OlmoeModelManifest) -> None:
    expected = manifest.all_shard_basenames
    try:
        found = {entry.name for entry in converted_model_dir.glob("*.safetensors") if entry.is_file()}
    except OSError as exc:
        raise ColibriStage2Failure("missing_converted_shard") from exc
    extra = found - expected
    if extra:
        raise ColibriStage2Failure("unknown_converted_shard")
    missing = expected - found
    if missing:
        raise ColibriStage2Failure("missing_converted_shard")


def _pick_env(inherited: Mapping[str, str], *names: str) -> str | None:
    folded = {key.casefold(): value for key, value in inherited.items()}
    for name in names:
        value = folded.get(name.casefold())
        if value:
            return value
    return None


def build_runner_environment(
    *, converted_model_dir: Path, session_dir: Path, parent_environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The exact, closed seven-key child environment. No user env is
    inherited except the fixed platform keys every Windows process needs;
    TEMP/TMP always point at this run's own private reference session
    directory, never the caller's general (and much less isolated) temp
    directory."""

    inherited = os.environ if parent_environment is None else parent_environment
    system_root = _pick_env(inherited, "SystemRoot", "WINDIR")
    system_drive = _pick_env(inherited, "SystemDrive")
    windir = _pick_env(inherited, "WINDIR", "SystemRoot")
    if not all((system_root, system_drive, windir)):
        raise ColibriStage2Failure("platform_unsupported")
    session_path = str(session_dir)
    env = {
        "SNAP": str(converted_model_dir),
        "OMP_NUM_THREADS": "12",
        "SystemRoot": system_root,
        "SystemDrive": system_drive,
        "WINDIR": windir,
        "TEMP": session_path,
        "TMP": session_path,
    }
    if set(env) != CHILD_ENV_FIXED_KEYS:
        raise AssertionError("runner child environment is not closed")
    return env


def _default_interactive_check() -> bool:
    import sys

    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def run_one_token_proof(
    *,
    olmoe_exe: Path,
    converted_model_dir: Path,
    api: LifecycleApi,
    approved: bool,
    interactive_check: Callable[[], bool] = _default_interactive_check,
    resource_probe: ResourceProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
    reference_session_parent: Path | None = None,
) -> OneTokenRunResult:
    """Run ``olmoe.exe 8 8 <private-derived-ref-path>`` exactly once.

    Fails closed with ``reviewed_model_manifest_unavailable`` before any
    file is opened if the reviewed registry has no entry for the pinned
    model revision. Every other precondition is checked before process
    creation.
    """

    manifest = require_reviewed_manifest(PINNED_MODEL_REVISION, PINNED_COLIBRI_COMMIT)

    if not approved or not interactive_check():
        raise ColibriStage2Failure("noninteractive_approval_rejected")

    # Validate every approved directory chain -- ordinary directory, no
    # symlink/junction/reparse point anywhere down to the drive/root
    # anchor -- before any file is opened or any process is launched.
    require_ordinary_directory(
        olmoe_exe.parent, missing_category="executable_not_found", reparse_category="reparse_point_rejected"
    )
    resolved_model_dir = require_ordinary_directory(
        converted_model_dir, missing_category="missing_converted_shard", reparse_category="reparse_point_rejected"
    )
    require_direct_child_path(resolved_model_dir, manifest.config_basename, category="reparse_point_rejected")
    for basename in manifest.shard_basenames:
        require_direct_child_path(resolved_model_dir, basename, category="reparse_point_rejected")

    _verify_identity(
        olmoe_exe,
        expected_basename=manifest.engine_basename,
        expected_size=manifest.engine_size_bytes,
        expected_sha256=manifest.engine_sha256,
        missing_category="executable_not_found",
        mismatch_category="runtime_identity_mismatch",
    )
    _verify_identity(
        converted_model_dir / manifest.config_basename,
        expected_basename=manifest.config_basename,
        expected_size=manifest.config_size_bytes,
        expected_sha256=manifest.config_sha256,
        missing_category="missing_converted_shard",
        mismatch_category="missing_converted_shard",
    )
    _verify_no_unknown_shards(converted_model_dir, manifest)
    for basename, size, digest in zip(manifest.shard_basenames, manifest.shard_size_bytes, manifest.shard_sha256):
        _verify_identity(
            converted_model_dir / basename,
            expected_basename=basename,
            expected_size=size,
            expected_sha256=digest,
            missing_category="missing_converted_shard",
            mismatch_category="missing_converted_shard",
        )

    if canonical_reference_sha256() != manifest.ref_sha256:
        raise ColibriStage2Failure("reference_hash_mismatch")

    # The private reference session is created -- and its own directory
    # chain validated -- before the child environment is built, since the
    # child's TEMP/TMP point directly at this session directory rather
    # than the caller's general temporary directory.
    session_dir = create_private_reference_session(reference_session_parent)
    require_ordinary_directory(
        session_dir, missing_category="reference_write_failed", reparse_category="reference_write_failed"
    )
    environment = build_runner_environment(converted_model_dir=converted_model_dir, session_dir=session_dir)

    reference: ReferenceArtifact | None = None
    process: CreatedProcess | None = None
    job: Any = None
    job_assigned = False
    pump: _SplitStreamPump | None = None
    primary_failure: ColibriStage2Failure | None = None
    cleanup_failed = False
    reference_removed = False
    matched_count: int | None = None
    exit_code: int | None = None
    resources = _UNAVAILABLE_RESOURCE_EVIDENCE
    started = clock()

    try:
        reference = write_private_reference(session_dir)
        if reference.sha256 != manifest.ref_sha256 or reference.size_bytes != manifest.ref_size_bytes:
            raise ColibriStage2Failure("reference_hash_mismatch")

        setup_deadline = clock() + _SETUP_DEADLINE_SECONDS
        process = api.create_suspended(
            olmoe_exe, (CAP_ARGUMENT, BITS_ARGUMENT, str(reference.path)), environment
        )
        if not api.process_image_matches(process, olmoe_exe):
            raise ColibriStage2Failure("runtime_identity_mismatch")
        if _sha256_file(olmoe_exe, "executable_identity_unavailable") != manifest.engine_sha256:
            raise ColibriStage2Failure("runtime_identity_mismatch")

        job = api.create_job()
        api.configure_kill_on_close(job)
        api.assign_process(job, process)
        job_assigned = True
        if not api.verify_job_assignment(job, process):
            raise ColibriStage2Failure("job_assignment_failed")
        if clock() >= setup_deadline:
            raise ColibriStage2Failure("timeout", timeout_ms=int(_SETUP_DEADLINE_SECONDS * 1000))

        pump = _SplitStreamPump(api, process)
        pump.post_initial()

        deadline = started + _TOTAL_RUN_DEADLINE_SECONDS
        api.resume_process(process)
        exited = False
        while True:
            pump.service()
            if pump.stdout.overflowed or pump.stderr.overflowed:
                raise ColibriStage2Failure(
                    "output_overflow", bytes_observed=min(_MAX_STREAM_BYTES + 1, 2**31 - 1)
                )
            if not exited:
                exit_code = api.process_exit_code(process)
                if exit_code is not None:
                    exited = True
            if exited and pump.all_finished:
                break
            remaining = deadline - clock()
            if remaining <= 0:
                raise ColibriStage2Failure("timeout", timeout_ms=int(_TOTAL_RUN_DEADLINE_SECONDS * 1000))
            # Always wait at least 1ms: rounding a small remainder down to 0
            # would ask for a zero-length wait forever without the deadline
            # check ever observing forward progress.
            pump.wait(max(1, int(min(_WAIT_SLICE_MS, remaining * 1000))))

        if exit_code != 0:
            raise ColibriStage2Failure(
                "nonzero_exit", exit_code=max(0, min(exit_code if exit_code is not None else 1, 2**31 - 1))
            )
        if pump.stderr.bytes_value():
            raise ColibriStage2Failure("stderr_present")

        stdout_text = pump.stdout.bytes_value().decode("utf-8", errors="replace")
        lines = [line for line in stdout_text.splitlines() if line]
        matches = [_MATCH_LINE.fullmatch(line) for line in lines]
        matches = [match for match in matches if match is not None]
        if len(matches) == 0:
            raise ColibriStage2Failure("malformed_output")
        if len(matches) > 1:
            raise ColibriStage2Failure("duplicate_match_line")
        matched_count = int(matches[0].group(1))
        expected_count = int(matches[0].group(2))
        if matched_count != 1 or expected_count != 1:
            raise ColibriStage2Failure(
                "match_count_mismatch",
                matched_count=max(0, min(matched_count, 2**31 - 1)),
                expected_count=max(0, min(expected_count, 2**31 - 1)),
            )
        if canonical_reference_sha256() != manifest.ref_sha256:
            raise ColibriStage2Failure("token_identity_mismatch")
    except ColibriStage2Failure as exc:
        primary_failure = exc
    except IsolatedServerFailure as exc:
        # A real WindowsLifecycleApi call failed with its own (PR #40)
        # closed category. This runner exposes only its own closed
        # vocabulary, so the underlying category string is never
        # propagated directly -- but it is translated truthfully rather
        # than collapsed into one blanket category regardless of cause.
        primary_failure = ColibriStage2Failure(
            _ISOLATED_SERVER_FAILURE_CATEGORY_MAP.get(exc.category, _DEFAULT_ISOLATED_SERVER_FAILURE_CATEGORY)
        )
    finally:
        cleanup_deadline = clock() + _CLEANUP_DEADLINE_SECONDS
        if process is not None and resource_probe is not None:
            try:
                resources = resource_probe(job, process)
            except Exception:  # noqa: BLE001 - resource evidence is always best-effort
                resources = _UNAVAILABLE_RESOURCE_EVIDENCE
        if process is not None:
            try:
                if job is not None and job_assigned:
                    api.terminate_job(job)
                else:
                    api.terminate_process(process)
                wait_ms = int(max(0.0, cleanup_deadline - clock()) * 1000)
                if not api.wait_process(process, min(5000, wait_ms)):
                    cleanup_failed = True
            except (ColibriStage2Failure, IsolatedServerFailure):
                cleanup_failed = True
            if cancel_pending_pipe_io(
                api, (process.stdout, process.stderr), deadline=cleanup_deadline, clock=clock
            ):
                cleanup_failed = True
            try:
                if api.descendant_process_ids(process.process_id):
                    cleanup_failed = True
            except (ColibriStage2Failure, IsolatedServerFailure):
                cleanup_failed = True
            for handle in (
                getattr(process, "thread_handle", None),
                getattr(process, "process_handle", None),
                job,
            ):
                if handle is not None:
                    try:
                        api.close_handle(handle)
                    except (ColibriStage2Failure, IsolatedServerFailure):
                        cleanup_failed = True
        if reference is not None:
            try:
                delete_private_reference(reference)
                reference_removed = True
            except ColibriStage2Failure:
                cleanup_failed = True
        try:
            teardown_private_reference_session(session_dir)
        except ColibriStage2Failure:
            cleanup_failed = True

    orphan_free = not cleanup_failed

    if cleanup_failed:
        raise ColibriStage2Failure("cleanup_failed")
    if primary_failure is not None:
        raise primary_failure

    elapsed_ms = round((clock() - started) * 1000)
    matching_line = f"Matching tokens: {matched_count}/1".encode("ascii")
    # Bound to the reviewed engine identity, every reviewed model identity
    # (config + all three shards), the reference identity, and the exact
    # 1/1 evidence -- not just the reference hash and the matching line --
    # so the evidence changes if any pinned identity changes.
    evidence_payload = b"|".join(
        (
            manifest.engine_sha256.encode("ascii"),
            manifest.config_sha256.encode("ascii"),
            *(digest.encode("ascii") for digest in manifest.shard_sha256),
            manifest.ref_sha256.encode("ascii"),
            matching_line,
        )
    )
    evidence_sha256 = hashlib.sha256(evidence_payload).hexdigest()
    return OneTokenRunResult(
        category="passed",
        ok=True,
        matched_count=matched_count,
        expected_count=1,
        token_id=EXPECTED_GENERATED_TOKEN_ID,
        evidence_sha256=evidence_sha256,
        elapsed_ms=max(0, elapsed_ms),
        exit_code=exit_code,
        cleanup_complete=not cleanup_failed,
        orphan_free=orphan_free,
        reference_removed=reference_removed,
        resources=resources,
    )
