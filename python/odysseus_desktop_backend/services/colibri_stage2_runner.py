"""Developer-only real one-token runner for Colibrì Stage 2A (OLMoE).

This module launches the pinned ``olmoe.exe`` exactly once against a
private, embedded-token derived reference, and proves ``Matching tokens:
1/1`` for the single reviewed generated token id (7785). It reuses the
suspended-process, kill-on-close-Job-Object, and bounded-overlapped-pipe
primitives already reviewed in the PR #40 isolated-server stack rather than
reimplementing Windows process lifecycle handling.

The manifest gate is checked first, before any file is opened or any process
is created, and it is the *only* source of the identities, the cap/bits
arguments, and the token oracle this module uses: there is no parameter here
for an expected hash, size, model revision, engine identity, converter
identity, expected token, or substitute registry.

The command grammar is fixed by ``build_token_command``:

    olmoe.exe <cap> <bits> <derived-reference-path>

with ``cap`` and ``bits`` read from the reviewed registry entry (both ``8``)
and the reference path pointing at a file this process derived in a private
session directory from embedded token arrays -- never a caller-supplied path
and never a tokenizer.

Nothing in this module performs a real launch on its own: the caller must
pass ``approved=True`` from an interactive terminal.
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
    EVIDENCE_STATE_MEASURED,
    EVIDENCE_STATE_UNAVAILABLE,
    EXIT_CATEGORY_CLEAN,
    EXIT_CATEGORY_NONZERO,
    EXIT_CATEGORY_NOT_OBSERVED,
    EXIT_CATEGORY_TIMED_OUT,
    PINNED_COLIBRI_COMMIT,
    PINNED_MODEL_REVISION,
    RESUME_LEDGER_BASENAME,
    TOKEN_RUN_EVIDENCE_SCHEMA_VERSION,
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
    reference_object,
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

# Returns ``(peak whole-tree bytes | None, state)``. The state is
# ``measured`` only when the OS positively reported a value.
TreeMemoryProbe = Callable[[Any], tuple[int | None, str]]


def default_tree_memory_probe(job: Any) -> tuple[int | None, str]:
    """Peak whole-tree memory for the Job Object that owns the child.

    Reuses the one reviewed ``QueryInformationJobObject`` path already used
    for bounded conversion rather than declaring a second copy of the ctypes
    structures. Reports ``measured`` only when the OS positively returned a
    peak; every failure, and every non-Windows platform, yields
    ``(None, "unavailable")`` -- never a zero standing in for "unknown".
    """

    if job is None:
        return None, EVIDENCE_STATE_UNAVAILABLE
    try:
        from odysseus_desktop_backend.services.colibri_stage2_conversion import peak_job_memory_bytes

        _, peak_tree_bytes = peak_job_memory_bytes(job)
    except Exception:  # noqa: BLE001 - resource evidence is always best-effort
        return None, EVIDENCE_STATE_UNAVAILABLE
    if not isinstance(peak_tree_bytes, int) or isinstance(peak_tree_bytes, bool) or peak_tree_bytes <= 0:
        return None, EVIDENCE_STATE_UNAVAILABLE
    return peak_tree_bytes, EVIDENCE_STATE_MEASURED


@dataclass(frozen=True, slots=True)
class RunIdentityEvidence:
    """Exactly which engine, model, converter, and reference were run.

    Every field is either a pinned identifier or a SHA-256 taken from the
    reviewed registry entry. There is no path, filename outside the pinned
    basenames, username, environment value, or prompt text here, so this
    record is safe to persist verbatim.
    """

    model_repository: str
    model_revision: str
    colibri_commit: str
    engine_sha256: str
    converter_kind: str
    converter_sha256: str
    config_sha256: str
    shard_sha256: tuple[str, str, str]
    reference_sha256: str
    cap_argument: str
    bits_argument: str


@dataclass(frozen=True, slots=True)
class LatencyEvidence:
    """Wall-clock latencies actually observed, each with its own state.

    ``startup_latency_ms`` spans process resume to the engine's *first*
    observed output byte. The engine's only reviewed output is its match
    line, which it emits after loading the model and generating the token,
    so this figure is a combined model-load-plus-generation measurement and
    an upper bound on model load alone -- no finer decomposition is
    observable from this engine's output, and none is invented here.

    ``one_token_latency_ms`` spans process resume to the observed process
    exit: end-to-end latency for the run that produces and verifies exactly
    one token.

    A state of ``unavailable`` means the corresponding endpoint was never
    observed (e.g. the child produced no output, or timed out without
    exiting). ``None`` is then the only value, so a missing measurement can
    never be misread as ``0``.
    """

    startup_latency_ms: int | None
    startup_latency_state: str
    one_token_latency_ms: int | None
    one_token_latency_state: str


@dataclass(frozen=True, slots=True)
class OneTokenRunResult:
    """The only fields a caller may ever observe from a run attempt.

    Deliberately absent: captured stdout/stderr (bounded stream bytes are
    parsed in memory and discarded), any filesystem path, the child
    environment, the username, and the prompt text. Only closed categories,
    pinned identities, small integers, and measurement states escape.
    """

    category: str
    ok: bool
    evidence_schema_version: str
    identities: RunIdentityEvidence
    matched_count: int | None
    expected_count: int
    expected_token_id: int
    generated_token_id: int | None
    exit_category: str
    evidence_sha256: str | None
    elapsed_ms: int
    exit_code: int | None
    latency: LatencyEvidence
    peak_tree_memory_bytes: int | None
    peak_tree_memory_state: str
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

    def __init__(self, api: LifecycleApi, process: CreatedProcess, clock: Callable[[], float]) -> None:
        self.api = api
        self.pipes: tuple[Any, Any] = (process.stdout, process.stderr)
        self.stdout = _BoundedStreamCapture()
        self.stderr = _BoundedStreamCapture()
        self._captures = (self.stdout, self.stderr)
        self._finished: set[int] = set()
        self._clock = clock
        # Timestamp of the first output byte seen on either stream, used
        # solely to derive the startup latency. It is a monotonic clock
        # reading, never any part of the output itself.
        self.first_output_at: float | None = None

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
                        if self.first_output_at is None:
                            self.first_output_at = self._clock()
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


def _verify_converted_directory_contents(converted_model_dir: Path, manifest: OlmoeModelManifest) -> None:
    """Enumerate the converted directory and reject anything unexpected.

    The full direct-child listing is checked, not just ``*.safetensors``: a
    stray subdirectory, an extra config, a leftover partial, or any other
    unknown artifact is rejected rather than ignored.

    Exactly one non-attested name is tolerated -- the conversion resume
    ledger. It is a by-product of the bounded converter that legitimately
    sits beside the artifacts, and it is *never* opened, parsed, or trusted
    as authority for anything: every identity used by this runner comes from
    the reviewed registry entry and is re-verified against the files
    themselves. Tolerating its presence is not the same as believing it.

    Reparse points and non-regular entries are rejected here as well, so a
    junction dropped into the directory cannot pass as an artifact.
    """

    required = manifest.expected_direct_child_basenames
    tolerated = required | {RESUME_LEDGER_BASENAME}
    try:
        with os.scandir(converted_model_dir) as scan:
            entries = sorted(entry.name for entry in scan)
    except OSError as exc:
        raise ColibriStage2Failure("missing_converted_shard") from exc

    found: set[str] = set()
    for name in entries:
        if name not in tolerated:
            raise ColibriStage2Failure("unknown_converted_shard")
        candidate = require_direct_child_path(
            converted_model_dir, name, category="reparse_point_rejected"
        )
        _assert_regular_no_reparse(candidate, "unknown_converted_shard")
        found.add(name)

    if required - found:
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


def build_token_command(
    manifest: OlmoeModelManifest, olmoe_exe: Path, reference_path: Path
) -> tuple[Path, tuple[str, str, str]]:
    """The exact, closed real-token command grammar.

        olmoe.exe <cap> <bits> <derived-reference-path>

    ``cap`` and ``bits`` come from the reviewed registry entry (which pinned
    them to ``8``/``8`` at construction), never from a caller. Exactly three
    arguments are produced -- there is no flag, no prompt string, no model
    path, and no tokenizer on this command line. The engine locates the
    converted model through the closed ``SNAP`` environment key instead.
    """

    if olmoe_exe.name != manifest.engine_basename:
        raise ColibriStage2Failure("executable_not_found")
    if reference_path.name != manifest.ref_basename:
        raise ColibriStage2Failure("reference_hash_mismatch")
    return olmoe_exe, (manifest.cap_argument, manifest.bits_argument, str(reference_path))


def _verify_reference_contract(manifest: OlmoeModelManifest) -> None:
    """Prove the derived reference encodes exactly the reviewed contract.

    The token oracle is closed: the prompt ids and the single expected
    generated token id are compared between the reviewed registry entry and
    the in-process reference derivation. Neither side can be supplied by a
    caller, and the run is abandoned if they disagree by so much as one id.
    """

    payload = reference_object()
    prompt_ids = tuple(payload["prompt_ids"])
    full_ids = tuple(payload["full_ids"])
    if prompt_ids != tuple(manifest.prompt_token_ids):
        raise ColibriStage2Failure("token_identity_mismatch")
    if full_ids != prompt_ids + (manifest.expected_generated_token_id,):
        raise ColibriStage2Failure("token_identity_mismatch")
    if canonical_reference_sha256() != manifest.ref_sha256:
        raise ColibriStage2Failure("reference_hash_mismatch")


def classify_process_exit(*, exit_code: int | None, timed_out: bool) -> str:
    """Map an observed process outcome onto the closed exit vocabulary.

    Never a raw status string or message: a deadline that fired is
    ``timed_out``, an exit nobody sampled is ``not_observed``, and an
    observed exit is ``clean_exit`` or ``nonzero_exit`` by its code alone.
    """

    if timed_out:
        return EXIT_CATEGORY_TIMED_OUT
    if exit_code is None:
        return EXIT_CATEGORY_NOT_OBSERVED
    return EXIT_CATEGORY_CLEAN if exit_code == 0 else EXIT_CATEGORY_NONZERO


def _latency_evidence(
    *, resumed_at: float | None, first_output_at: float | None, exit_observed_at: float | None
) -> LatencyEvidence:
    """Derive both latencies from positively observed clock readings only."""

    def span(end: float | None) -> tuple[int | None, str]:
        if resumed_at is None or end is None:
            return None, EVIDENCE_STATE_UNAVAILABLE
        return max(0, round((end - resumed_at) * 1000)), EVIDENCE_STATE_MEASURED

    startup_ms, startup_state = span(first_output_at)
    one_token_ms, one_token_state = span(exit_observed_at)
    return LatencyEvidence(
        startup_latency_ms=startup_ms,
        startup_latency_state=startup_state,
        one_token_latency_ms=one_token_ms,
        one_token_latency_state=one_token_state,
    )


def _identity_evidence(manifest: OlmoeModelManifest) -> RunIdentityEvidence:
    return RunIdentityEvidence(
        model_repository=manifest.model_repository,
        model_revision=manifest.model_revision,
        colibri_commit=manifest.colibri_commit,
        engine_sha256=manifest.engine_sha256,
        converter_kind=manifest.converter_kind,
        converter_sha256=manifest.converter_source_sha256,
        config_sha256=manifest.config_sha256,
        shard_sha256=tuple(manifest.shard_sha256),
        reference_sha256=manifest.ref_sha256,
        cap_argument=manifest.cap_argument,
        bits_argument=manifest.bits_argument,
    )


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
    tree_memory_probe: TreeMemoryProbe | None = default_tree_memory_probe,
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
    _verify_converted_directory_contents(resolved_model_dir, manifest)
    for basename, size, digest in zip(manifest.shard_basenames, manifest.shard_size_bytes, manifest.shard_sha256):
        _verify_identity(
            converted_model_dir / basename,
            expected_basename=basename,
            expected_size=size,
            expected_sha256=digest,
            missing_category="missing_converted_shard",
            mismatch_category="missing_converted_shard",
        )

    _verify_reference_contract(manifest)

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
    orphan_detected = False
    orphan_check_conclusive = False
    reference_removed = False
    matched_count: int | None = None
    exit_code: int | None = None
    resumed_at: float | None = None
    exit_observed_at: float | None = None
    resources = _UNAVAILABLE_RESOURCE_EVIDENCE
    peak_tree_memory_bytes: int | None = None
    peak_tree_memory_state = EVIDENCE_STATE_UNAVAILABLE
    timed_out = False
    started = clock()

    try:
        reference = write_private_reference(session_dir)
        if reference.sha256 != manifest.ref_sha256 or reference.size_bytes != manifest.ref_size_bytes:
            raise ColibriStage2Failure("reference_hash_mismatch")

        setup_deadline = clock() + _SETUP_DEADLINE_SECONDS
        executable, arguments = build_token_command(manifest, olmoe_exe, reference.path)
        process = api.create_suspended(executable, arguments, environment)
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

        pump = _SplitStreamPump(api, process, clock)
        pump.post_initial()

        deadline = started + _TOTAL_RUN_DEADLINE_SECONDS
        api.resume_process(process)
        resumed_at = clock()
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
                    exit_observed_at = clock()
            if exited and pump.all_finished:
                break
            remaining = deadline - clock()
            if remaining <= 0:
                timed_out = True
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
        # Sampled while the Job Object handle is still open and before it is
        # terminated/closed, since a closed job reports nothing.
        if process is not None and job is not None and tree_memory_probe is not None:
            try:
                peak_tree_memory_bytes, peak_tree_memory_state = tree_memory_probe(job)
            except Exception:  # noqa: BLE001 - resource evidence is always best-effort
                peak_tree_memory_bytes, peak_tree_memory_state = None, EVIDENCE_STATE_UNAVAILABLE
            if peak_tree_memory_state != EVIDENCE_STATE_MEASURED:
                peak_tree_memory_bytes = None
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
            # Orphan evidence is tracked separately from general cleanup
            # uncertainty: a surviving descendant is a specific, reportable
            # fact (`orphan_detected`), while a probe that could not answer
            # is cleanup uncertainty. Both fail closed, and `orphan_free` is
            # asserted only when the probe positively answered "none".
            try:
                if api.descendant_process_ids(process.process_id):
                    orphan_detected = True
                orphan_check_conclusive = True
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

    orphan_free = orphan_check_conclusive and not orphan_detected

    if orphan_detected:
        raise ColibriStage2Failure("orphan_detected")
    if cleanup_failed:
        raise ColibriStage2Failure("cleanup_failed")
    if primary_failure is not None:
        raise primary_failure

    elapsed_ms = round((clock() - started) * 1000)
    matching_line = f"Matching tokens: {matched_count}/1".encode("ascii")
    # Bound to every reviewed identity the run depended on -- engine, model
    # revision, executed converter, config, all three shards, the reference,
    # and the cap/bits/token contract -- plus the exact 1/1 evidence, so the
    # evidence hash changes if any pinned identity or argument changes.
    evidence_payload = b"|".join(
        (
            manifest.model_revision.encode("ascii"),
            manifest.colibri_commit.encode("ascii"),
            manifest.engine_sha256.encode("ascii"),
            manifest.converter_kind.encode("ascii"),
            manifest.converter_source_sha256.encode("ascii"),
            manifest.config_sha256.encode("ascii"),
            *(digest.encode("ascii") for digest in manifest.shard_sha256),
            manifest.ref_sha256.encode("ascii"),
            manifest.cap_argument.encode("ascii"),
            manifest.bits_argument.encode("ascii"),
            str(manifest.expected_generated_token_id).encode("ascii"),
            matching_line,
        )
    )
    evidence_sha256 = hashlib.sha256(evidence_payload).hexdigest()
    return OneTokenRunResult(
        category="passed",
        ok=True,
        evidence_schema_version=TOKEN_RUN_EVIDENCE_SCHEMA_VERSION,
        identities=_identity_evidence(manifest),
        matched_count=matched_count,
        expected_count=1,
        expected_token_id=manifest.expected_generated_token_id,
        # The engine never prints the token id; it prints its own comparison
        # against the reviewed reference. A confirmed 1/1 against a reference
        # whose verified digest encodes exactly one expected generated id is
        # therefore what proves the generated id -- and it is only reported
        # once that comparison has passed.
        generated_token_id=manifest.expected_generated_token_id,
        exit_category=classify_process_exit(exit_code=exit_code, timed_out=timed_out),
        evidence_sha256=evidence_sha256,
        elapsed_ms=max(0, elapsed_ms),
        exit_code=exit_code,
        latency=_latency_evidence(
            resumed_at=resumed_at,
            first_output_at=None if pump is None else pump.first_output_at,
            exit_observed_at=exit_observed_at,
        ),
        peak_tree_memory_bytes=peak_tree_memory_bytes,
        peak_tree_memory_state=peak_tree_memory_state,
        cleanup_complete=not cleanup_failed,
        orphan_free=orphan_free,
        reference_removed=reference_removed,
        resources=resources,
    )
