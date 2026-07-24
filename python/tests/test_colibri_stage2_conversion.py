"""Tests for the Colibrì Stage 2A download/conversion orchestration and
capture (Part 3 / Blockers 2-4).

No test in this file launches a real process, opens a real network
connection, or executes a real converter/downloader -- every ``Downloader``
and ``Converter`` is a synthetic fake, and the default real adapters
(``PinnedRevisionFileDownloader`` / ``PinnedScriptConverter``) are never
exercised for real here.
"""

from __future__ import annotations

import hashlib
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
CONFIG_BASENAME = common.EXPECTED_CONFIG_BASENAME
SHARD_BASENAMES = common.EXPECTED_SHARD_BASENAMES


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(basename: str, data: bytes) -> conv.SourceShardEntry:
    return conv.SourceShardEntry(basename=basename, size_bytes=len(data), sha256=_sha256(data))


class _NeverCallDownloader:
    def download(self, *, basename: str, expected_size_bytes: int, expected_sha256: str, destination: Path) -> None:
        raise AssertionError("downloader must never be called")


class _NeverCallConverter:
    def convert(self, *, model_dir: Path, output_dir: Path) -> None:
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
    converted = tmp_path / "converted-never-created"
    exit_code = conv.main(["--destination", str(destination), "--converted-destination", str(converted)])
    assert exit_code == 0
    assert not destination.exists()
    assert not converted.exists()
    out = capsys.readouterr().out
    assert '"mode": "dry_run"' in out


# ---------------------------------------------------------------------------
# Blocker 2: closed reviewed source manifest
# ---------------------------------------------------------------------------


def test_source_shard_entry_rejects_names_outside_required_files() -> None:
    with pytest.raises(ValueError):
        conv.SourceShardEntry(basename="not-a-required-file.bin", size_bytes=10, sha256=HASH_A)


def test_source_shard_entry_rejects_traversal_and_unsafe_basenames() -> None:
    for basename in ("../config.json", "config.json/../evil", "C:\\config.json", "..", "."):
        with pytest.raises(ValueError):
            conv.SourceShardEntry(basename=basename, size_bytes=10, sha256=HASH_A)


def test_source_shard_entry_rejects_nonpositive_or_oversized_size() -> None:
    with pytest.raises(ValueError):
        conv.SourceShardEntry(basename=CONFIG_BASENAME, size_bytes=0, sha256=HASH_A)
    with pytest.raises(ValueError):
        conv.SourceShardEntry(basename=CONFIG_BASENAME, size_bytes=-1, sha256=HASH_A)
    with pytest.raises(ValueError):
        conv.SourceShardEntry(basename=CONFIG_BASENAME, size_bytes=2 * 1024 * 1024, sha256=HASH_A)


def test_source_shard_entry_rejects_malformed_sha256() -> None:
    with pytest.raises(ValueError):
        conv.SourceShardEntry(basename=CONFIG_BASENAME, size_bytes=10, sha256="short")
    with pytest.raises(ValueError):
        conv.SourceShardEntry(basename=CONFIG_BASENAME, size_bytes=10, sha256="Z" * 64)


def test_source_manifest_unreviewed_in_this_commit() -> None:
    assert dict(conv.REVIEWED_SOURCE_SHARD_MANIFEST) == {}
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.require_reviewed_source_manifest()


def test_source_manifest_partial_coverage_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = MappingProxyType({CONFIG_BASENAME: _entry(CONFIG_BASENAME, b"{}")})
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", partial)
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.require_reviewed_source_manifest()


def test_source_manifest_wrong_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    full = MappingProxyType(
        {
            CONFIG_BASENAME: _entry(CONFIG_BASENAME, b"{}"),
            SHARD_BASENAMES[0]: _entry(SHARD_BASENAMES[0], b"shard-0"),
            SHARD_BASENAMES[1]: _entry(SHARD_BASENAMES[1], b"shard-1"),
            # Registered under a key that does not match the entry's own basename.
            "not-the-real-name": _entry(SHARD_BASENAMES[2], b"shard-2"),
        }
    )
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", full)
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.require_reviewed_source_manifest()


def _reviewed_manifest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shard_bytes: tuple[bytes, bytes, bytes] | None = None,
    config_bytes: bytes = b'{"fake": true}',
):
    shard_bytes = shard_bytes or (b"shard-0", b"shard-1", b"shard-2")
    full = MappingProxyType(
        {
            CONFIG_BASENAME: _entry(CONFIG_BASENAME, config_bytes),
            SHARD_BASENAMES[0]: _entry(SHARD_BASENAMES[0], shard_bytes[0]),
            SHARD_BASENAMES[1]: _entry(SHARD_BASENAMES[1], shard_bytes[1]),
            SHARD_BASENAMES[2]: _entry(SHARD_BASENAMES[2], shard_bytes[2]),
        }
    )
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", full)
    return full


def test_reviewed_manifest_succeeds_when_all_four_entries_are_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    full = _reviewed_manifest(monkeypatch)
    reviewed = conv.require_reviewed_source_manifest()
    assert dict(reviewed) == dict(full)


# ---------------------------------------------------------------------------
# Approved-mode preconditions
# ---------------------------------------------------------------------------


def test_approved_preconditions_check_source_manifest_before_anything_else(
    tmp_path: Path,
) -> None:
    def _boom_probe(path: Path) -> int:
        raise AssertionError("disk probe must not run before the manifest gate")

    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.check_approved_preconditions(
            interactive_check=lambda: False,
            approved=False,
            destination_dir=tmp_path / "d",
            converted_dir=tmp_path / "c",
            free_bytes_probe=_boom_probe,
            isolated_python_env_ready=False,
            dependency_versions={},
        )


def test_approved_preconditions_reject_noninteractive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reviewed_manifest(monkeypatch)
    with pytest.raises(conv.ColibriStage2Failure, match="noninteractive_approval_rejected"):
        conv.check_approved_preconditions(
            interactive_check=lambda: False,
            approved=True,
            destination_dir=tmp_path / "d",
            converted_dir=tmp_path / "c",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
        )


def test_approved_preconditions_reject_missing_explicit_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    with pytest.raises(conv.ColibriStage2Failure, match="noninteractive_approval_rejected"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=False,
            destination_dir=tmp_path / "d",
            converted_dir=tmp_path / "c",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
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
            destination_dir=destination,
            converted_dir=tmp_path / "c",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
        )


def test_approved_preconditions_reject_nonempty_converted_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    converted = tmp_path / "converted"
    converted.mkdir()
    (converted / "leftover.bin").write_bytes(b"x")
    with pytest.raises(conv.ColibriStage2Failure, match="destination_not_empty"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=True,
            destination_dir=tmp_path / "d",
            converted_dir=converted,
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
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
            destination_dir=destination,
            converted_dir=tmp_path / "c",
            free_bytes_probe=probe,
            isolated_python_env_ready=False,
            dependency_versions={},
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
            destination_dir=tmp_path / "dest",
            converted_dir=tmp_path / "c",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=False,
            dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
        )


def test_approved_preconditions_reject_missing_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reviewed_manifest(monkeypatch)
    with pytest.raises(conv.ColibriStage2Failure, match="dependency_unavailable"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=True,
            destination_dir=tmp_path / "dest",
            converted_dir=tmp_path / "c",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.3.0"},  # missing safetensors
        )


def test_approved_preconditions_pass_when_everything_is_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = _reviewed_manifest(monkeypatch)
    reviewed = conv.check_approved_preconditions(
        interactive_check=lambda: True,
        approved=True,
        destination_dir=tmp_path / "dest",
        converted_dir=tmp_path / "c",
        free_bytes_probe=lambda path: 2**40,
        isolated_python_env_ready=True,
        dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
    )
    assert dict(reviewed) == dict(full)


def test_cli_approve_never_touches_network_and_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "dest"
    converted = tmp_path / "converted"
    exit_code = conv.main(
        ["--destination", str(destination), "--converted-destination", str(converted), "--approve"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"mode": "approved_rejected"' in out
    assert '"rejection_category": "source_model_manifest_unreviewed"' in out
    assert not destination.exists()
    assert not converted.exists()


def test_default_isolated_python_env_ready_is_not_hardcoded_false() -> None:
    # The CLI must no longer permanently supply False -- the real detector
    # is at least callable and returns an actual bool derived from
    # sys.prefix/sys.base_prefix, not a constant.
    result = conv._default_isolated_python_env_ready()
    assert isinstance(result, bool)


def test_default_dependency_versions_is_not_hardcoded_and_always_captures_python() -> None:
    versions = conv._default_dependency_versions()
    assert isinstance(versions, dict)
    assert "python" in versions
    assert common.is_simple_version(versions["python"])


# ---------------------------------------------------------------------------
# Path safety (Blocker 3)
# ---------------------------------------------------------------------------


def _shard_setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    destination_dir = tmp_path / "source"
    final_converted_dir = tmp_path / "converted"
    temp_output_parent = tmp_path / "scratch"
    destination_dir.mkdir()
    final_converted_dir.mkdir()
    temp_output_parent.mkdir()
    config_path = destination_dir / CONFIG_BASENAME
    config_path.write_bytes(b'{"fake": true}')
    return destination_dir, config_path, final_converted_dir, temp_output_parent


class _RecordingDownloader:
    def __init__(self, payload_by_basename: dict[str, bytes]) -> None:
        self.payload_by_basename = payload_by_basename
        self.calls: list[str] = []

    def download(self, *, basename: str, expected_size_bytes: int, expected_sha256: str, destination: Path) -> None:
        self.calls.append(basename)
        destination.write_bytes(self.payload_by_basename[basename])


class _RecordingConverter:
    """A fake standing in for the unmodified pinned convert_olmoe.py: given
    a model_dir containing config.json plus exactly one source shard, it
    writes config.json (copied unchanged) plus the converted shard into
    output_dir."""

    def __init__(self, converted_shard_payload: bytes) -> None:
        self.converted_shard_payload = converted_shard_payload
        self.calls: list[tuple[Path, Path]] = []

    def convert(self, *, model_dir: Path, output_dir: Path) -> None:
        self.calls.append((model_dir, output_dir))
        config_path = model_dir / CONFIG_BASENAME
        (output_dir / CONFIG_BASENAME).write_bytes(config_path.read_bytes())
        shard_names = [
            entry.name
            for entry in model_dir.iterdir()
            if entry.name != CONFIG_BASENAME
        ]
        assert len(shard_names) == 1, "exactly one source shard must be visible to the converter"
        (output_dir / shard_names[0]).write_bytes(self.converted_shard_payload)


def test_shard_transaction_happy_path_deletes_source_after_conversion(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})
    converted_payload = b"converted bytes"
    converter = _RecordingConverter(converted_payload)

    result = conv.run_shard_transaction(
        expected_source=entry,
        destination_dir=destination_dir,
        config_path=config_path,
        final_converted_dir=final_converted_dir,
        temp_output_parent=temp_output_parent,
        downloader=downloader,
        converter=converter,
    )

    assert result.source_deleted is True
    assert result.converted_basename == SHARD_BASENAMES[0]
    assert not (destination_dir / SHARD_BASENAMES[0]).exists()
    assert result.converted_sha256 == _sha256(converted_payload)
    assert result.converted_size_bytes == len(converted_payload)
    assert (final_converted_dir / SHARD_BASENAMES[0]).read_bytes() == converted_payload
    # The per-shard temp output directory is removed afterwards.
    assert list(temp_output_parent.iterdir()) == []
    # The config in destination_dir is untouched -- it is moved only once,
    # by the orchestrator, after every shard succeeds.
    assert config_path.exists()


def test_shard_transaction_rejects_config_as_the_source(tmp_path: Path) -> None:
    # config.json is a valid SourceShardEntry on its own (it is one of the
    # four required source files), but run_shard_transaction is only ever
    # for the three safetensors shards -- config flows through
    # download_and_verify_config and the orchestrator's final move instead.
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    entry = _entry(CONFIG_BASENAME, b'{"fake": true}')
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_basename_rejected"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=_NeverCallDownloader(),
            converter=_NeverCallConverter(),
        )


def test_shard_transaction_never_overwrites_existing_converted_shard(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    (final_converted_dir / SHARD_BASENAMES[0]).write_bytes(b"already there")

    with pytest.raises(conv.ColibriStage2Failure, match="converted_shard_already_exists"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=_NeverCallDownloader(),
            converter=_NeverCallConverter(),
        )


def test_shard_transaction_rejects_source_hash_mismatch(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = conv.SourceShardEntry(basename=SHARD_BASENAMES[0], size_bytes=len(shard_payload), sha256=HASH_A)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})

    with pytest.raises(conv.ColibriStage2Failure, match="shard_verification_failed"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=downloader,
            converter=_NeverCallConverter(),
        )
    assert (destination_dir / SHARD_BASENAMES[0]).exists()  # never got to conversion; source intact


def test_shard_transaction_retains_source_when_conversion_fails(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})

    class _FailingConverter:
        def convert(self, *, model_dir: Path, output_dir: Path) -> None:
            raise OSError("conversion crashed")

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=downloader,
            converter=_FailingConverter(),
        )
    assert (destination_dir / SHARD_BASENAMES[0]).exists()
    assert not (final_converted_dir / SHARD_BASENAMES[0]).exists()
    assert list(temp_output_parent.iterdir()) == []  # temp output still cleaned up


def test_shard_transaction_retains_source_when_converted_output_missing(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})

    class _SilentConverter:
        def convert(self, *, model_dir: Path, output_dir: Path) -> None:
            pass  # never produces any expected converted output

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_output_unexpected"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=downloader,
            converter=_SilentConverter(),
        )
    assert (destination_dir / SHARD_BASENAMES[0]).exists()


def test_shard_transaction_rejects_unexpected_extra_file_in_temp_output(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})

    class _LeakyConverter:
        def convert(self, *, model_dir: Path, output_dir: Path) -> None:
            (output_dir / CONFIG_BASENAME).write_bytes((model_dir / CONFIG_BASENAME).read_bytes())
            (output_dir / SHARD_BASENAMES[0]).write_bytes(b"converted")
            (output_dir / "unexpected-extra-file.bin").write_bytes(b"leaked")

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_output_unexpected"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=downloader,
            converter=_LeakyConverter(),
        )
    assert (destination_dir / SHARD_BASENAMES[0]).exists()


def test_shard_transaction_rejects_mutated_config_in_temp_output(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})

    class _ConfigMutatingConverter:
        def convert(self, *, model_dir: Path, output_dir: Path) -> None:
            (output_dir / CONFIG_BASENAME).write_bytes(b"mutated config")
            (output_dir / SHARD_BASENAMES[0]).write_bytes(b"converted")

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_output_unexpected"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=downloader,
            converter=_ConfigMutatingConverter(),
        )
    assert (destination_dir / SHARD_BASENAMES[0]).exists()


def test_shard_transaction_deletion_only_after_move_into_final_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})
    converted_payload = b"converted bytes"
    converter = _RecordingConverter(converted_payload)

    real_unlink = Path.unlink

    def _boom_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == SHARD_BASENAMES[0] and self.parent == destination_dir:
            raise OSError("disk error deleting source")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _boom_unlink)

    with pytest.raises(conv.ColibriStage2Failure, match="source_shard_deletion_failed"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=downloader,
            converter=converter,
        )
    # The converted output was already moved into place before the
    # (failed) source deletion attempt -- proving the ordering.
    assert (final_converted_dir / SHARD_BASENAMES[0]).read_bytes() == converted_payload
    assert (destination_dir / SHARD_BASENAMES[0]).exists()


def test_shard_transaction_rejects_traversal_style_final_directory_escape(tmp_path: Path) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)

    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=tmp_path / "does-not-exist",
            temp_output_parent=temp_output_parent,
            downloader=_NeverCallDownloader(),
            converter=_NeverCallConverter(),
        )


def test_shard_transaction_rejects_reparse_point_destination_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)

    real_lstat = conv.os.lstat

    class _FakeStatResult:
        def __init__(self, real_result: object) -> None:
            self.st_mode = real_result.st_mode  # type: ignore[attr-defined]
            self.st_file_attributes = 0x400

    def _fake_lstat(path: object, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == destination_dir:  # type: ignore[arg-type]
            return _FakeStatResult(result)
        return result

    monkeypatch.setattr(conv.os, "lstat", _fake_lstat)
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=destination_dir,
            config_path=config_path,
            final_converted_dir=final_converted_dir,
            temp_output_parent=temp_output_parent,
            downloader=_NeverCallDownloader(),
            converter=_NeverCallConverter(),
        )


def test_download_and_verify_config_rejects_traversal_basename(tmp_path: Path) -> None:
    destination_dir = tmp_path / "source"
    destination_dir.mkdir()
    with pytest.raises(ValueError):
        conv.SourceShardEntry(basename="../config.json", size_bytes=10, sha256=HASH_A)


def test_download_and_verify_config_happy_path(tmp_path: Path) -> None:
    destination_dir = tmp_path / "source"
    destination_dir.mkdir()
    payload = b'{"real": true}'
    entry = _entry(CONFIG_BASENAME, payload)
    downloader = _RecordingDownloader({CONFIG_BASENAME: payload})

    config_path = conv.download_and_verify_config(
        expected_config=entry, destination_dir=destination_dir, downloader=downloader
    )
    assert config_path.read_bytes() == payload
    assert downloader.calls == [CONFIG_BASENAME]


def test_download_and_verify_config_rejects_size_or_hash_mismatch(tmp_path: Path) -> None:
    destination_dir = tmp_path / "source"
    destination_dir.mkdir()
    payload = b'{"real": true}'
    entry = conv.SourceShardEntry(basename=CONFIG_BASENAME, size_bytes=len(payload), sha256=HASH_A)
    downloader = _RecordingDownloader({CONFIG_BASENAME: payload})

    with pytest.raises(conv.ColibriStage2Failure, match="shard_verification_failed"):
        conv.download_and_verify_config(
            expected_config=entry, destination_dir=destination_dir, downloader=downloader
        )


# ---------------------------------------------------------------------------
# Full orchestrated sequence (Blocker 4)
# ---------------------------------------------------------------------------


class _FullRunDownloader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def download(self, *, basename: str, expected_size_bytes: int, expected_sha256: str, destination: Path) -> None:
        self.calls.append(basename)
        destination.write_bytes(self.payloads[basename])


class _FullRunConverter:
    def __init__(self, converted_payloads: dict[str, bytes]) -> None:
        self.converted_payloads = converted_payloads
        self.calls: list[Path] = []

    def convert(self, *, model_dir: Path, output_dir: Path) -> None:
        self.calls.append(model_dir)
        (output_dir / CONFIG_BASENAME).write_bytes((model_dir / CONFIG_BASENAME).read_bytes())
        shard_names = [entry.name for entry in model_dir.iterdir() if entry.name != CONFIG_BASENAME]
        assert len(shard_names) == 1
        (output_dir / shard_names[0]).write_bytes(self.converted_payloads[shard_names[0]])


def test_run_approved_conversion_is_unreachable_while_manifest_is_empty(tmp_path: Path) -> None:
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.run_approved_conversion(
            interactive_check=lambda: True,
            approved=True,
            destination_dir=tmp_path / "d",
            final_converted_dir=tmp_path / "c",
            temp_output_parent=tmp_path / "t",
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
            converter_basename=common.EXPECTED_CONVERTER_SCRIPT_BASENAME,
            converter_size_bytes=10,
            converter_sha256=HASH_A,
            downloader=_NeverCallDownloader(),
            converter=_NeverCallConverter(),
        )


def test_run_approved_conversion_full_sequence_with_reviewed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {
        CONFIG_BASENAME: b'{"real": true}',
        SHARD_BASENAMES[0]: b"source-0",
        SHARD_BASENAMES[1]: b"source-1",
        SHARD_BASENAMES[2]: b"source-2",
    }
    converted_payloads = {
        SHARD_BASENAMES[0]: b"converted-0",
        SHARD_BASENAMES[1]: b"converted-1",
        SHARD_BASENAMES[2]: b"converted-2",
    }
    _reviewed_manifest(
        monkeypatch,
        shard_bytes=(payloads[SHARD_BASENAMES[0]], payloads[SHARD_BASENAMES[1]], payloads[SHARD_BASENAMES[2]]),
        config_bytes=payloads[CONFIG_BASENAME],
    )

    destination_dir = tmp_path / "source"
    final_converted_dir = tmp_path / "converted"
    temp_output_parent = tmp_path / "scratch"
    destination_dir.mkdir()
    final_converted_dir.mkdir()
    temp_output_parent.mkdir()

    downloader = _FullRunDownloader(payloads)
    converter = _FullRunConverter(converted_payloads)

    capture = conv.run_approved_conversion(
        interactive_check=lambda: True,
        approved=True,
        destination_dir=destination_dir,
        final_converted_dir=final_converted_dir,
        temp_output_parent=temp_output_parent,
        free_bytes_probe=lambda path: 2**40,
        isolated_python_env_ready=True,
        dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
        converter_basename=common.EXPECTED_CONVERTER_SCRIPT_BASENAME,
        converter_size_bytes=4096,
        converter_sha256=HASH_B,
        downloader=downloader,
        converter=converter,
    )

    assert capture["state"] == "unreviewed_conversion_capture"
    assert len(capture["shards"]) == 3
    assert {shard["source_basename"] for shard in capture["shards"]} == set(SHARD_BASENAMES)
    assert downloader.calls == [CONFIG_BASENAME, *SHARD_BASENAMES]
    # Config is retained across all three fake conversion calls, then
    # moved into the final directory exactly once.
    assert len(converter.calls) == 3
    assert (final_converted_dir / CONFIG_BASENAME).read_bytes() == payloads[CONFIG_BASENAME]
    for basename in SHARD_BASENAMES:
        assert (final_converted_dir / basename).read_bytes() == converted_payloads[basename]
        assert not (destination_dir / basename).exists()
    assert not (destination_dir / CONFIG_BASENAME).exists()  # moved, not copied
    assert list(temp_output_parent.iterdir()) == []


# ---------------------------------------------------------------------------
# Default real adapters -- structural checks only, never real network/process
# ---------------------------------------------------------------------------


def test_pinned_revision_downloader_uses_only_pinned_repository_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    payload = b"pinned bytes"
    requested_urls: list[str] = []

    class _FakeResponse:
        def __enter__(self) -> "_FakeResponse":
            self._remaining = payload
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            chunk, self._remaining = self._remaining[:size], self._remaining[size:]
            return chunk

    def _fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        requested_urls.append(request.full_url)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    destination = tmp_path / SHARD_BASENAMES[0]
    downloader = conv.PinnedRevisionFileDownloader()
    downloader.download(
        basename=SHARD_BASENAMES[0],
        expected_size_bytes=len(payload),
        expected_sha256=_sha256(payload),
        destination=destination,
    )

    assert len(requested_urls) == 1
    assert common.PINNED_MODEL_REPOSITORY in requested_urls[0]
    assert common.PINNED_MODEL_REVISION in requested_urls[0]
    assert SHARD_BASENAMES[0] in requested_urls[0]
    assert destination.read_bytes() == payload
    assert not (destination.parent / f"{SHARD_BASENAMES[0]}.partial").exists()


def test_pinned_revision_downloader_deletes_partial_on_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    payload = b"pinned bytes"

    class _FakeResponse:
        def __enter__(self) -> "_FakeResponse":
            self._remaining = payload
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            chunk, self._remaining = self._remaining[:size], self._remaining[size:]
            return chunk

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: _FakeResponse())

    destination = tmp_path / SHARD_BASENAMES[0]
    downloader = conv.PinnedRevisionFileDownloader()
    with pytest.raises(conv.ColibriStage2Failure, match="shard_verification_failed"):
        downloader.download(
            basename=SHARD_BASENAMES[0],
            expected_size_bytes=len(payload),
            expected_sha256=HASH_A,  # does not match payload
            destination=destination,
        )
    assert not destination.exists()
    assert not (destination.parent / f"{SHARD_BASENAMES[0]}.partial").exists()


def test_pinned_revision_downloader_has_no_repository_or_revision_override_parameter() -> None:
    import inspect

    signature = inspect.signature(conv.PinnedRevisionFileDownloader.download)
    names = set(signature.parameters)
    assert "repository" not in names
    assert "revision" not in names


def test_pinned_script_converter_rejects_wrong_script_basename(tmp_path: Path) -> None:
    wrong_script = tmp_path / "not_convert_olmoe.py"
    wrong_script.write_text("pass")
    converter = conv.PinnedScriptConverter(converter_script_path=wrong_script)
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        converter.convert(model_dir=tmp_path, output_dir=tmp_path)


def test_pinned_script_converter_uses_shell_free_argv_with_current_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_module
    import sys

    script = tmp_path / "convert_olmoe.py"
    script.write_text("pass")
    recorded: dict[str, object] = {}

    def _fake_run(argv: list[str], **kwargs: object) -> None:
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    converter = conv.PinnedScriptConverter(converter_script_path=script)
    converter.convert(model_dir=tmp_path / "model", output_dir=tmp_path / "out")

    assert recorded["argv"][0] == sys.executable
    assert recorded["argv"][1] == str(script)
    assert recorded["kwargs"]["shell"] is False


# ---------------------------------------------------------------------------
# Privacy-safe conversion capture
# ---------------------------------------------------------------------------


def _capture_kwargs(**overrides: object) -> dict[str, object]:
    shard_results = [
        conv.ShardTransactionResult(
            source_basename=basename,
            converted_basename=basename,
            source_deleted=True,
            converted_sha256=HASH_A,
            converted_size_bytes=100,
            elapsed_ms=10,
        )
        for basename in SHARD_BASENAMES
    ]
    kwargs: dict[str, object] = dict(
        converter_basename=common.EXPECTED_CONVERTER_SCRIPT_BASENAME,
        converter_size_bytes=4096,
        converter_sha256=HASH_B,
        source_config=_entry(CONFIG_BASENAME, b'{"fake": true}'),
        converted_config_sha256=HASH_A,
        converted_config_size_bytes=64,
        shard_results=shard_results,
        dependency_versions={"python": "3.11.9", "torch": "2.3.0"},
        total_elapsed_ms=999,
        cleanup_complete=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_conversion_capture_is_closed_and_privacy_safe(tmp_path: Path) -> None:
    capture = conv.build_conversion_capture(**_capture_kwargs())
    assert capture["state"] == "unreviewed_conversion_capture"
    assert capture["schema_version"] == common.CONVERSION_CAPTURE_SCHEMA_VERSION
    assert capture["model_repository"] == common.PINNED_MODEL_REPOSITORY
    assert capture["model_revision"] == common.PINNED_MODEL_REVISION
    assert capture["license_identifier"] == common.PINNED_LICENSE_IDENTIFIER
    assert capture["colibri_commit"] == common.PINNED_COLIBRI_COMMIT
    assert len(capture["shards"]) == 3
    serialized = repr(capture)
    assert str(tmp_path) not in serialized
    assert "C:\\" not in serialized
    assert "/home" not in serialized


def test_conversion_capture_requires_exactly_three_pinned_shards() -> None:
    kwargs = _capture_kwargs()
    kwargs["shard_results"] = kwargs["shard_results"][:2]  # type: ignore[index]
    with pytest.raises(ValueError):
        conv.build_conversion_capture(**kwargs)


def test_conversion_capture_rejects_unknown_dependency_names() -> None:
    kwargs = _capture_kwargs(dependency_versions={"rust": "1.0.0"})
    with pytest.raises(ValueError):
        conv.build_conversion_capture(**kwargs)


def test_conversion_capture_rejects_transformers_as_a_dependency() -> None:
    assert "transformers" not in common.ALLOWED_CONVERSION_DEPENDENCY_NAMES
    kwargs = _capture_kwargs(dependency_versions={"transformers": "4.40.0"})
    with pytest.raises(ValueError):
        conv.build_conversion_capture(**kwargs)


def test_conversion_capture_never_validates_as_a_reviewed_manifest() -> None:
    capture = conv.build_conversion_capture(**_capture_kwargs())
    with pytest.raises(TypeError):
        manifest_mod.OlmoeModelManifest(**capture)  # type: ignore[arg-type]
    assert "schema_version" in capture  # a key OlmoeModelManifest does not accept
    assert "shards" in capture
