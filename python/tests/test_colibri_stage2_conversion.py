"""Tests for the Colibrì Stage 2A download/conversion plan and capture
(Part 3)."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from types import MappingProxyType

import pytest

from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services import colibri_stage2_conversion as conv
from odysseus_desktop_backend.services import colibri_stage2_manifest as manifest_mod

HASH_A = "a" * 64
HASH_B = "b" * 64


class _NeverCallDownloader:
    def download(self, basename: str, destination: Path) -> None:
        raise AssertionError("downloader must never be called")


class _NeverCallConverter:
    def convert(self, model_dir: Path) -> None:
        raise AssertionError("converter must never be called")


# ---------------------------------------------------------------------------
# Dry-run: process-free, network-free
# ---------------------------------------------------------------------------


def test_dry_run_plan_discloses_every_required_field(tmp_path: Path) -> None:
    plan = conv.build_dry_run_plan(tmp_path / "dest")
    assert plan.model_repository == common.PINNED_MODEL_REPOSITORY
    assert plan.model_revision == common.PINNED_MODEL_REVISION
    assert plan.license_identifier == "Apache-2.0"
    assert plan.required_source_files == conv.REQUIRED_SOURCE_FILES
    assert len(plan.required_source_files) == 4
    assert plan.approx_download_bytes == 13_840_000_000
    assert plan.required_free_space_bytes == 18 * 1024 * 1024 * 1024
    assert "0125-Instruct" in plan.deviation_statement
    assert "0924" in plan.deviation_statement
    assert "approval" in plan.approval_statement.lower()


def test_dry_run_is_process_free_and_network_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry run must not touch the network or spawn a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(socket, "socket", _boom)
    conv.build_dry_run_plan(tmp_path / "does-not-exist")


def test_cli_dry_run_does_not_touch_destination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    destination = tmp_path / "never-created"
    exit_code = conv.main(["--destination", str(destination)])
    assert exit_code == 0
    assert not destination.exists()
    out = capsys.readouterr().out
    assert '"mode": "dry_run"' in out


# ---------------------------------------------------------------------------
# Source manifest review gate
# ---------------------------------------------------------------------------


def test_source_manifest_unreviewed_in_this_commit() -> None:
    assert dict(conv.REVIEWED_SOURCE_SHARD_SHA256) == {}
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.require_reviewed_source_manifest()


def test_source_manifest_partial_coverage_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = MappingProxyType({conv.REQUIRED_SOURCE_FILES[0]: HASH_A})
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_SHA256", partial)
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.require_reviewed_source_manifest()


def test_source_manifest_truncated_hash_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    full = MappingProxyType({name: "ab12" for name in conv.REQUIRED_SOURCE_FILES})
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_SHA256", full)
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.require_reviewed_source_manifest()


def _reviewed_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    full = MappingProxyType({name: HASH_A for name in conv.REQUIRED_SOURCE_FILES})
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_SHA256", full)


# ---------------------------------------------------------------------------
# Approved-mode preconditions
# ---------------------------------------------------------------------------


def test_approved_preconditions_check_source_manifest_before_anything_else(
    tmp_path: Path,
) -> None:
    # Even with every other precondition satisfiable, an unreviewed source
    # manifest must be the reported reason -- and no network/disk probe may
    # be needed to reach that conclusion.
    def _boom_probe(path: Path) -> int:
        raise AssertionError("disk probe must not run before the manifest gate")

    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.check_approved_preconditions(
            interactive_check=lambda: False,
            approved=False,
            destination=tmp_path / "d",
            free_bytes_probe=_boom_probe,
            isolated_python_env_ready=False,
            dependencies_installed=False,
        )


def test_approved_preconditions_reject_noninteractive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reviewed_manifest(monkeypatch)
    with pytest.raises(conv.ColibriStage2Failure, match="noninteractive_approval_rejected"):
        conv.check_approved_preconditions(
            interactive_check=lambda: False,
            approved=True,
            destination=tmp_path / "d",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependencies_installed=True,
        )


def test_approved_preconditions_reject_missing_explicit_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    with pytest.raises(conv.ColibriStage2Failure, match="noninteractive_approval_rejected"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=False,
            destination=tmp_path / "d",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependencies_installed=True,
        )


def test_approved_preconditions_reject_nonempty_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "leftover.bin").write_bytes(b"x")
    with pytest.raises(conv.ColibriStage2Failure, match="destination_not_empty"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=True,
            destination=destination,
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependencies_installed=True,
        )


def test_approved_preconditions_check_disk_space_before_python_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    destination = tmp_path / "dest"
    probe_calls = []

    def probe(path: Path) -> int:
        probe_calls.append(path)
        return 1024  # far below the 18 GiB floor

    with pytest.raises(conv.ColibriStage2Failure, match="insufficient_disk_space"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=True,
            destination=destination,
            free_bytes_probe=probe,
            isolated_python_env_ready=False,
            dependencies_installed=False,
        )
    assert probe_calls == [destination]


def test_approved_preconditions_reject_missing_python_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    with pytest.raises(conv.ColibriStage2Failure, match="python_environment_unavailable"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=True,
            destination=tmp_path / "dest",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=False,
            dependencies_installed=True,
        )


def test_approved_preconditions_reject_missing_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    with pytest.raises(conv.ColibriStage2Failure, match="dependency_unavailable"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=True,
            destination=tmp_path / "dest",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependencies_installed=False,
        )


def test_approved_preconditions_pass_when_everything_is_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    reviewed = conv.check_approved_preconditions(
        interactive_check=lambda: True,
        approved=True,
        destination=tmp_path / "dest",
        free_bytes_probe=lambda path: 2**40,
        isolated_python_env_ready=True,
        dependencies_installed=True,
    )
    assert dict(reviewed) == {name: HASH_A for name in conv.REQUIRED_SOURCE_FILES}


def test_cli_approve_never_touches_network_and_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "dest"
    exit_code = conv.main(["--destination", str(destination), "--approve"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"mode": "approved_rejected"' in out
    assert '"rejection_category": "source_model_manifest_unreviewed"' in out
    assert not destination.exists()


# ---------------------------------------------------------------------------
# Per-shard transactional download/verify/convert/delete
# ---------------------------------------------------------------------------


class _RecordingDownloader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Path]] = []

    def download(self, basename: str, destination: Path) -> None:
        self.calls.append((basename, destination))
        destination.write_bytes(self.payload)


class _RecordingConverter:
    """A fake standing in for the unmodified pinned convert_olmoe.py: it
    knows its own converted-output directory, independent of the source
    ``model_dir`` it is invoked with (mirroring how a real converter run
    can target an output directory distinct from the source shard dir)."""

    def __init__(self, converted_dir: Path, converted_basename: str, payload: bytes) -> None:
        self.converted_dir = converted_dir
        self.converted_basename = converted_basename
        self.payload = payload
        self.calls: list[Path] = []

    def convert(self, model_dir: Path) -> None:
        self.calls.append(model_dir)
        (self.converted_dir / self.converted_basename).write_bytes(self.payload)


def _shard_setup(tmp_path: Path) -> tuple[Path, Path, str, bytes]:
    source_dir = tmp_path / "source"
    converted_dir = tmp_path / "converted"
    source_dir.mkdir()
    converted_dir.mkdir()
    payload = b"source shard bytes"
    return source_dir, converted_dir, "shard.safetensors", payload


def test_shard_transaction_happy_path_deletes_source_after_conversion(tmp_path: Path) -> None:
    source_dir, converted_dir, basename, payload = _shard_setup(tmp_path)
    import hashlib

    expected_sha = hashlib.sha256(payload).hexdigest()
    downloader = _RecordingDownloader(payload)
    converted_payload = b"converted bytes"
    converter = _RecordingConverter(converted_dir, "converted.bin", converted_payload)

    result = conv.run_shard_transaction(
        source_basename=basename,
        expected_source_sha256=expected_sha,
        destination_dir=source_dir,
        converted_dir=converted_dir,
        converted_basename="converted.bin",
        downloader=downloader,
        converter=converter,
    )

    assert result.source_deleted is True
    assert not (source_dir / basename).exists()
    assert result.converted_sha256 == hashlib.sha256(converted_payload).hexdigest()
    assert result.converted_size_bytes == len(converted_payload)
    assert (converted_dir / "converted.bin").exists()


def test_shard_transaction_requires_full_reviewed_hash(tmp_path: Path) -> None:
    source_dir, converted_dir, basename, payload = _shard_setup(tmp_path)
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.run_shard_transaction(
            source_basename=basename,
            expected_source_sha256="short",
            destination_dir=source_dir,
            converted_dir=converted_dir,
            converted_basename="converted.bin",
            downloader=_NeverCallDownloader(),
            converter=_NeverCallConverter(),
        )


def test_shard_transaction_never_overwrites_existing_converted_shard(tmp_path: Path) -> None:
    source_dir, converted_dir, basename, payload = _shard_setup(tmp_path)
    (converted_dir / "converted.bin").write_bytes(b"already there")
    with pytest.raises(conv.ColibriStage2Failure, match="converted_shard_already_exists"):
        conv.run_shard_transaction(
            source_basename=basename,
            expected_source_sha256=HASH_A,
            destination_dir=source_dir,
            converted_dir=converted_dir,
            converted_basename="converted.bin",
            downloader=_NeverCallDownloader(),
            converter=_NeverCallConverter(),
        )


def test_shard_transaction_rejects_source_hash_mismatch(tmp_path: Path) -> None:
    source_dir, converted_dir, basename, payload = _shard_setup(tmp_path)
    downloader = _RecordingDownloader(payload)
    with pytest.raises(conv.ColibriStage2Failure, match="shard_verification_failed"):
        conv.run_shard_transaction(
            source_basename=basename,
            expected_source_sha256=HASH_A,  # does not match payload
            destination_dir=source_dir,
            converted_dir=converted_dir,
            converted_basename="converted.bin",
            downloader=downloader,
            converter=_NeverCallConverter(),
        )
    assert (source_dir / basename).exists()  # never got to conversion; source intact


def test_shard_transaction_retains_source_when_conversion_fails(tmp_path: Path) -> None:
    source_dir, converted_dir, basename, payload = _shard_setup(tmp_path)
    import hashlib

    expected_sha = hashlib.sha256(payload).hexdigest()
    downloader = _RecordingDownloader(payload)

    class _FailingConverter:
        def convert(self, model_dir: Path) -> None:
            raise OSError("conversion crashed")

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.run_shard_transaction(
            source_basename=basename,
            expected_source_sha256=expected_sha,
            destination_dir=source_dir,
            converted_dir=converted_dir,
            converted_basename="converted.bin",
            downloader=downloader,
            converter=_FailingConverter(),
        )
    assert (source_dir / basename).exists()
    assert not (converted_dir / "converted.bin").exists()


def test_shard_transaction_retains_source_when_converted_output_missing(tmp_path: Path) -> None:
    source_dir, converted_dir, basename, payload = _shard_setup(tmp_path)
    import hashlib

    expected_sha = hashlib.sha256(payload).hexdigest()
    downloader = _RecordingDownloader(payload)

    class _SilentConverter:
        def convert(self, model_dir: Path) -> None:
            pass  # never produces the expected converted file

    with pytest.raises(conv.ColibriStage2Failure, match="converted_shard_missing"):
        conv.run_shard_transaction(
            source_basename=basename,
            expected_source_sha256=expected_sha,
            destination_dir=source_dir,
            converted_dir=converted_dir,
            converted_basename="converted.bin",
            downloader=downloader,
            converter=_SilentConverter(),
        )
    assert (source_dir / basename).exists()


def test_shard_transaction_deletion_only_after_hashing_converted_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, converted_dir, basename, payload = _shard_setup(tmp_path)
    import hashlib

    expected_sha = hashlib.sha256(payload).hexdigest()
    downloader = _RecordingDownloader(payload)
    converted_payload = b"converted bytes"
    converter = _RecordingConverter(converted_dir, "converted.bin", converted_payload)

    real_unlink = Path.unlink

    def _boom_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == basename:
            raise OSError("disk error deleting source")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _boom_unlink)

    with pytest.raises(conv.ColibriStage2Failure, match="source_shard_deletion_failed"):
        conv.run_shard_transaction(
            source_basename=basename,
            expected_source_sha256=expected_sha,
            destination_dir=source_dir,
            converted_dir=converted_dir,
            converted_basename="converted.bin",
            downloader=downloader,
            converter=converter,
        )
    # The converted output was already produced and hashed before the
    # (failed) deletion attempt -- proving the ordering.
    assert (converted_dir / "converted.bin").read_bytes() == converted_payload
    assert (source_dir / basename).exists()


# ---------------------------------------------------------------------------
# Privacy-safe conversion capture
# ---------------------------------------------------------------------------


def test_conversion_capture_is_closed_and_privacy_safe(tmp_path: Path) -> None:
    result = conv.ShardTransactionResult(
        source_basename="model-00001-of-00003.safetensors",
        converted_basename="expert-0.bin",
        source_deleted=True,
        converted_sha256=HASH_A,
        converted_size_bytes=123,
        elapsed_ms=456,
    )
    capture = conv.build_conversion_capture(
        shard_results=[result],
        dependency_versions={"python": "3.11.9", "torch": "2.3.0"},
        total_elapsed_ms=999,
        cleanup_complete=True,
    )
    assert capture["state"] == "unreviewed_conversion_capture"
    assert capture["schema_version"] == common.CONVERSION_CAPTURE_SCHEMA_VERSION
    assert capture["shards"] == [
        {
            "source_basename": "model-00001-of-00003.safetensors",
            "converted_basename": "expert-0.bin",
            "source_deleted": True,
            "converted_sha256": HASH_A,
            "converted_size_bytes": 123,
            "elapsed_ms": 456,
        }
    ]
    serialized = repr(capture)
    assert str(tmp_path) not in serialized
    assert "C:\\" not in serialized
    assert "/home" not in serialized


def test_conversion_capture_rejects_unknown_dependency_names() -> None:
    with pytest.raises(ValueError):
        conv.build_conversion_capture(
            shard_results=[],
            dependency_versions={"rust": "1.0.0"},
            total_elapsed_ms=0,
            cleanup_complete=True,
        )


def test_conversion_capture_never_validates_as_a_reviewed_manifest() -> None:
    capture = conv.build_conversion_capture(
        shard_results=[],
        dependency_versions={},
        total_elapsed_ms=0,
        cleanup_complete=False,
    )
    with pytest.raises(TypeError):
        manifest_mod.OlmoeModelManifest(**capture)  # type: ignore[arg-type]
    assert "model_repository" not in capture
    assert "colibri_commit" not in capture
