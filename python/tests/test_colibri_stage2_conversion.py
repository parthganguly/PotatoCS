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
from odysseus_desktop_backend.services import colibri_stage2_path_safety as path_safety

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


def test_source_manifest_unreviewed_when_registry_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", MappingProxyType({}))
    with pytest.raises(conv.ColibriStage2Failure, match="source_model_manifest_unreviewed"):
        conv.require_reviewed_source_manifest()


def test_default_source_manifest_has_exactly_four_reviewed_entries() -> None:
    assert set(conv.REVIEWED_SOURCE_SHARD_MANIFEST) == set(conv.REQUIRED_SOURCE_FILES)
    assert len(conv.REVIEWED_SOURCE_SHARD_MANIFEST) == 4
    assert tuple(conv.REVIEWED_SOURCE_SHARD_MANIFEST) == conv.REQUIRED_SOURCE_FILES


def test_default_source_manifest_matches_reviewed_capture() -> None:
    expected = {
        "config.json": (828, "272998dd7ba4846dcc682f0b5a46144f4bcd9dde8e94d2f17bd8e5cf2f23d6ce"),
        "model-00001-of-00003.safetensors": (
            4997744872,
            "61874210ca7c360f43f8c622cecc12441083d40190eae3b56bc9d6e1c0a30c1e",
        ),
        "model-00002-of-00003.safetensors": (
            4997235176,
            "c523a43b8a17269d5fab33395048a83633f4d1d89c1958570cea738e2bbe80c9",
        ),
        "model-00003-of-00003.safetensors": (
            3843741912,
            "97ae01e3519c52e63a018bca96ab17a89c4cd5cab1c6d742efed0fa5c0e2bb17",
        ),
    }
    for basename, (size_bytes, sha256) in expected.items():
        entry = conv.REVIEWED_SOURCE_SHARD_MANIFEST[basename]
        assert entry.basename == basename
        assert entry.size_bytes == size_bytes
        assert entry.sha256 == sha256


def test_default_source_manifest_exact_total_bytes() -> None:
    total = sum(entry.size_bytes for entry in conv.REVIEWED_SOURCE_SHARD_MANIFEST.values())
    assert total == 13_838_722_788


def test_default_source_manifest_is_immutable() -> None:
    assert isinstance(conv.REVIEWED_SOURCE_SHARD_MANIFEST, MappingProxyType)
    with pytest.raises(TypeError):
        conv.REVIEWED_SOURCE_SHARD_MANIFEST["config.json"] = None  # type: ignore[index]


def test_default_source_manifest_satisfies_require_reviewed_source_manifest() -> None:
    reviewed = conv.require_reviewed_source_manifest()
    assert dict(reviewed) == dict(conv.REVIEWED_SOURCE_SHARD_MANIFEST)


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", MappingProxyType({}))

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", MappingProxyType({}))
    destination = tmp_path / "dest"
    converted = tmp_path / "converted"
    exit_code = conv.main(
        ["--destination", str(destination), "--converted-destination", str(converted), "--approve"]
    )
    assert exit_code != 0  # nonzero on a closed rejection
    out = capsys.readouterr().out
    assert '"mode": "approved_rejected"' in out
    assert '"rejection_category": "source_model_manifest_unreviewed"' in out
    assert not destination.exists()


def test_cli_approve_blocks_before_every_side_effect_while_manifest_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # With the source manifest empty, the CLI must fail closed before
    # creating directories, opening the converter file, probing
    # dependencies, making a network request, or launching a subprocess --
    # so every one of those is wired to explode if reached.
    import subprocess as subprocess_module
    import urllib.request

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not run before the source manifest gate")

    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", MappingProxyType({}))
    monkeypatch.setattr(conv, "_default_dependency_versions", _boom)
    monkeypatch.setattr(conv, "_default_isolated_python_env_ready", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(subprocess_module, "run", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)

    destination = tmp_path / "dest"
    converted = tmp_path / "converted"
    converter_script = tmp_path / "convert_olmoe.py"
    exit_code = conv.main(
        [
            "--destination", str(destination),
            "--converted-destination", str(converted),
            "--converter-script", str(converter_script),
            "--approve",
        ]
    )
    assert exit_code != 0
    out = capsys.readouterr().out
    assert '"rejection_category": "source_model_manifest_unreviewed"' in out
    assert not destination.exists()
    assert not converted.exists()
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


def test_run_approved_conversion_is_unreachable_while_manifest_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", MappingProxyType({}))
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
        downloader=downloader,
        converter=converter,
    )

    assert capture["state"] == "unreviewed_conversion_capture"
    assert capture["converter_basename"] == common.REVIEWED_CONVERTER_IDENTITY.basename
    assert capture["converter_sha256"] == common.REVIEWED_CONVERTER_IDENTITY.sha256
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


def test_run_approved_conversion_never_overwrites_an_existing_final_config(
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

    # Simulate a race: something else places a config.json into the final
    # directory exactly at the moment of the fourth (and last) atomic
    # placement -- the config move itself. check_approved_preconditions
    # already requires converted_dir to start empty, so the only way this
    # scenario arises for real is a race occurring mid-run, not a
    # pre-existing file before the run starts.
    import unittest.mock as mock

    real_atomic_move = conv.atomic_no_replace_move
    call_count = {"n": 0}

    def _racy_atomic_move(source: Path, destination: Path, *, exists_category: str) -> None:
        call_count["n"] += 1
        if call_count["n"] == 4:  # three shard placements, then the config placement
            destination.write_bytes(b"a race-created, unrelated config")
        real_atomic_move(source, destination, exists_category=exists_category)

    with mock.patch.object(conv, "atomic_no_replace_move", side_effect=_racy_atomic_move):
        with pytest.raises(conv.ColibriStage2Failure, match="converted_shard_already_exists"):
            conv.run_approved_conversion(
                interactive_check=lambda: True,
                approved=True,
                destination_dir=destination_dir,
                final_converted_dir=final_converted_dir,
                temp_output_parent=temp_output_parent,
                free_bytes_probe=lambda path: 2**40,
                isolated_python_env_ready=True,
                dependency_versions={"torch": "2.3.0", "safetensors": "0.4.2"},
                downloader=downloader,
                converter=converter,
            )
    # The race-created final config survives completely untouched.
    assert (final_converted_dir / CONFIG_BASENAME).read_bytes() == b"a race-created, unrelated config"


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


def _patch_reviewed_converter_identity(monkeypatch: pytest.MonkeyPatch, script_path: Path) -> None:
    data = script_path.read_bytes()
    monkeypatch.setattr(
        common,
        "REVIEWED_CONVERTER_IDENTITY",
        common.ReviewedConverterIdentity(
            basename=script_path.name,
            size_bytes=len(data),
            sha256=_sha256(data),
            colibri_commit=common.PINNED_COLIBRI_COMMIT,
        ),
    )


def test_pinned_script_converter_rejects_content_not_matching_reviewed_hash(tmp_path: Path) -> None:
    # Correct basename, but the CLI never trusts a caller-provided expected
    # hash -- the real bytes must match common.REVIEWED_CONVERTER_IDENTITY,
    # which this test deliberately leaves unpatched (still the real pinned
    # identity), so this synthetic 4-byte script cannot possibly match it.
    script = tmp_path / "convert_olmoe.py"
    script.write_text("pass")
    converter = conv.PinnedScriptConverter(converter_script_path=script)
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        converter.convert(model_dir=tmp_path, output_dir=tmp_path)


def test_pinned_script_converter_uses_shell_free_argv_with_current_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_module
    import sys

    script = tmp_path / "convert_olmoe.py"
    script.write_text("pass")
    _patch_reviewed_converter_identity(monkeypatch, script)
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


def test_converter_argv_uses_model_and_out_never_output_or_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_module

    script = tmp_path / "convert_olmoe.py"
    script.write_text("pass")
    _patch_reviewed_converter_identity(monkeypatch, script)
    recorded: dict[str, object] = {}

    def _fake_run(argv: list[str], **kwargs: object) -> None:
        recorded["argv"] = argv

    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    converter = conv.PinnedScriptConverter(converter_script_path=script)
    model_dir = tmp_path / "model"
    output_dir = tmp_path / "out"
    converter.convert(model_dir=model_dir, output_dir=output_dir)

    argv = recorded["argv"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == str(model_dir)
    assert "--out" in argv
    assert argv[argv.index("--out") + 1] == str(output_dir)
    assert "--output" not in argv
    assert "--repo" not in argv


def test_reviewed_converter_identity_matches_the_verified_local_checkout() -> None:
    checkout_script = Path(
        r"C:\Users\Parth Ganguly\Documents\Codex\odysseus-colibri-proof-deps\colibri-72d3d372\c\tools\convert_olmoe.py"
    )
    if not checkout_script.is_file():
        pytest.skip("verified local Colibrì checkout is not present on this machine")
    data = checkout_script.read_bytes()
    assert checkout_script.name == common.REVIEWED_CONVERTER_IDENTITY.basename
    assert len(data) == common.REVIEWED_CONVERTER_IDENTITY.size_bytes
    assert hashlib.sha256(data).hexdigest() == common.REVIEWED_CONVERTER_IDENTITY.sha256


def test_require_reviewed_converter_identity_rejects_tampered_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "convert_olmoe.py"
    script.write_bytes(b"original-bytes-x")
    _patch_reviewed_converter_identity(monkeypatch, script)
    conv.require_reviewed_converter_identity(script)  # matches -- no raise

    script.write_bytes(b"tampered-bytes-x")  # same length, different content/hash
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.require_reviewed_converter_identity(script)


# ---------------------------------------------------------------------------
# Privacy-safe conversion capture
# ---------------------------------------------------------------------------


def _valid_shard_result(basename: str, **overrides: object) -> conv.ShardTransactionResult:
    kwargs: dict[str, object] = dict(
        source_basename=basename,
        source_size_bytes=64,
        source_sha256=HASH_A,
        source_verified=True,
        source_deleted=True,
        converted_basename=basename,
        converted_size_bytes=100,
        converted_sha256=HASH_B,
        partial_cleanup_complete=True,
        temporary_output_cleanup_complete=True,
        elapsed_ms=10,
    )
    kwargs.update(overrides)
    return conv.ShardTransactionResult(**kwargs)


def _capture_kwargs(**overrides: object) -> dict[str, object]:
    shard_results = [_valid_shard_result(basename) for basename in SHARD_BASENAMES]
    kwargs: dict[str, object] = dict(
        source_config=_entry(CONFIG_BASENAME, b'{"fake": true}'),
        source_config_verified=True,
        source_config_moved_to_final=True,
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


# ---------------------------------------------------------------------------
# Path-safety ancestor-walk-order correction (Blocker/Part 5)
# ---------------------------------------------------------------------------


def test_symlink_ancestor_is_rejected_before_resolution(tmp_path: Path) -> None:
    # The real regression: an ANCESTOR of the target directory (not the
    # leaf itself) is a symlink. The old, buggy implementation resolved
    # the leaf path first and only then walked the *resolved* path's
    # ancestors -- silently erasing this symlinked segment from the chain
    # before it was ever inspected, which would have incorrectly PASSED
    # this case. The corrected implementation walks the original lexical
    # chain first.
    real_target = tmp_path / "real-ancestor"
    real_target.mkdir()
    leaf = real_target / "actual-dir"
    leaf.mkdir()
    link_ancestor = tmp_path / "link-ancestor"
    try:
        link_ancestor.symlink_to(real_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted on this machine")

    candidate = link_ancestor / "actual-dir"
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        path_safety.require_ordinary_directory(
            candidate, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
        )


def test_synthetic_reparse_ancestor_is_rejected_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Windows-reparse-point analog of the symlink-ancestor test above,
    # for machines where creating real symlinks is not permitted. Only the
    # ANCESTOR is tampered -- the leaf itself is an ordinary directory.
    ancestor = tmp_path / "junction-like-ancestor"
    ancestor.mkdir()
    leaf = ancestor / "child"
    leaf.mkdir()

    real_lstat = path_safety.os.lstat

    class _FakeStatResult:
        def __init__(self, real_result: object) -> None:
            self.st_mode = real_result.st_mode  # type: ignore[attr-defined]
            self.st_file_attributes = 0x400

    def _fake_lstat(path: object, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == ancestor:  # type: ignore[arg-type]
            return _FakeStatResult(result)
        return result

    monkeypatch.setattr(path_safety.os, "lstat", _fake_lstat)
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        path_safety.require_ordinary_directory(
            leaf, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
        )


def test_require_ordinary_directory_accepts_a_clean_nested_path(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    resolved = path_safety.require_ordinary_directory(
        nested, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    assert resolved.is_dir()


def test_require_ordinary_directory_rejects_relative_paths() -> None:
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        path_safety.require_ordinary_directory(
            Path("relative/path"),
            missing_category="unsafe_directory_rejected",
            reparse_category="unsafe_directory_rejected",
        )


@pytest.mark.parametrize(
    "basename",
    [
        "a/b",
        "a\\b",
        "/etc/passwd",
        "\\\\server\\share",
        "C:foo",
        "C:\\foo",
        ".",
        "..",
        "..\\config.json",
        "../config.json",
        "",
    ],
    ids=[
        "forward-slash",
        "backslash",
        "posix-absolute",
        "unc-style",
        "drive-qualified-relative",
        "drive-qualified-absolute",
        "dot",
        "dot-dot",
        "traversal-backslash",
        "traversal-forward-slash",
        "empty",
    ],
)
def test_require_direct_child_path_rejects_unsafe_basenames(tmp_path: Path, basename: str) -> None:
    resolved_dir = path_safety.require_ordinary_directory(
        tmp_path, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        path_safety.require_direct_child_path(resolved_dir, basename, category="unsafe_directory_rejected")


def test_require_direct_child_path_accepts_an_ordinary_basename(tmp_path: Path) -> None:
    resolved_dir = path_safety.require_ordinary_directory(
        tmp_path, missing_category="unsafe_directory_rejected", reparse_category="unsafe_directory_rejected"
    )
    candidate = path_safety.require_direct_child_path(
        resolved_dir, "config.json", category="unsafe_directory_rejected"
    )
    assert candidate == resolved_dir / "config.json"


# ---------------------------------------------------------------------------
# Atomic no-replace placement (Blocker/Part 6)
# ---------------------------------------------------------------------------


def test_atomic_no_replace_move_happy_path(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "destination.bin"
    path_safety.atomic_no_replace_move(source, destination, exists_category="converted_shard_already_exists")
    assert destination.read_bytes() == b"payload"
    assert not source.exists()


def test_atomic_no_replace_move_rejects_a_race_created_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "destination.bin"
    # Simulate a destination introduced by a concurrent racer immediately
    # before placement.
    destination.write_bytes(b"race winner content")

    with pytest.raises(conv.ColibriStage2Failure, match="converted_shard_already_exists"):
        path_safety.atomic_no_replace_move(source, destination, exists_category="converted_shard_already_exists")

    # The race-created destination survives completely unchanged, and the
    # source is never consumed by a failed attempt.
    assert destination.read_bytes() == b"race winner content"
    assert source.read_bytes() == b"payload"


def test_shard_transaction_no_replace_rejects_race_created_final_shard(tmp_path: Path) -> None:
    # A destination file introduced immediately before the atomic move
    # step (after the "already exists" pre-check has already passed) must
    # still be rejected, not silently overwritten.
    destination_dir, config_path, final_converted_dir, temp_output_parent = _shard_setup(tmp_path)
    shard_payload = b"source shard bytes"
    entry = _entry(SHARD_BASENAMES[0], shard_payload)
    downloader = _RecordingDownloader({SHARD_BASENAMES[0]: shard_payload})
    converted_payload = b"converted bytes"
    converter = _RecordingConverter(converted_payload)

    real_atomic_move = conv.atomic_no_replace_move

    def _racy_atomic_move(source: Path, destination: Path, *, exists_category: str) -> None:
        destination.write_bytes(b"race winner content")
        real_atomic_move(source, destination, exists_category=exists_category)

    import unittest.mock as mock

    with mock.patch.object(conv, "atomic_no_replace_move", side_effect=_racy_atomic_move):
        with pytest.raises(conv.ColibriStage2Failure, match="converted_shard_already_exists"):
            conv.run_shard_transaction(
                expected_source=entry,
                destination_dir=destination_dir,
                config_path=config_path,
                final_converted_dir=final_converted_dir,
                temp_output_parent=temp_output_parent,
                downloader=downloader,
                converter=converter,
            )
    assert (final_converted_dir / SHARD_BASENAMES[0]).read_bytes() == b"race winner content"


# ---------------------------------------------------------------------------
# Approved CLI full integration (Part 4/7) -- fake network/subprocess only
# ---------------------------------------------------------------------------


def test_cli_approve_invokes_orchestrator_when_gates_are_synthetically_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess as subprocess_module
    import urllib.request

    payloads = {
        CONFIG_BASENAME: b'{"real": true}',
        SHARD_BASENAMES[0]: b"source-0",
        SHARD_BASENAMES[1]: b"source-1",
        SHARD_BASENAMES[2]: b"source-2",
    }
    _reviewed_manifest(
        monkeypatch,
        shard_bytes=(payloads[SHARD_BASENAMES[0]], payloads[SHARD_BASENAMES[1]], payloads[SHARD_BASENAMES[2]]),
        config_bytes=payloads[CONFIG_BASENAME],
    )

    converter_script = tmp_path / "convert_olmoe.py"
    converter_script.write_text("pass")
    _patch_reviewed_converter_identity(monkeypatch, converter_script)

    monkeypatch.setattr(conv, "_default_isolated_python_env_ready", lambda: True)
    monkeypatch.setattr(conv, "_default_dependency_versions", lambda: {"torch": "2.3.0", "safetensors": "0.4.2"})
    monkeypatch.setattr(conv, "_default_interactive_check", lambda: True)
    monkeypatch.setattr(conv, "_default_free_bytes_probe", lambda path: 2**40)

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._remaining = payload

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            chunk, self._remaining = self._remaining[:size], self._remaining[size:]
            return chunk

    def _fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        requested_basename = request.full_url.rsplit("/", 1)[-1]
        return _FakeResponse(payloads[requested_basename])

    def _fake_subprocess_run(argv: list[str], **kwargs: object) -> None:
        model_dir = Path(argv[argv.index("--model") + 1])
        output_dir = Path(argv[argv.index("--out") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / CONFIG_BASENAME).write_bytes((model_dir / CONFIG_BASENAME).read_bytes())
        shard_names = [entry.name for entry in model_dir.iterdir() if entry.name != CONFIG_BASENAME]
        assert len(shard_names) == 1
        (output_dir / shard_names[0]).write_bytes(b"converted-" + shard_names[0].encode("ascii"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(subprocess_module, "run", _fake_subprocess_run)

    destination = tmp_path / "dest"
    converted = tmp_path / "converted"
    exit_code = conv.main(
        [
            "--destination", str(destination),
            "--converted-destination", str(converted),
            "--converter-script", str(converter_script),
            "--approve",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"state": "unreviewed_conversion_capture"' in out
    assert '"mode"' not in out  # only the closed capture is printed on success
    assert (converted / CONFIG_BASENAME).exists()
    for basename in SHARD_BASENAMES:
        assert (converted / basename).exists()
        assert not (destination / basename).exists()


# ---------------------------------------------------------------------------
# Approved-mode preflight ordering (Blocker 1)
# ---------------------------------------------------------------------------


def test_noninteractive_approved_mode_has_zero_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess as subprocess_module
    import urllib.request

    _reviewed_manifest(monkeypatch)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not run before the interactive check passes")

    monkeypatch.setattr(conv, "_default_interactive_check", lambda: False)
    monkeypatch.setattr(conv, "_default_isolated_python_env_ready", _boom)
    monkeypatch.setattr(conv, "_default_dependency_versions", _boom)
    monkeypatch.setattr(conv, "require_reviewed_converter_identity", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(subprocess_module, "run", _boom)

    destination = tmp_path / "dest"
    converted = tmp_path / "converted"
    converter_script = tmp_path / "convert_olmoe.py"  # never read
    exit_code = conv.main(
        [
            "--destination", str(destination),
            "--converted-destination", str(converted),
            "--converter-script", str(converter_script),
            "--approve",
        ]
    )
    assert exit_code != 0
    out = capsys.readouterr().out
    assert '"rejection_category": "noninteractive_approval_rejected"' in out
    assert not destination.exists()
    assert not converted.exists()


def test_dependency_probe_occurs_only_after_interactive_and_venv_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _reviewed_manifest(monkeypatch)
    call_order: list[str] = []

    def _interactive_check() -> bool:
        call_order.append("interactive_check")
        return True

    def _venv_ready() -> bool:
        call_order.append("venv_ready")
        return True

    def _dependency_versions() -> dict[str, str]:
        call_order.append("dependency_versions")
        return {"torch": "2.3.0", "safetensors": "0.4.2"}

    def _boom_converter_identity(script_path: Path) -> None:
        call_order.append("converter_identity")
        raise conv.ColibriStage2Failure("conversion_failed")

    monkeypatch.setattr(conv, "_default_interactive_check", _interactive_check)
    monkeypatch.setattr(conv, "_default_isolated_python_env_ready", _venv_ready)
    monkeypatch.setattr(conv, "_default_dependency_versions", _dependency_versions)
    monkeypatch.setattr(conv, "require_reviewed_converter_identity", _boom_converter_identity)

    destination = tmp_path / "dest"
    converted = tmp_path / "converted"
    converter_script = tmp_path / "convert_olmoe.py"
    conv.main(
        [
            "--destination", str(destination),
            "--converted-destination", str(converted),
            "--converter-script", str(converter_script),
            "--approve",
        ]
    )

    assert call_order == ["interactive_check", "venv_ready", "dependency_versions", "converter_identity"]


def test_parent_directories_are_never_created_implicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _reviewed_manifest(monkeypatch)
    monkeypatch.setattr(conv, "_default_interactive_check", lambda: True)
    monkeypatch.setattr(conv, "_default_isolated_python_env_ready", lambda: True)
    monkeypatch.setattr(conv, "_default_dependency_versions", lambda: {"torch": "2.3.0", "safetensors": "0.4.2"})

    converter_script = tmp_path / "convert_olmoe.py"
    converter_script.write_text("pass")
    _patch_reviewed_converter_identity(monkeypatch, converter_script)

    missing_parent = tmp_path / "does-not-exist-yet"
    destination = missing_parent / "dest"
    converted = tmp_path / "converted"
    exit_code = conv.main(
        [
            "--destination", str(destination),
            "--converted-destination", str(converted),
            "--converter-script", str(converter_script),
            "--approve",
        ]
    )
    assert exit_code != 0
    out = capsys.readouterr().out
    assert '"rejection_category": "unsafe_directory_rejected"' in out
    assert not missing_parent.exists()
    assert not destination.exists()


def test_leaf_directories_are_created_only_after_every_precondition_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A fully satisfied preflight (existing parents, empty roots, real
    # free space) reaches step 10 and creates exactly the three leaves --
    # never their parents (which already existed).
    _reviewed_manifest(monkeypatch)
    monkeypatch.setattr(conv, "_default_interactive_check", lambda: True)
    monkeypatch.setattr(conv, "_default_isolated_python_env_ready", lambda: True)
    monkeypatch.setattr(conv, "_default_dependency_versions", lambda: {"torch": "2.3.0", "safetensors": "0.4.2"})
    monkeypatch.setattr(conv, "_default_free_bytes_probe", lambda path: 2**40)

    converter_script = tmp_path / "convert_olmoe.py"
    converter_script.write_text("pass")
    _patch_reviewed_converter_identity(monkeypatch, converter_script)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not perform real network/process work in this test")

    import subprocess as subprocess_module
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(subprocess_module, "run", _boom)

    destination = tmp_path / "dest"
    converted = tmp_path / "converted"
    # The booby-trapped network call fires once the leaves have already
    # been created and run_approved_conversion actually starts -- proving
    # creation happened at step 10, before any network/process work.
    with pytest.raises(AssertionError):
        conv.main(
            [
                "--destination", str(destination),
                "--converted-destination", str(converted),
                "--converter-script", str(converter_script),
                "--approve",
            ]
        )
    assert destination.is_dir()
    assert converted.is_dir()


# ---------------------------------------------------------------------------
# Complete source-to-converted capture (Blocker 3)
# ---------------------------------------------------------------------------


def test_capture_includes_all_three_exact_source_sizes_and_hashes_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {
        CONFIG_BASENAME: b'{"real": true}',
        SHARD_BASENAMES[0]: b"source-0",
        SHARD_BASENAMES[1]: b"source-01",
        SHARD_BASENAMES[2]: b"source-012",
    }
    converted_payloads = {
        SHARD_BASENAMES[0]: b"converted-0",
        SHARD_BASENAMES[1]: b"converted-01",
        SHARD_BASENAMES[2]: b"converted-012",
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
        downloader=downloader,
        converter=converter,
    )

    shards = capture["shards"]
    assert [shard["source_basename"] for shard in shards] == list(SHARD_BASENAMES)
    assert [shard["converted_basename"] for shard in shards] == list(SHARD_BASENAMES)
    for basename, shard in zip(SHARD_BASENAMES, shards):
        assert shard["source_size_bytes"] == len(payloads[basename])
        assert shard["source_sha256"] == _sha256(payloads[basename])
        assert shard["converted_size_bytes"] == len(converted_payloads[basename])
        assert shard["converted_sha256"] == _sha256(converted_payloads[basename])
        assert shard["source_verified"] is True
        assert shard["source_deleted"] is True
        assert shard["partial_cleanup_complete"] is True
        assert shard["temporary_output_cleanup_complete"] is True
    assert capture["source_config_verified"] is True
    assert capture["source_config_moved_to_final"] is True


def test_capture_rejects_false_or_incomplete_proof_booleans() -> None:
    for field in (
        "source_verified",
        "source_deleted",
        "partial_cleanup_complete",
        "temporary_output_cleanup_complete",
    ):
        kwargs = _capture_kwargs()
        shard_results = list(kwargs["shard_results"])  # type: ignore[arg-type]
        shard_results[0] = _valid_shard_result(SHARD_BASENAMES[0], **{field: False})
        kwargs["shard_results"] = shard_results
        with pytest.raises(ValueError):
            conv.build_conversion_capture(**kwargs)


def test_capture_rejects_false_source_config_proof_booleans() -> None:
    for field in ("source_config_verified", "source_config_moved_to_final"):
        kwargs = _capture_kwargs(**{field: False})
        with pytest.raises(ValueError):
            conv.build_conversion_capture(**kwargs)


def test_capture_rejects_shard_results_out_of_order() -> None:
    kwargs = _capture_kwargs()
    shard_results = list(kwargs["shard_results"])  # type: ignore[arg-type]
    shard_results[0], shard_results[1] = shard_results[1], shard_results[0]
    kwargs["shard_results"] = shard_results
    with pytest.raises(ValueError):
        conv.build_conversion_capture(**kwargs)


def test_capture_rejects_duplicate_shard_basenames() -> None:
    kwargs = _capture_kwargs()
    shard_results = list(kwargs["shard_results"])  # type: ignore[arg-type]
    shard_results[1] = _valid_shard_result(SHARD_BASENAMES[0])  # duplicate of index 0
    kwargs["shard_results"] = shard_results
    with pytest.raises(ValueError):
        conv.build_conversion_capture(**kwargs)


def test_capture_rejects_missing_shard_entry() -> None:
    kwargs = _capture_kwargs()
    shard_results = list(kwargs["shard_results"])[:2]  # type: ignore[arg-type]
    kwargs["shard_results"] = shard_results
    with pytest.raises(ValueError):
        conv.build_conversion_capture(**kwargs)


def test_capture_rejects_nonpositive_shard_sizes() -> None:
    for field in ("source_size_bytes", "converted_size_bytes"):
        kwargs = _capture_kwargs()
        shard_results = list(kwargs["shard_results"])  # type: ignore[arg-type]
        shard_results[0] = _valid_shard_result(SHARD_BASENAMES[0], **{field: 0})
        kwargs["shard_results"] = shard_results
        with pytest.raises(ValueError):
            conv.build_conversion_capture(**kwargs)


def test_capture_rejects_malformed_shard_hashes() -> None:
    for field in ("source_sha256", "converted_sha256"):
        kwargs = _capture_kwargs()
        shard_results = list(kwargs["shard_results"])  # type: ignore[arg-type]
        shard_results[0] = _valid_shard_result(SHARD_BASENAMES[0], **{field: "not-a-hash"})
        kwargs["shard_results"] = shard_results
        with pytest.raises(ValueError):
            conv.build_conversion_capture(**kwargs)


# ---------------------------------------------------------------------------
# Partial-file and converter path safety (Blocker 4)
# ---------------------------------------------------------------------------


def test_stale_partial_file_survives_untouched_and_blocks_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    destination = tmp_path / SHARD_BASENAMES[0]
    partial_path = tmp_path / f"{SHARD_BASENAMES[0]}.partial"
    partial_path.write_bytes(b"stale leftover from a crashed run")

    def _boom_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not touch the network when a partial already exists")

    monkeypatch.setattr(urllib.request, "urlopen", _boom_urlopen)

    downloader = conv.PinnedRevisionFileDownloader()
    with pytest.raises(conv.ColibriStage2Failure, match="partial_already_exists"):
        downloader.download(
            basename=SHARD_BASENAMES[0],
            expected_size_bytes=100,
            expected_sha256=HASH_A,
            destination=destination,
        )
    assert partial_path.read_bytes() == b"stale leftover from a crashed run"
    assert not destination.exists()


def test_race_created_partial_survives_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a partial file appearing exactly between path-safety
    # validation and the exclusive-create attempt.
    import urllib.request

    destination = tmp_path / SHARD_BASENAMES[0]
    partial_path = tmp_path / f"{SHARD_BASENAMES[0]}.partial"

    real_require_direct_child_path = conv.require_direct_child_path

    def _racy_require_direct_child_path(resolved_directory: Path, basename: str, *, category: str) -> Path:
        result = real_require_direct_child_path(resolved_directory, basename, category=category)
        if basename.endswith(".partial"):
            result.write_bytes(b"race winner content")
        return result

    monkeypatch.setattr(conv, "require_direct_child_path", _racy_require_direct_child_path)

    def _boom_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not touch the network when a partial already exists")

    monkeypatch.setattr(urllib.request, "urlopen", _boom_urlopen)

    downloader = conv.PinnedRevisionFileDownloader()
    with pytest.raises(conv.ColibriStage2Failure, match="partial_already_exists"):
        downloader.download(
            basename=SHARD_BASENAMES[0],
            expected_size_bytes=100,
            expected_sha256=HASH_A,
            destination=destination,
        )
    assert partial_path.read_bytes() == b"race winner content"


def test_partial_download_file_is_created_exclusively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    payload = b"pinned bytes"
    recorded_modes: list[str] = []
    real_open = Path.open

    def _recording_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self.name.endswith(".partial"):
            recorded_modes.append(mode)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _recording_open)

    class _FakeResponse:
        def __init__(self, data: bytes) -> None:
            self._remaining = data

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            chunk, self._remaining = self._remaining[:size], self._remaining[size:]
            return chunk

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: _FakeResponse(payload))

    destination = tmp_path / SHARD_BASENAMES[0]
    downloader = conv.PinnedRevisionFileDownloader()
    downloader.download(
        basename=SHARD_BASENAMES[0],
        expected_size_bytes=len(payload),
        expected_sha256=_sha256(payload),
        destination=destination,
    )
    assert destination.read_bytes() == payload
    assert recorded_modes == ["xb"]


def test_downloader_rejects_reparse_point_destination_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    real_lstat = conv.os.lstat

    class _FakeStatResult:
        def __init__(self, real_result: object) -> None:
            self.st_mode = real_result.st_mode  # type: ignore[attr-defined]
            self.st_file_attributes = 0x400

    def _fake_lstat(path: object, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == tmp_path:  # type: ignore[arg-type]
            return _FakeStatResult(result)
        return result

    monkeypatch.setattr(conv.os, "lstat", _fake_lstat)

    def _boom_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not touch the network when the destination directory is unsafe")

    monkeypatch.setattr(urllib.request, "urlopen", _boom_urlopen)

    downloader = conv.PinnedRevisionFileDownloader()
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        downloader.download(
            basename=SHARD_BASENAMES[0],
            expected_size_bytes=100,
            expected_sha256=HASH_A,
            destination=tmp_path / SHARD_BASENAMES[0],
        )


def test_require_reviewed_converter_identity_rejects_relative_path() -> None:
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        conv.require_reviewed_converter_identity(Path("relative/convert_olmoe.py"))


def test_converter_reparse_point_is_rejected_before_subprocess_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_module

    script = tmp_path / "convert_olmoe.py"
    script.write_text("pass")
    _patch_reviewed_converter_identity(monkeypatch, script)

    real_lstat = conv.os.lstat

    class _FakeStatResult:
        def __init__(self, real_result: object) -> None:
            self.st_mode = real_result.st_mode  # type: ignore[attr-defined]
            self.st_file_attributes = 0x400

    def _fake_lstat(path: object, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == script:  # type: ignore[arg-type]
            return _FakeStatResult(result)
        return result

    monkeypatch.setattr(conv.os, "lstat", _fake_lstat)

    def _boom_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not launch a subprocess for a reparse-point converter script")

    monkeypatch.setattr(subprocess_module, "run", _boom_run)

    converter = conv.PinnedScriptConverter(converter_script_path=script)
    # Caught by require_direct_child_path's own reparse-point check on the
    # script leaf itself, before ever reaching the regular-file/hash check.
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        converter.convert(model_dir=tmp_path, output_dir=tmp_path)


def test_converter_identity_is_rechecked_immediately_before_each_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_module

    script = tmp_path / "convert_olmoe.py"
    script.write_bytes(b"original content")
    _patch_reviewed_converter_identity(monkeypatch, script)

    monkeypatch.setattr(subprocess_module, "run", lambda *args, **kwargs: None)
    converter = conv.PinnedScriptConverter(converter_script_path=script)
    converter.convert(model_dir=tmp_path, output_dir=tmp_path)  # succeeds -- identity matches

    # Tamper the script's bytes AFTER the first (successful) launch. A
    # second launch must re-read and re-hash the file rather than trust
    # any state cached from the first call.
    script.write_bytes(b"tampered-content!")
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        converter.convert(model_dir=tmp_path, output_dir=tmp_path)
