"""Download/conversion orchestration and evidence capture for Colibrì
Stage 2A.

This module never downloads model weights and never runs a converter with
its default (real) adapters unless the approved-execution gate passes --
which, in this commit, it structurally cannot, because
``REVIEWED_SOURCE_SHARD_MANIFEST`` is empty. It provides:

* a process-free, network-free dry-run plan describing the sequential
  download -> verify -> convert -> delete-source-shard sequence;
* the closed, immutable reviewed *source* manifest gate (basename, exact
  size, and SHA-256 for each of the four required upstream files), which
  fails closed with ``source_model_manifest_unreviewed`` while empty;
* the approved-execution precondition gate;
* path-safety-checked transactional per-shard and per-config primitives
  the real approved sequence calls, exercised only with injected fakes in
  tests;
* default real adapters (a pinned-revision single-file downloader and a
  pinned-converter-script invoker) used only once a reviewed manifest and
  explicit approval are both present;
* a closed, privacy-safe conversion capture shape distinct from
  ``OlmoeModelManifest`` -- it can never itself authorize inference.
"""

from __future__ import annotations

import hashlib
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

from odysseus_desktop_backend.services.colibri_stage2_common import (
    ALLOWED_CONVERSION_DEPENDENCY_NAMES,
    APPROVAL_STATEMENT,
    APPROX_DOWNLOAD_BYTES,
    CONVERSION_CAPTURE_SCHEMA_VERSION,
    CONVERSION_CAPTURE_STATE,
    DEVIATION_STATEMENT,
    EXPECTED_CONFIG_BASENAME,
    EXPECTED_CONVERTER_SCRIPT_BASENAME,
    EXPECTED_SHARD_BASENAMES,
    PINNED_COLIBRI_COMMIT,
    PINNED_LICENSE_IDENTIFIER,
    PINNED_MODEL_REPOSITORY,
    PINNED_MODEL_REVISION,
    REQUIRED_FREE_SPACE_BYTES,
    ColibriStage2Failure,
    is_hex64,
    is_safe_basename,
    is_simple_version,
)
from odysseus_desktop_backend.services.colibri_stage2_path_safety import (
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


# Immutable and, in this correction commit, empty: the complete official
# upstream identities (exact basename + exact size + SHA-256) for
# config.json and the three safetensors shards have not yet been reviewed
# and committed. Populating this requires a separate, dedicated,
# human-reviewed commit with real, non-truncated, officially published
# values for every one of the four ``REQUIRED_SOURCE_FILES`` entries --
# never a caller-supplied override.
REVIEWED_SOURCE_SHARD_MANIFEST: Mapping[str, SourceShardEntry] = MappingProxyType({})


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
            "for each of the three shards, in order: download and verify only that shard",
            "create a new empty per-shard converter-output directory",
            f"run the unmodified pinned {EXPECTED_CONVERTER_SCRIPT_BASENAME} through the current venv Python",
            "verify the temporary output contains exactly config.json and that one converted shard",
            "verify the converted config remains byte-identical, then hash and record the converted shard",
            "atomically move the converted shard into the final directory only if absent",
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
) -> Mapping[str, SourceShardEntry]:
    """The full approved-mode gate, checked before any network activity.

    Returns the reviewed source manifest on success. Every check here runs
    before a single byte is downloaded, and while the reviewed source
    manifest stays empty, no other check result can ever unblock a run.
    """

    reviewed = require_reviewed_source_manifest()
    if not approved or not interactive_check():
        raise ColibriStage2Failure("noninteractive_approval_rejected")
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
    def convert(self, *, model_dir: Path, output_dir: Path) -> None: ...


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
        partial_path = destination.parent / f"{basename}.partial"
        url = (
            f"https://huggingface.co/{PINNED_MODEL_REPOSITORY}/resolve/"
            f"{PINNED_MODEL_REVISION}/{basename}"
        )
        digest = hashlib.sha256()
        observed = 0
        started = self.clock()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "odysseus-colibri-stage2"})
            with urllib.request.urlopen(request, timeout=self.socket_timeout_seconds) as response:
                with partial_path.open("wb") as handle:
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
        os.replace(partial_path, destination)


@dataclass(frozen=True, slots=True)
class PinnedScriptConverter:
    """Default real converter adapter.

    Invokes the exact pinned ``convert_olmoe.py`` through the *current*
    venv's Python interpreter (``sys.executable``), with explicit argv and
    ``shell=False``, under one absolute deadline. stdout/stderr are
    discarded rather than retained -- their content is never needed for
    pass/fail and must never appear in any evidence capture.
    """

    converter_script_path: Path
    absolute_deadline_seconds: float = 1800.0

    def convert(self, *, model_dir: Path, output_dir: Path) -> None:
        import subprocess

        if self.converter_script_path.name != EXPECTED_CONVERTER_SCRIPT_BASENAME:
            raise ColibriStage2Failure("conversion_failed")
        argv = [
            sys.executable,
            str(self.converter_script_path),
            "--model",
            str(model_dir),
            "--output",
            str(output_dir),
        ]
        try:
            subprocess.run(
                argv,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.absolute_deadline_seconds,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ColibriStage2Failure("conversion_failed") from exc


# ---------------------------------------------------------------------------
# Per-file / per-shard transactional primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShardTransactionResult:
    source_basename: str
    converted_basename: str
    source_deleted: bool
    converted_sha256: str
    converted_size_bytes: int
    elapsed_ms: int


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


def download_and_verify_config(
    *, expected_config: SourceShardEntry, destination_dir: Path, downloader: Downloader
) -> Path:
    """Step 1 of the approved sequence: download and verify config.json
    exactly once. The returned path stays available (never deleted here)
    for every subsequent converter call."""

    if expected_config.basename != EXPECTED_CONFIG_BASENAME:
        raise ColibriStage2Failure("unsafe_basename_rejected")
    resolved_destination_dir = require_ordinary_directory(
        destination_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    config_path = require_direct_child_path(
        resolved_destination_dir, expected_config.basename, category="unsafe_directory_rejected"
    )
    if config_path.exists():
        raise ColibriStage2Failure("destination_not_empty")

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
    return config_path


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

    if final_converted_path.exists():
        raise ColibriStage2Failure("converted_shard_already_exists")

    started = clock()

    # 3a: download and verify only this source shard.
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

    # 3b: a fresh, empty per-shard converter-output directory.
    temp_output_dir = Path(tempfile.mkdtemp(prefix="colibri-stage2-shard-", dir=resolved_temp_parent))

    primary_failure: ColibriStage2Failure | None = None
    converted_sha256 = ""
    converted_size_bytes = 0
    try:
        # 3c/3d: the exact pinned converter, explicit argv, shell=False,
        # bounded/deadlined -- enforced by the adapter itself.
        try:
            converter.convert(model_dir=resolved_destination_dir, output_dir=temp_output_dir)
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

        # 3h: atomically move into the final directory, only if absent.
        if final_converted_path.exists():
            raise ColibriStage2Failure("converted_shard_already_exists")
        try:
            os.replace(temp_shard_path, final_converted_path)
        except OSError as exc:
            raise ColibriStage2Failure("conversion_failed") from exc
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

    elapsed_ms = round((clock() - started) * 1000)
    return ShardTransactionResult(
        source_basename=source_basename,
        converted_basename=source_basename,
        source_deleted=True,
        converted_sha256=converted_sha256,
        converted_size_bytes=converted_size_bytes,
        elapsed_ms=max(0, elapsed_ms),
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
    converter_basename: str,
    converter_size_bytes: int,
    converter_sha256: str,
    downloader: Downloader,
    converter: Converter,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The complete approved download/conversion sequence.

    Unreachable while ``REVIEWED_SOURCE_SHARD_MANIFEST`` is empty --
    ``check_approved_preconditions`` (via ``require_reviewed_source_manifest``)
    is the very first thing this calls, before any directory is even
    validated. Once a reviewed manifest is committed, this is the complete,
    real, executable path: no second implementation is required.
    """

    reviewed = check_approved_preconditions(
        interactive_check=interactive_check,
        approved=approved,
        destination_dir=destination_dir,
        converted_dir=final_converted_dir,
        free_bytes_probe=free_bytes_probe,
        isolated_python_env_ready=isolated_python_env_ready,
        dependency_versions=dependency_versions,
    )

    total_started = clock()

    config_entry = reviewed[EXPECTED_CONFIG_BASENAME]
    config_path = download_and_verify_config(
        expected_config=config_entry, destination_dir=destination_dir, downloader=downloader
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
        )
        shard_results.append(result)

    # Step 6: copy/move the verified config into the final directory,
    # exactly once, only after every shard has succeeded.
    resolved_final_dir = require_ordinary_directory(
        final_converted_dir, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    final_config_path = require_direct_child_path(
        resolved_final_dir, EXPECTED_CONFIG_BASENAME, category="unsafe_directory_rejected"
    )
    if final_config_path.exists():
        raise ColibriStage2Failure("converted_shard_already_exists")
    try:
        os.replace(config_path, final_config_path)
    except OSError as exc:
        raise ColibriStage2Failure("conversion_failed") from exc

    converted_config_size_bytes = final_config_path.stat().st_size
    converted_config_sha256 = _sha256_file(final_config_path)
    total_elapsed_ms = round((clock() - total_started) * 1000)

    return build_conversion_capture(
        converter_basename=converter_basename,
        converter_size_bytes=converter_size_bytes,
        converter_sha256=converter_sha256,
        source_config=config_entry,
        converted_config_sha256=converted_config_sha256,
        converted_config_size_bytes=converted_config_size_bytes,
        shard_results=shard_results,
        dependency_versions=dependency_versions,
        total_elapsed_ms=max(0, total_elapsed_ms),
        cleanup_complete=True,
    )


# ---------------------------------------------------------------------------
# Privacy-safe conversion capture
# ---------------------------------------------------------------------------


def build_conversion_capture(
    *,
    converter_basename: str,
    converter_size_bytes: int,
    converter_sha256: str,
    source_config: SourceShardEntry,
    converted_config_sha256: str,
    converted_config_size_bytes: int,
    shard_results: Sequence[ShardTransactionResult],
    dependency_versions: Mapping[str, str],
    total_elapsed_ms: int,
    cleanup_complete: bool,
) -> dict[str, Any]:
    """A closed, privacy-safe capture with the complete reviewable identity
    set the next tiny registry-pinning commit needs.

    Never contains a path, username, environment value, or raw tool
    output, and never validates as an ``OlmoeModelManifest`` -- its keys
    never match that dataclass's constructor, its state is always
    ``unreviewed_conversion_capture``, and it can never itself authorize a
    real run.
    """

    if converter_basename != EXPECTED_CONVERTER_SCRIPT_BASENAME or not is_hex64(converter_sha256):
        raise ValueError("invalid converter identity")
    if isinstance(converter_size_bytes, bool) or not isinstance(converter_size_bytes, int) or converter_size_bytes <= 0:
        raise ValueError("invalid converter size_bytes")
    if not isinstance(source_config, SourceShardEntry) or source_config.basename != EXPECTED_CONFIG_BASENAME:
        raise ValueError("invalid source config identity")
    if not is_hex64(converted_config_sha256):
        raise ValueError("invalid converted config sha256")
    if (
        isinstance(converted_config_size_bytes, bool)
        or not isinstance(converted_config_size_bytes, int)
        or converted_config_size_bytes <= 0
    ):
        raise ValueError("invalid converted config size_bytes")
    if len(shard_results) != 3 or {result.source_basename for result in shard_results} != set(EXPECTED_SHARD_BASENAMES):
        raise ValueError("conversion capture requires exactly the three pinned converted shards")

    unknown = set(dependency_versions) - ALLOWED_CONVERSION_DEPENDENCY_NAMES
    if unknown:
        raise ValueError(f"unknown conversion dependency names: {sorted(unknown)}")
    for name, version in dependency_versions.items():
        if not is_safe_basename(name) or not is_simple_version(version):
            raise ValueError(f"invalid conversion dependency version for {name!r}")

    shards: list[dict[str, Any]] = []
    for result in shard_results:
        if not is_hex64(result.converted_sha256):
            raise ValueError("shard result has an invalid converted SHA-256")
        shards.append(
            {
                "source_basename": result.source_basename,
                "converted_basename": result.converted_basename,
                "source_deleted": bool(result.source_deleted),
                "converted_sha256": result.converted_sha256,
                "converted_size_bytes": max(0, int(result.converted_size_bytes)),
                "elapsed_ms": max(0, int(result.elapsed_ms)),
            }
        )

    return {
        "schema_version": CONVERSION_CAPTURE_SCHEMA_VERSION,
        "state": CONVERSION_CAPTURE_STATE,
        "model_repository": PINNED_MODEL_REPOSITORY,
        "model_revision": PINNED_MODEL_REVISION,
        "license_identifier": PINNED_LICENSE_IDENTIFIER,
        "colibri_commit": PINNED_COLIBRI_COMMIT,
        "converter_basename": converter_basename,
        "converter_size_bytes": max(0, int(converter_size_bytes)),
        "converter_sha256": converter_sha256,
        "source_config_basename": source_config.basename,
        "source_config_size_bytes": source_config.size_bytes,
        "source_config_sha256": source_config.sha256,
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


def main(argv: Sequence[str] | None = None) -> int:
    """Developer-only CLI. Defaults to dry-run; ``--approve`` requests the
    approved-execution gate, which fails closed in this correction commit
    because ``REVIEWED_SOURCE_SHARD_MANIFEST`` is empty -- using real
    (not hard-coded) venv and dependency detection either way."""

    import argparse
    import json

    parser = argparse.ArgumentParser(description="Colibrì Stage 2A OLMoE download/conversion plan")
    parser.add_argument("--destination", required=True, help="Target directory for the source download")
    parser.add_argument(
        "--converted-destination", required=True, help="Target directory for the final converted model"
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

    if args.approve:
        try:
            check_approved_preconditions(
                interactive_check=_default_interactive_check,
                approved=True,
                destination_dir=destination,
                converted_dir=converted_destination,
                free_bytes_probe=_default_free_bytes_probe,
                isolated_python_env_ready=_default_isolated_python_env_ready(),
                dependency_versions=_default_dependency_versions(),
            )
        except ColibriStage2Failure as exc:
            output["mode"] = "approved_rejected"
            output["rejection_category"] = exc.category

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
