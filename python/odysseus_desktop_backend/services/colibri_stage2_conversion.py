"""Download/conversion plan and evidence capture for Colibrì Stage 2A.

This module never downloads model weights and never runs a converter
itself in this implementation commit. It provides:

* a process-free, network-free dry-run plan describing the sequential
  download -> verify -> convert -> delete-source-shard sequence;
* the approved-execution precondition gate, which fails closed with
  ``source_model_manifest_unreviewed`` because
  ``REVIEWED_SOURCE_SHARD_SHA256`` is empty in this commit;
* the per-shard transactional primitive the future approved sequence will
  call, exercised only with injected fakes in tests;
* a closed, privacy-safe conversion capture shape distinct from
  ``OlmoeModelManifest`` — it can never itself authorize inference.
"""

from __future__ import annotations

import hashlib
import stat as stat_module
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
    EXPECTED_CONVERTER_SCRIPT_BASENAME,
    EXPECTED_SHARD_BASENAMES,
    PINNED_LICENSE_IDENTIFIER,
    PINNED_MODEL_REPOSITORY,
    PINNED_MODEL_REVISION,
    REQUIRED_FREE_SPACE_BYTES,
    ColibriStage2Failure,
    is_hex64,
    is_safe_basename,
    is_simple_version,
)

REQUIRED_SOURCE_FILES = ("config.json", *EXPECTED_SHARD_BASENAMES)

# Reviewed, officially published SHA-256 values for each required source
# file, keyed by basename. Empty in this implementation commit: no download
# is authorized until a separately reviewed commit populates this with
# full, non-truncated, real upstream hashes for every entry in
# ``REQUIRED_SOURCE_FILES``.
REVIEWED_SOURCE_SHARD_SHA256: Mapping[str, str] = MappingProxyType({})

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


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
            "download one exact source file from the immutable revision",
            "verify its basename, exact size, and SHA-256",
            f"run the unmodified pinned {EXPECTED_CONVERTER_SCRIPT_BASENAME} with --model",
            "verify the expected converted output shard exists as a regular file",
            "hash and record the converted shard",
            "delete the corresponding source shard only after the above succeeds",
            "verify the source shard deletion",
            "repeat for the next source file",
        ),
        deviation_statement=DEVIATION_STATEMENT,
        approval_statement=APPROVAL_STATEMENT,
    )


def require_reviewed_source_manifest() -> Mapping[str, str]:
    """Fail closed unless every required source file has a full reviewed hash."""

    if set(REVIEWED_SOURCE_SHARD_SHA256) != set(REQUIRED_SOURCE_FILES):
        raise ColibriStage2Failure("source_model_manifest_unreviewed")
    if not all(is_hex64(value) for value in REVIEWED_SOURCE_SHARD_SHA256.values()):
        raise ColibriStage2Failure("source_model_manifest_unreviewed")
    return REVIEWED_SOURCE_SHARD_SHA256


def check_approved_preconditions(
    *,
    interactive_check: Callable[[], bool],
    approved: bool,
    destination: Path,
    free_bytes_probe: Callable[[Path], int],
    isolated_python_env_ready: bool,
    dependencies_installed: bool,
) -> Mapping[str, str]:
    """The full approved-mode gate, checked before any network activity.

    Returns the reviewed source manifest on success. Every check here runs
    before a single byte is downloaded.
    """

    reviewed = require_reviewed_source_manifest()
    if not approved or not interactive_check():
        raise ColibriStage2Failure("noninteractive_approval_rejected")
    if destination.exists() and any(destination.iterdir()):
        raise ColibriStage2Failure("destination_not_empty")
    if free_bytes_probe(destination) < REQUIRED_FREE_SPACE_BYTES:
        raise ColibriStage2Failure("insufficient_disk_space")
    if not isolated_python_env_ready:
        raise ColibriStage2Failure("python_environment_unavailable")
    if not dependencies_installed:
        raise ColibriStage2Failure("dependency_unavailable")
    return reviewed


class Downloader(Protocol):
    def download(self, basename: str, destination: Path) -> None: ...


class Converter(Protocol):
    def convert(self, model_dir: Path) -> None: ...


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


def _assert_regular_no_reparse(path: Path) -> bool:
    import os

    try:
        info = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return stat_module.S_ISREG(info.st_mode)


def run_shard_transaction(
    *,
    source_basename: str,
    expected_source_sha256: str,
    destination_dir: Path,
    converted_dir: Path,
    converted_basename: str,
    downloader: Downloader,
    converter: Converter,
    clock: Callable[[], float] = time.monotonic,
) -> ShardTransactionResult:
    """One fully transactional shard: download -> verify -> convert ->
    verify converted output -> delete source -> verify deletion.

    A failure at any step before the converted output is hashed leaves the
    source shard untouched. An existing converted shard is never
    overwritten.
    """

    if not is_hex64(expected_source_sha256):
        raise ColibriStage2Failure("source_model_manifest_unreviewed")
    started = clock()
    source_path = destination_dir / source_basename
    converted_path = converted_dir / converted_basename

    if converted_path.exists():
        raise ColibriStage2Failure("converted_shard_already_exists")

    try:
        downloader.download(source_basename, source_path)
    except OSError as exc:
        raise ColibriStage2Failure("shard_download_failed") from exc
    if not _assert_regular_no_reparse(source_path):
        raise ColibriStage2Failure("shard_download_failed")
    if source_path.name != source_basename or _sha256_file(source_path) != expected_source_sha256:
        raise ColibriStage2Failure("shard_verification_failed")

    try:
        converter.convert(destination_dir)
    except OSError as exc:
        raise ColibriStage2Failure("conversion_failed") from exc
    if not _assert_regular_no_reparse(converted_path):
        raise ColibriStage2Failure("converted_shard_missing")

    converted_sha256 = _sha256_file(converted_path)
    converted_size = converted_path.stat().st_size

    # Only after the converted output is verified present and hashed may
    # the source shard be deleted.
    try:
        source_path.unlink()
    except OSError as exc:
        raise ColibriStage2Failure("source_shard_deletion_failed") from exc
    if source_path.exists():
        raise ColibriStage2Failure("source_shard_deletion_unverified")

    elapsed_ms = round((clock() - started) * 1000)
    return ShardTransactionResult(
        source_basename=source_basename,
        converted_basename=converted_basename,
        source_deleted=True,
        converted_sha256=converted_sha256,
        converted_size_bytes=converted_size,
        elapsed_ms=max(0, elapsed_ms),
    )


def build_conversion_capture(
    *,
    shard_results: Sequence[ShardTransactionResult],
    dependency_versions: Mapping[str, str],
    total_elapsed_ms: int,
    cleanup_complete: bool,
) -> dict[str, Any]:
    """A closed, privacy-safe capture: basenames/hashes/sizes/times only.

    Never contains a path, username, environment value, or raw tool
    output, and never validates as an ``OlmoeModelManifest`` — its state is
    always ``unreviewed_conversion_capture`` and it can never itself
    authorize a real run.
    """

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
        "shards": shards,
        "dependency_versions": dict(dependency_versions),
        "total_elapsed_ms": max(0, int(total_elapsed_ms)),
        "cleanup_complete": bool(cleanup_complete),
    }


def _default_free_bytes_probe(path: Path) -> int:
    import shutil

    probe_path = path if path.exists() else path.parent
    return shutil.disk_usage(probe_path).free


def _default_interactive_check() -> bool:
    import sys

    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """Developer-only CLI. Defaults to dry-run; ``--approve`` requests the
    approved-execution gate, which fails closed in this implementation
    commit because ``REVIEWED_SOURCE_SHARD_SHA256`` is empty."""

    import argparse
    import json

    parser = argparse.ArgumentParser(description="Colibrì Stage 2A OLMoE download/conversion plan")
    parser.add_argument("--destination", required=True, help="Target directory for the source download")
    parser.add_argument("--approve", action="store_true", help="Request approved execution (currently always fails closed)")
    args = parser.parse_args(argv)
    destination = Path(args.destination)

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
                destination=destination,
                free_bytes_probe=_default_free_bytes_probe,
                isolated_python_env_ready=False,
                dependencies_installed=False,
            )
        except ColibriStage2Failure as exc:
            output["mode"] = "approved_rejected"
            output["rejection_category"] = exc.category

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
