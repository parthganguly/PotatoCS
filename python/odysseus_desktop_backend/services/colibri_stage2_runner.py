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
import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
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
from odysseus_desktop_backend.services.colibri_stage2_engine_output import (
    ParsedEngineOutput,
    parse_engine_output,
    verify_one_token_output,
)
from odysseus_desktop_backend.services.colibri_stage2_job_probe import (
    JobMemberProbe,
    JobTeardownEvidence,
    job_active_process_count,
    terminate_and_prove_job_empty,
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
_MAX_STREAM_BYTES = 4096
MAPPING_NO_METADATA: Mapping[str, int] = MappingProxyType({})
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
    """Latencies actually observed, each with its own state and provenance.

    Two are **engine-reported**, parsed from the pinned engine's own closed
    output lines:

    - ``model_load_latency_ms`` from ``resident weights loaded in <s>s``;
    - ``generation_latency_ms`` from the ``(<s>s for 1 tokens)`` field of the
      ``Speed:`` line.

    Two are **independently measured** by this process from a monotonic
    clock:

    - ``end_to_end_latency_ms``, process resume to the observed process exit;
    - ``first_output_latency_ms``, process resume to the first observed
      output byte. This is *only* that: the pinned engine prints its banner
      before ``model_init``, so this figure is neither model-load latency nor
      an upper bound on it, and no such claim is made for it anywhere.

    A state of ``unavailable`` means the value was not positively observed --
    the engine timings are unavailable on any run whose output was never
    successfully parsed. ``None`` is then the only value, so a missing
    measurement can never be misread as ``0``.
    """

    model_load_latency_ms: int | None
    model_load_latency_state: str
    generation_latency_ms: int | None
    generation_latency_state: str
    end_to_end_latency_ms: int | None
    end_to_end_latency_state: str
    first_output_latency_ms: int | None
    first_output_latency_state: str


_UNAVAILABLE_LATENCY_EVIDENCE = LatencyEvidence(
    model_load_latency_ms=None,
    model_load_latency_state=EVIDENCE_STATE_UNAVAILABLE,
    generation_latency_ms=None,
    generation_latency_state=EVIDENCE_STATE_UNAVAILABLE,
    end_to_end_latency_ms=None,
    end_to_end_latency_state=EVIDENCE_STATE_UNAVAILABLE,
    first_output_latency_ms=None,
    first_output_latency_state=EVIDENCE_STATE_UNAVAILABLE,
)


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
    # Engine-reported: the numerator of its own ``Matching tokens`` line,
    # retained wherever it was safely parsed -- including on a failed run.
    matched_count: int | None
    # What the reviewed contract requires. Always 1, never engine-supplied.
    contract_expected_count: int
    # Engine-reported: the denominator of its own ``Matching tokens`` line.
    # Kept separate from ``contract_expected_count`` so a run where the engine
    # disagreed about how many tokens were even expected shows both numbers
    # rather than one silently standing in for the other.
    engine_reported_expected_count: int | None
    expected_token_id: int
    # Always read from the engine's own ``C engine :`` line, retained as soon
    # as it is parsed and before any comparison. On a wrong-token run this
    # holds the actual wrong integer. It is never assigned from
    # ``expected_token_id``; ``None`` means no single generated token was
    # parsed at all.
    generated_token_id: int | None
    exit_category: str
    evidence_sha256: str | None
    elapsed_ms: int
    exit_code: int | None
    latency: LatencyEvidence
    peak_tree_memory_bytes: int | None
    peak_tree_memory_state: str
    cleanup_complete: bool
    # Whether a private session directory was actually created, and whether
    # its bounded removal then completed. These are distinct from
    # ``reference_removed``: a run can create a session and fail before ever
    # writing a reference file into it, and that session still has to be
    # removed and accounted for.
    session_created: bool
    reference_session_removed: bool
    job_empty_proven: bool
    job_member_count: int | None
    root_exit_confirmed: bool
    descendant_count: int | None
    orphan_free: bool
    reference_removed: bool
    resources: ResourceEvidence
    failure_metadata: Mapping[str, int] = MAPPING_NO_METADATA
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
    *,
    resumed_at: float | None,
    first_output_at: float | None,
    exit_observed_at: float | None,
    parsed: ParsedEngineOutput | None,
) -> LatencyEvidence:
    """Assemble latency evidence from positively observed values only.

    Engine-reported timings come solely from a successfully parsed output (so
    they are ``unavailable`` on any unparsed run); the two independently
    measured spans come solely from monotonic clock readings.
    """

    def span(end: float | None) -> tuple[int | None, str]:
        if resumed_at is None or end is None:
            return None, EVIDENCE_STATE_UNAVAILABLE
        return max(0, round((end - resumed_at) * 1000)), EVIDENCE_STATE_MEASURED

    def engine_ms(seconds: float | None) -> tuple[int | None, str]:
        if seconds is None:
            return None, EVIDENCE_STATE_UNAVAILABLE
        return max(0, round(seconds * 1000)), EVIDENCE_STATE_MEASURED

    end_to_end_ms, end_to_end_state = span(exit_observed_at)
    first_output_ms, first_output_state = span(first_output_at)
    model_load_ms, model_load_state = engine_ms(None if parsed is None else parsed.model_load_seconds)
    generation_ms, generation_state = engine_ms(None if parsed is None else parsed.generation_seconds)
    return LatencyEvidence(
        model_load_latency_ms=model_load_ms,
        model_load_latency_state=model_load_state,
        generation_latency_ms=generation_ms,
        generation_latency_state=generation_state,
        end_to_end_latency_ms=end_to_end_ms,
        end_to_end_latency_state=end_to_end_state,
        first_output_latency_ms=first_output_ms,
        first_output_latency_state=first_output_state,
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


def _verify_preconditions(
    *, olmoe_exe: Path, converted_model_dir: Path, manifest: OlmoeModelManifest
) -> Path:
    """Every check that must pass before a process can exist.

    Returns the resolved, canonical converted-model directory. Read-only:
    nothing here creates a file, a directory, or a process, which is what
    lets its failures be raised rather than reported as an attempt.

    Validates each approved directory chain -- ordinary directory, no
    symlink/junction/reparse point anywhere down to the drive/root anchor --
    before any file is opened, then the engine, config, whole-directory
    contents, and every shard against the reviewed registry entry, then the
    derived reference against the reviewed token contract.
    """

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
    return resolved_model_dir


REFERENCE_SESSION_PARENT_BASENAME = "runtime-temp"


def default_reference_session_parent(resolved_model_dir: Path) -> Path:
    """The deterministic private-session parent for the real run.

    Derived as the ordinary sibling ``<converted-model-dir>/../runtime-temp``
    and never from ``TEMP``/``TMP``. The caller's temp spelling is not a
    trustworthy input: the first real invocation was handed
    ``C:\\Users\\PARTHG~1\\AppData\\Local\\Temp``, a Windows 8.3 short-name
    alias whose canonical form is the long ``Parth Ganguly`` spelling. The
    post-creation ordinary-directory check compares the original lexical path
    against its resolution and correctly rejected the mismatch -- but only
    after the directory had been created, so an empty session was left
    behind. Deriving the parent from an already-canonicalised, already-proven
    location removes the whole class of alias mismatch rather than trying to
    normalise every spelling a shell might supply.

    An environment variable is never consulted, and there is deliberately no
    CLI option: this is a derived location, not a configurable one.

    The directory must already exist and is validated with the same full
    lexical-ancestor, ordinary-directory and non-reparse guarantees as every
    other approved directory, before anything is created inside it. It is
    never created here -- a missing parent is a failure, not something to
    provision silently.
    """

    candidate = require_direct_child_path(
        resolved_model_dir.parent,
        REFERENCE_SESSION_PARENT_BASENAME,
        category="reference_write_failed",
    )
    return require_ordinary_directory(
        candidate,
        missing_category="reference_write_failed",
        reparse_category="reference_write_failed",
    )


def _evidence_digest(manifest: OlmoeModelManifest, generated_token_id: int, matched_count: int) -> str:
    """Bind the evidence hash to every reviewed identity and the real result.

    The generated token id folded in here is the one parsed from the engine's
    own output, so this digest cannot be reproduced by a run that failed to
    generate the reviewed token.
    """

    payload = b"|".join(
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
            str(generated_token_id).encode("ascii"),
            f"Matching tokens: {matched_count}/1".encode("ascii"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def attempt_one_token_proof(
    *,
    olmoe_exe: Path,
    converted_model_dir: Path,
    api: LifecycleApi,
    approved: bool,
    interactive_check: Callable[[], bool] = _default_interactive_check,
    resource_probe: ResourceProbe | None = None,
    tree_memory_probe: TreeMemoryProbe | None = default_tree_memory_probe,
    job_member_probe: JobMemberProbe = job_active_process_count,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    reference_session_parent: Path | None = None,
) -> OneTokenRunResult:
    """Attempt the one-token proof and always return closed evidence.

    Unlike :func:`run_one_token_proof`, this never raises for a closed
    operational failure: it returns a result whose ``ok`` is ``False``, whose
    ``category`` is the closed failure category, and which still carries
    every measurement and cleanup proof that was actually obtained. That is
    what lets a caller report a failed attempt without losing the evidence,
    and without a traceback.

    Precondition failures that occur before a process exists (manifest gate,
    approval, path safety, identity mismatches) still raise, because there is
    no attempt to describe: nothing was launched, nothing was measured, and
    no cleanup was owed.
    """

    manifest = require_reviewed_manifest(PINNED_MODEL_REVISION, PINNED_COLIBRI_COMMIT)

    if not approved or not interactive_check():
        raise ColibriStage2Failure("noninteractive_approval_rejected")

    resolved_model_dir = _verify_preconditions(
        olmoe_exe=olmoe_exe, converted_model_dir=converted_model_dir, manifest=manifest
    )

    # Everything from here on may leave something behind, so it all happens
    # inside the one cleanup-owned block below. `session_dir` is declared
    # absent first and is only ever read through a `is not None` guard: the
    # `finally` clause runs even when session creation itself raised.
    session_dir: Path | None = None
    session_created = False
    session_removed = False
    reference: ReferenceArtifact | None = None
    process: CreatedProcess | None = None
    job: Any = None
    job_assigned = False
    pump: _SplitStreamPump | None = None
    primary_failure: ColibriStage2Failure | None = None
    cleanup_failed = False
    reference_removed = False
    parsed: ParsedEngineOutput | None = None
    generated_token_id: int | None = None
    matched_count: int | None = None
    engine_reported_expected_count: int | None = None
    verified = False
    exit_code: int | None = None
    resumed_at: float | None = None
    exit_observed_at: float | None = None
    resources = _UNAVAILABLE_RESOURCE_EVIDENCE
    peak_tree_memory_bytes: int | None = None
    peak_tree_memory_state = EVIDENCE_STATE_UNAVAILABLE
    teardown: JobTeardownEvidence | None = None
    timed_out = False
    started = clock()

    try:
        # The session parent is resolved and proven *before* anything is
        # created inside it; the session itself is then created, validated,
        # and only afterwards used to build the child environment, since the
        # child's TEMP/TMP point at this session directory alone.
        session_parent = (
            default_reference_session_parent(resolved_model_dir)
            if reference_session_parent is None
            else reference_session_parent
        )
        session_dir = create_private_reference_session(session_parent)
        session_created = True
        require_ordinary_directory(
            session_dir,
            missing_category="reference_write_failed",
            reparse_category="reference_write_failed",
        )
        environment = build_runner_environment(
            converted_model_dir=converted_model_dir, session_dir=session_dir
        )

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

        # Parse the drained stdout, then run the independent comparison. The
        # bounded stream bytes are consumed here and never retained: only the
        # small parsed record survives this block.
        parsed = parse_engine_output(pump.stdout.bytes_value())

        # Retain what the engine actually said *before* judging it. A run that
        # generated the wrong token must report that wrong token, not a null
        # and never the expected value -- the observation is the whole point
        # of having run it.
        if len(parsed.generated_token_ids) == 1:
            generated_token_id = parsed.generated_token_ids[0]
        matched_count = parsed.matched_count
        engine_reported_expected_count = parsed.expected_count

        verify_one_token_output(parsed, expected_token_id=manifest.expected_generated_token_id)
        verified = True
        if canonical_reference_sha256() != manifest.ref_sha256:
            verified = False
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
    except Exception:  # noqa: BLE001 - an unexpected defect must not skip cleanup
        # Once a session directory may exist, no exception may escape this
        # function: escaping would leave the directory behind *and* let the
        # caller classify the run as a side-effect-free pre-launch rejection.
        # The exception object is discarded unread -- its message could carry
        # a local path -- and the run is reported as an internal failure whose
        # cleanup evidence is whatever actually happened below.
        primary_failure = ColibriStage2Failure("unexpected_internal_failure")
    finally:
        cleanup_deadline = clock() + _CLEANUP_DEADLINE_SECONDS
        if process is not None and resource_probe is not None:
            try:
                resources = resource_probe(job, process)
            except Exception:  # noqa: BLE001 - resource evidence is always best-effort
                resources = _UNAVAILABLE_RESOURCE_EVIDENCE
        # Sampled while the Job Object handle is still open and before the
        # job is terminated or closed, since a closed handle reports nothing.
        if process is not None and job is not None and tree_memory_probe is not None:
            try:
                peak_tree_memory_bytes, peak_tree_memory_state = tree_memory_probe(job)
            except Exception:  # noqa: BLE001 - resource evidence is always best-effort
                peak_tree_memory_bytes, peak_tree_memory_state = None, EVIDENCE_STATE_UNAVAILABLE
            if peak_tree_memory_state != EVIDENCE_STATE_MEASURED:
                peak_tree_memory_bytes = None
        if process is not None:
            # Terminate the complete job and prove, under the absolute
            # cleanup deadline, that the Job Object holds zero processes.
            # This is the primary ownership proof; the descendant
            # enumeration inside it is supplementary evidence only.
            teardown = terminate_and_prove_job_empty(
                api,
                job=job,
                process=process,
                job_assigned=job_assigned,
                member_probe=job_member_probe,
                deadline=cleanup_deadline,
                clock=clock,
                sleep=sleep,
                failure_types=(ColibriStage2Failure, IsolatedServerFailure),
            )
            if teardown.cleanup_failed:
                cleanup_failed = True
            if cancel_pending_pipe_io(
                api, (process.stdout, process.stderr), deadline=cleanup_deadline, clock=clock
            ):
                cleanup_failed = True
            # The job handle is closed last, only after the zero-member proof
            # and the peak-memory measurement have both completed: a closed
            # handle can answer neither query.
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
        # `reference_removed` stays False when no reference file was ever
        # written -- an unwritten file is not a removed one, and claiming
        # otherwise would report cleanup that never happened.
        if reference is not None:
            try:
                delete_private_reference(reference)
                reference_removed = True
            except ColibriStage2Failure:
                cleanup_failed = True
        # Bounded removal of the session directory is attempted on *every*
        # exit path once it may exist -- including a failure of the very
        # validation that follows its creation, which is what leaked an empty
        # session directory on the first real invocation.
        if session_dir is not None:
            try:
                teardown_private_reference_session(session_dir)
                session_removed = True
            except ColibriStage2Failure:
                cleanup_failed = True

    # `orphan_free` is asserted only after the Job Object itself reported
    # zero members: a descendant snapshot alone cannot prove an empty tree.
    job_empty_proven = teardown is not None and teardown.job_empty_proven
    orphan_detected = teardown is not None and teardown.orphan_detected
    orphan_free = (
        job_empty_proven
        and teardown is not None
        and teardown.descendant_probe_conclusive
        and not teardown.orphan_detected
    )

    failure: ColibriStage2Failure | None = None
    if orphan_detected:
        failure = ColibriStage2Failure("orphan_detected")
    elif cleanup_failed:
        failure = ColibriStage2Failure("cleanup_failed")
    elif primary_failure is not None:
        failure = primary_failure

    # `verified` -- not "a token was parsed" -- is what makes a run a pass.
    # A wrong generated token is parsed and reported, and must still fail.
    ok = failure is None and verified
    if not ok and failure is None:
        # No closed failure was recorded yet nothing was verified. That
        # combination must still fail, and truthfully: the output never
        # yielded the reviewed token.
        failure = ColibriStage2Failure("malformed_output")
    elapsed_ms = round((clock() - started) * 1000)
    return OneTokenRunResult(
        category="passed" if ok else failure.category,  # type: ignore[union-attr]
        ok=ok,
        evidence_schema_version=TOKEN_RUN_EVIDENCE_SCHEMA_VERSION,
        identities=_identity_evidence(manifest),
        matched_count=matched_count,
        contract_expected_count=1,
        engine_reported_expected_count=engine_reported_expected_count,
        expected_token_id=manifest.expected_generated_token_id,
        generated_token_id=generated_token_id,
        exit_category=classify_process_exit(exit_code=exit_code, timed_out=timed_out),
        evidence_sha256=(
            _evidence_digest(manifest, generated_token_id, matched_count)
            if ok and generated_token_id is not None and matched_count is not None
            else None
        ),
        elapsed_ms=max(0, elapsed_ms),
        exit_code=exit_code,
        latency=_latency_evidence(
            resumed_at=resumed_at,
            first_output_at=None if pump is None else pump.first_output_at,
            exit_observed_at=exit_observed_at,
            parsed=parsed,
        ),
        peak_tree_memory_bytes=peak_tree_memory_bytes,
        peak_tree_memory_state=peak_tree_memory_state,
        cleanup_complete=not cleanup_failed,
        session_created=session_created,
        reference_session_removed=session_removed,
        job_empty_proven=job_empty_proven,
        job_member_count=None if teardown is None else teardown.job_member_count,
        root_exit_confirmed=teardown is not None and teardown.root_exit_confirmed,
        descendant_count=None if teardown is None else teardown.descendant_count,
        orphan_free=orphan_free,
        reference_removed=reference_removed,
        resources=resources,
        failure_metadata=(
            MappingProxyType(dict(failure.numeric_metadata))
            if failure is not None
            else MAPPING_NO_METADATA
        ),
    )


def run_one_token_proof(
    *,
    olmoe_exe: Path,
    converted_model_dir: Path,
    api: LifecycleApi,
    approved: bool,
    interactive_check: Callable[[], bool] = _default_interactive_check,
    resource_probe: ResourceProbe | None = None,
    tree_memory_probe: TreeMemoryProbe | None = default_tree_memory_probe,
    job_member_probe: JobMemberProbe = job_active_process_count,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    reference_session_parent: Path | None = None,
) -> OneTokenRunResult:
    """Run ``olmoe.exe 8 8 <private-derived-ref-path>`` exactly once, or raise.

    The raising form of :func:`attempt_one_token_proof`, kept for callers that
    want an exception rather than a record. Fails closed with
    ``reviewed_model_manifest_unavailable`` before any file is opened if the
    reviewed registry has no entry for the pinned model revision. Every other
    precondition is checked before process creation.
    """

    result = attempt_one_token_proof(
        olmoe_exe=olmoe_exe,
        converted_model_dir=converted_model_dir,
        api=api,
        approved=approved,
        interactive_check=interactive_check,
        resource_probe=resource_probe,
        tree_memory_probe=tree_memory_probe,
        job_member_probe=job_member_probe,
        clock=clock,
        sleep=sleep,
        reference_session_parent=reference_session_parent,
    )
    if not result.ok:
        raise ColibriStage2Failure(result.category, **dict(result.failure_metadata))
    return result
