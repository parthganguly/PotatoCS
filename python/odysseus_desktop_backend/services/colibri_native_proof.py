"""Developer-only runner for the pinned Colibrì native exactness proof.

This module is intentionally not imported by production RPC code. It binds the
reviewed engine, oracle executable, and source fixture before executing the
oracle with no shell. Child output is bounded and is never returned or logged.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

ENGINE_ENV = "ODYSSEUS_COLIBRI_PROOF_ENGINE"
ORACLE_ENV = "ODYSSEUS_COLIBRI_PROOF_ORACLE"
FIXTURE_ENV = "ODYSSEUS_COLIBRI_PROOF_FIXTURE"

PROOF_TIMEOUT_SECONDS = 30.0
MAX_STREAM_BYTES = 4 * 1024
_READ_CHUNK_BYTES = 512
_POLL_SECONDS = 0.01
_TERMINATE_GRACE_SECONDS = 1.0

EXPECTED_ENGINE_NAME = "glm.exe"
EXPECTED_ORACLE_NAME = "test_idot.exe"
EXPECTED_FIXTURE_NAME = "test_idot.c"
EXPECTED_ENGINE_SHA256 = "e9b4157fc2356c5fe7b348a826c37dc9b1dbf219b14bc0bd58388e7ff6af690c"
EXPECTED_ORACLE_SHA256 = "d41b8de17cebc44d5ba82a42c0eccc27179eddde2509dffcfe4ffbc475cfd0a5"
EXPECTED_FIXTURE_SHA256 = "5c80caf2fa4a3f22f1497e0eacacf9025d28d5c2ece191cc4a0e966c049768dc"
EXPECTED_KERNEL = "avx2"
EXPECTED_STDOUT = (
    b"idot kernel exactness (avx2): ok\r\n"
    b"idot driver exactness (avx2): ok\r\n"
)

_CHILD_ENV_ALLOWLIST = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")


@dataclass(frozen=True, slots=True)
class NativeProofResult:
    category: str
    ok: bool
    detail: str
    exit_code: int | None = None
    elapsed_ms: int = 0
    engine_sha256: str | None = None
    oracle_sha256: str | None = None
    fixture_sha256: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    kernel: str | None = None


@dataclass(slots=True)
class _BoundedCapture:
    retained: bytearray = field(default_factory=bytearray)
    overflowed: bool = False

    @property
    def retained_bytes(self) -> int:
        return len(self.retained)


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    category: str
    exit_code: int | None
    elapsed_ms: int
    stdout_matches_expected: bool
    stdout_bytes: int
    stderr_bytes: int


def _result(category: str, detail: str, **values: object) -> NativeProofResult:
    return NativeProofResult(category=category, ok=category == "passed", detail=detail, **values)


def _resolve_named_regular_file(raw_path: str, expected_name: str) -> Path | None:
    if not raw_path or not raw_path.strip():
        return None
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except OSError:
        return None
    if not path.is_file() or path.name != expected_name:
        return None
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _child_environment() -> dict[str, str]:
    # The static native fixture needs no user environment. Keeping only Windows
    # process essentials prevents unrelated API keys and proof paths reaching it.
    return {key: os.environ[key] for key in _CHILD_ENV_ALLOWLIST if key in os.environ}


def _read_bounded(stream: BinaryIO, capture: _BoundedCapture, overflow: threading.Event) -> None:
    while True:
        try:
            chunk = stream.read(_READ_CHUNK_BYTES)
        except OSError:
            return
        if not chunk:
            return
        remaining = MAX_STREAM_BYTES - capture.retained_bytes
        if remaining > 0:
            capture.retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            capture.overflowed = True
            overflow.set()
            return


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _execute_bounded(argv: list[str], timeout_seconds: float) -> _ExecutionResult:
    started = time.perf_counter()
    try:
        process = subprocess.Popen(  # noqa: S603 - explicit argv; shell is disabled
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=_child_environment(),
            bufsize=0,
        )
    except OSError:
        return _ExecutionResult("launch_failed", None, 0, False, 0, 0)

    assert process.stdout is not None
    assert process.stderr is not None
    overflow = threading.Event()
    stdout_capture = _BoundedCapture()
    stderr_capture = _BoundedCapture()
    readers = (
        threading.Thread(
            target=_read_bounded,
            args=(process.stdout, stdout_capture, overflow),
            name="colibri-proof-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded,
            args=(process.stderr, stderr_capture, overflow),
            name="colibri-proof-stderr",
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    category = "completed"
    deadline = started + timeout_seconds
    while process.poll() is None:
        if overflow.is_set():
            category = "output_overflow"
            _stop_process(process)
            break
        if time.perf_counter() >= deadline:
            category = "timeout"
            _stop_process(process)
            break
        time.sleep(_POLL_SECONDS)

    for reader in readers:
        reader.join(timeout=_TERMINATE_GRACE_SECONDS)

    if stdout_capture.overflowed or stderr_capture.overflowed:
        category = "output_overflow"
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return _ExecutionResult(
        category=category,
        exit_code=process.poll(),
        elapsed_ms=elapsed_ms,
        stdout_matches_expected=(
            not stdout_capture.overflowed and bytes(stdout_capture.retained) == EXPECTED_STDOUT
        ),
        stdout_bytes=stdout_capture.retained_bytes,
        stderr_bytes=stderr_capture.retained_bytes,
    )


def run_native_proof(engine_path: str, oracle_path: str, fixture_path: str) -> NativeProofResult:
    """Bind all reviewed artifacts, then run the exact AVX2 oracle safely."""

    engine = _resolve_named_regular_file(engine_path, EXPECTED_ENGINE_NAME)
    if engine is None:
        return _result("invalid_engine", "The configured proof engine is invalid.")
    oracle = _resolve_named_regular_file(oracle_path, EXPECTED_ORACLE_NAME)
    if oracle is None:
        return _result("invalid_oracle", "The configured proof oracle is invalid.")
    fixture = _resolve_named_regular_file(fixture_path, EXPECTED_FIXTURE_NAME)
    if fixture is None:
        return _result("invalid_fixture", "The configured proof fixture is invalid.")

    try:
        engine_hash = _sha256(engine)
    except OSError:
        return _result("invalid_engine", "The configured proof engine could not be read.")
    try:
        oracle_hash = _sha256(oracle)
    except OSError:
        return _result("invalid_oracle", "The configured proof oracle could not be read.")
    try:
        fixture_hash = _sha256(fixture)
    except OSError:
        return _result("invalid_fixture", "The configured proof fixture could not be read.")

    hashes = {
        "engine_sha256": engine_hash,
        "oracle_sha256": oracle_hash,
        "fixture_sha256": fixture_hash,
    }
    if engine_hash != EXPECTED_ENGINE_SHA256:
        return _result("engine_hash_mismatch", "The proof engine hash is not recognized.", **hashes)
    if oracle_hash != EXPECTED_ORACLE_SHA256:
        return _result("oracle_hash_mismatch", "The proof oracle hash is not recognized.", **hashes)
    if fixture_hash != EXPECTED_FIXTURE_SHA256:
        return _result("fixture_hash_mismatch", "The proof fixture hash is not recognized.", **hashes)

    execution = _execute_bounded([str(oracle)], PROOF_TIMEOUT_SECONDS)
    common = {
        "exit_code": execution.exit_code,
        "elapsed_ms": execution.elapsed_ms,
        "stdout_bytes": execution.stdout_bytes,
        "stderr_bytes": execution.stderr_bytes,
        **hashes,
    }
    if execution.category == "launch_failed":
        return _result("launch_failed", "The native proof oracle could not be started.", **common)
    if execution.category == "timeout":
        return _result("timeout", "The native proof exceeded its fixed time limit.", **common)
    if execution.category == "output_overflow":
        return _result("output_overflow", "The native proof exceeded its fixed output limit.", **common)
    if execution.exit_code != 0:
        return _result("nonzero_exit", "The native proof exited unsuccessfully.", **common)
    if execution.stderr_bytes != 0:
        return _result("stderr_present", "The native proof wrote unexpected error output.", **common)
    if not execution.stdout_matches_expected:
        return _result("output_mismatch", "The native proof output did not match the AVX2 oracle.", **common)

    return _result(
        "passed",
        "The AVX2 kernel and driver matched the scalar oracle.",
        kernel=EXPECTED_KERNEL,
        **common,
    )
