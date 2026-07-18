"""Safe runner tests plus the opt-in real Colibrì native proof."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from odysseus_desktop_backend.services import colibri_native_proof


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "proof.exe"
    executable.write_bytes(b"fixture executable")
    fixture = tmp_path / colibri_native_proof.EXPECTED_FIXTURE_NAME
    fixture.write_bytes(b"fixture source")
    return executable, fixture


def test_invalid_paths_return_fixed_categories() -> None:
    result = colibri_native_proof.run_native_proof("", "")
    assert result.category == "invalid_executable"
    assert result.detail == "The configured proof executable is invalid."


def test_fixture_hash_must_match_pinned_upstream(tmp_path: Path) -> None:
    executable, fixture = _artifacts(tmp_path)
    result = colibri_native_proof.run_native_proof(str(executable), str(fixture))
    assert result.category == "invalid_fixture"
    assert result.fixture_sha256 is not None
    assert str(tmp_path) not in repr(result)


def test_shell_free_argv_and_redacted_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable, fixture = _artifacts(tmp_path)
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_FIXTURE_SHA256", colibri_native_proof._sha256(fixture))
    sentinel = b"username path api-key raw external stderr"
    completed = subprocess.CompletedProcess([str(executable)], 7, stdout=b"noise", stderr=sentinel)
    runner = mock.Mock(return_value=completed)
    monkeypatch.setattr(colibri_native_proof.subprocess, "run", runner)

    result = colibri_native_proof.run_native_proof(str(executable), str(fixture))

    assert result.category == "nonzero_exit"
    assert result.stderr_bytes == len(sentinel)
    assert sentinel.decode() not in repr(result)
    args, kwargs = runner.call_args
    assert args == ([str(executable.resolve())],)
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == colibri_native_proof.PROOF_TIMEOUT_SECONDS
    assert kwargs["env"] == colibri_native_proof._child_environment()


def test_timeout_returns_only_fixed_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable, fixture = _artifacts(tmp_path)
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_FIXTURE_SHA256", colibri_native_proof._sha256(fixture))
    monkeypatch.setattr(
        colibri_native_proof.subprocess,
        "run",
        mock.Mock(side_effect=subprocess.TimeoutExpired([str(executable)], 30, output=b"private", stderr=b"secret")),
    )

    result = colibri_native_proof.run_native_proof(str(executable), str(fixture))

    assert result.category == "timeout"
    assert result.stdout_bytes == 7
    assert result.stderr_bytes == 6
    assert "private" not in repr(result)
    assert "secret" not in repr(result)


def test_exact_markers_pass_and_normalize_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable, fixture = _artifacts(tmp_path)
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_FIXTURE_SHA256", colibri_native_proof._sha256(fixture))
    stdout = b"idot kernel exactness (avx2): ok\r\nidot driver exactness (avx2): ok\r\n"
    monkeypatch.setattr(
        colibri_native_proof.subprocess,
        "run",
        mock.Mock(return_value=subprocess.CompletedProcess([str(executable)], 0, stdout=stdout, stderr=b"")),
    )

    result = colibri_native_proof.run_native_proof(str(executable), str(fixture))

    assert result.ok is True
    assert result.category == "passed"
    assert result.kernel == "avx2"
    assert result.stdout_bytes == len(stdout)
    assert result.stderr_bytes == 0


def test_environment_gated_real_colibri_native_proof() -> None:
    executable = os.environ.get(colibri_native_proof.EXECUTABLE_ENV)
    fixture = os.environ.get(colibri_native_proof.FIXTURE_ENV)
    if not executable and not fixture:
        pytest.skip("real Colibrì native proof paths are not configured")
    if not executable or not fixture:
        pytest.fail("both Colibrì native proof path variables must be configured")

    result = colibri_native_proof.run_native_proof(executable, fixture)

    assert result.category == "passed", dataclasses.asdict(result)
    assert result.exit_code == 0
    assert result.kernel is not None
    assert result.executable_sha256 is not None
    assert result.fixture_sha256 == colibri_native_proof.EXPECTED_FIXTURE_SHA256
    assert result.stderr_bytes == 0
