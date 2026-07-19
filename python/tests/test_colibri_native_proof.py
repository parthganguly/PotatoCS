"""Safe runner tests plus the opt-in real Colibrì native proof."""

from __future__ import annotations

import dataclasses
import io
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from odysseus_desktop_backend.services import colibri_native_proof


def _artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    engine = tmp_path / colibri_native_proof.EXPECTED_ENGINE_NAME
    oracle = tmp_path / colibri_native_proof.EXPECTED_ORACLE_NAME
    fixture = tmp_path / colibri_native_proof.EXPECTED_FIXTURE_NAME
    engine.write_bytes(b"engine executable")
    oracle.write_bytes(b"oracle executable")
    fixture.write_bytes(b"fixture source")
    return engine, oracle, fixture


def _trust_test_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    engine: Path,
    oracle: Path,
    fixture: Path,
) -> None:
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_ENGINE_SHA256", colibri_native_proof._sha256(engine))
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_ORACLE_SHA256", colibri_native_proof._sha256(oracle))
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_FIXTURE_SHA256", colibri_native_proof._sha256(fixture))


def _execution(
    *,
    stdout: bytes = colibri_native_proof.EXPECTED_STDOUT,
    stderr_bytes: int = 0,
    exit_code: int = 0,
    category: str = "completed",
) -> colibri_native_proof._ExecutionResult:
    return colibri_native_proof._ExecutionResult(
        category=category,
        exit_code=exit_code,
        elapsed_ms=1,
        stdout_matches_expected=(stdout == colibri_native_proof.EXPECTED_STDOUT),
        stdout_bytes=len(stdout),
        stderr_bytes=stderr_bytes,
    )


def test_invalid_paths_return_fixed_categories() -> None:
    result = colibri_native_proof.run_native_proof("", "", "")
    assert result.category == "invalid_engine"
    assert result.detail == "The configured proof engine is invalid."


def test_exact_filenames_are_required(tmp_path: Path) -> None:
    engine, oracle, fixture = _artifacts(tmp_path)
    renamed = tmp_path / "other.exe"
    renamed.write_bytes(engine.read_bytes())
    result = colibri_native_proof.run_native_proof(str(renamed), str(oracle), str(fixture))
    assert result.category == "invalid_engine"


def test_unrelated_oracle_with_exact_output_is_rejected_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, oracle, fixture = _artifacts(tmp_path)
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_ENGINE_SHA256", colibri_native_proof._sha256(engine))
    monkeypatch.setattr(colibri_native_proof, "EXPECTED_FIXTURE_SHA256", colibri_native_proof._sha256(fixture))
    launcher = mock.Mock(return_value=_execution())
    monkeypatch.setattr(colibri_native_proof, "_execute_bounded", launcher)

    result = colibri_native_proof.run_native_proof(str(engine), str(oracle), str(fixture))

    assert result.category == "oracle_hash_mismatch"
    assert result.oracle_sha256 == colibri_native_proof._sha256(oracle)
    assert str(tmp_path) not in repr(result)
    launcher.assert_not_called()


def test_all_reviewed_hashes_are_exposed_without_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, oracle, fixture = _artifacts(tmp_path)
    _trust_test_artifacts(monkeypatch, engine, oracle, fixture)
    monkeypatch.setattr(colibri_native_proof, "_execute_bounded", mock.Mock(return_value=_execution()))

    result = colibri_native_proof.run_native_proof(str(engine), str(oracle), str(fixture))

    assert result.engine_sha256 == colibri_native_proof._sha256(engine)
    assert result.oracle_sha256 == colibri_native_proof._sha256(oracle)
    assert result.fixture_sha256 == colibri_native_proof._sha256(fixture)
    assert str(tmp_path) not in repr(result)


class _CompletedProcess:
    def __init__(self) -> None:
        self.stdout = io.BytesIO(colibri_native_proof.EXPECTED_STDOUT)
        self.stderr = io.BytesIO(b"")
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -1

    def kill(self) -> None:
        self.returncode = -1

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def test_runner_uses_shell_free_argv_and_unbuffered_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = mock.Mock(return_value=_CompletedProcess())
    monkeypatch.setattr(colibri_native_proof.subprocess, "Popen", launcher)

    result = colibri_native_proof._execute_bounded(["oracle.exe"], 1.0)

    assert result.category == "completed"
    assert result.stdout_matches_expected is True
    args, kwargs = launcher.call_args
    assert args == (["oracle.exe"],)
    assert kwargs["shell"] is False
    assert kwargs["bufsize"] == 0
    assert kwargs["env"] == colibri_native_proof._child_environment()


def test_timeout_terminates_process_and_returns_fixed_metadata() -> None:
    result = colibri_native_proof._execute_bounded(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        0.05,
    )

    assert result.category == "timeout"
    assert result.elapsed_ms < 2000
    assert result.stdout_bytes == 0
    assert result.stderr_bytes == 0


def test_output_flood_is_terminated_and_retained_memory_is_bounded() -> None:
    code = "import os\nwhile True:\n os.write(1, b'x'*8192)\n os.write(2, b'y'*8192)"
    result = colibri_native_proof._execute_bounded([sys.executable, "-c", code], 5.0)

    assert result.category == "output_overflow"
    assert result.elapsed_ms < 2000
    assert result.stdout_bytes <= colibri_native_proof.MAX_STREAM_BYTES
    assert result.stderr_bytes <= colibri_native_proof.MAX_STREAM_BYTES
    assert "x" * 100 not in repr(result)


def test_expected_windows_avx2_stdout_is_exactly_68_bytes() -> None:
    assert colibri_native_proof.EXPECTED_STDOUT == (
        b"idot kernel exactness (avx2): ok\r\n"
        b"idot driver exactness (avx2): ok\r\n"
    )
    assert len(colibri_native_proof.EXPECTED_STDOUT) == 68


@pytest.mark.parametrize(
    "stdout",
    [
        b"idot kernel exactness (scalar): ok\r\nidot driver exactness (scalar): ok\r\n",
        b"idot kernel exactness (fake): ok\r\nidot driver exactness (fake): ok\r\n",
        b"idot kernel exactness (avx512): ok\r\nidot driver exactness (avx512): ok\r\n",
        b"idot kernel exactness (avx2): ok\r\nidot driver exactness (scalar): ok\r\n",
        colibri_native_proof.EXPECTED_STDOUT + b"extra\r\n",
    ],
    ids=("scalar", "fake", "avx512", "mismatched-labels", "extra-output"),
)
def test_only_exact_avx2_stdout_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    engine, oracle, fixture = _artifacts(tmp_path)
    _trust_test_artifacts(monkeypatch, engine, oracle, fixture)
    monkeypatch.setattr(colibri_native_proof, "_execute_bounded", mock.Mock(return_value=_execution(stdout=stdout)))

    result = colibri_native_proof.run_native_proof(str(engine), str(oracle), str(fixture))

    assert result.category == "output_mismatch"
    assert result.ok is False


def test_exact_avx2_output_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, oracle, fixture = _artifacts(tmp_path)
    _trust_test_artifacts(monkeypatch, engine, oracle, fixture)
    monkeypatch.setattr(colibri_native_proof, "_execute_bounded", mock.Mock(return_value=_execution()))

    result = colibri_native_proof.run_native_proof(str(engine), str(oracle), str(fixture))

    assert result.ok is True
    assert result.category == "passed"
    assert result.kernel == "avx2"
    assert result.stdout_bytes == len(colibri_native_proof.EXPECTED_STDOUT)
    assert result.stderr_bytes == 0


def test_stderr_is_rejected_without_exposure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, oracle, fixture = _artifacts(tmp_path)
    _trust_test_artifacts(monkeypatch, engine, oracle, fixture)
    monkeypatch.setattr(
        colibri_native_proof,
        "_execute_bounded",
        mock.Mock(return_value=_execution(stderr_bytes=17)),
    )

    result = colibri_native_proof.run_native_proof(str(engine), str(oracle), str(fixture))

    assert result.category == "stderr_present"
    assert result.stderr_bytes == 17
    assert "external stderr" not in repr(result)


def test_environment_gated_real_colibri_native_proof() -> None:
    engine = os.environ.get(colibri_native_proof.ENGINE_ENV)
    oracle = os.environ.get(colibri_native_proof.ORACLE_ENV)
    fixture = os.environ.get(colibri_native_proof.FIXTURE_ENV)
    configured = (engine, oracle, fixture)
    if not any(configured):
        pytest.skip("real Colibrì native proof paths are not configured")
    if not all(configured):
        pytest.fail("all three Colibrì native proof path variables must be configured")

    assert engine is not None
    assert oracle is not None
    assert fixture is not None
    result = colibri_native_proof.run_native_proof(engine, oracle, fixture)

    assert result.category == "passed", dataclasses.asdict(result)
    assert result.exit_code == 0
    assert result.kernel == colibri_native_proof.EXPECTED_KERNEL
    assert result.engine_sha256 == colibri_native_proof.EXPECTED_ENGINE_SHA256
    assert result.oracle_sha256 == colibri_native_proof.EXPECTED_ORACLE_SHA256
    assert result.fixture_sha256 == colibri_native_proof.EXPECTED_FIXTURE_SHA256
    assert result.stdout_bytes == len(colibri_native_proof.EXPECTED_STDOUT)
    assert result.stderr_bytes == 0
