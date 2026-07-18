"""Developer-only runner for the pinned Colibrì native exactness fixture.

This module is intentionally not imported by production RPC code. It executes
one explicit local executable with no arguments, never invokes a shell, and
never returns or logs child output or machine paths.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

EXECUTABLE_ENV = "ODYSSEUS_COLIBRI_PROOF_EXECUTABLE"
FIXTURE_ENV = "ODYSSEUS_COLIBRI_PROOF_FIXTURE"
PROOF_TIMEOUT_SECONDS = 30.0
EXPECTED_FIXTURE_NAME = "test_idot.c"
EXPECTED_FIXTURE_SHA256 = "5c80caf2fa4a3f22f1497e0eacacf9025d28d5c2ece191cc4a0e966c049768dc"

_EXACT_OUTPUT = re.compile(
    rb"idot kernel exactness \(([a-z0-9_.+-]+)\): ok\r?\n"
    rb"idot driver exactness \(\1\): ok\r?\n?"
)
_CHILD_ENV_ALLOWLIST = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")


@dataclass(frozen=True, slots=True)
class NativeProofResult:
    category: str
    ok: bool
    detail: str
    exit_code: int | None = None
    elapsed_ms: int = 0
    executable_sha256: str | None = None
    fixture_sha256: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    kernel: str | None = None


def _result(category: str, detail: str, **values: object) -> NativeProofResult:
    return NativeProofResult(category=category, ok=category == "passed", detail=detail, **values)


def _resolve_regular_file(raw_path: str) -> Path | None:
    if not raw_path or not raw_path.strip():
        return None
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except OSError:
        return None
    return path if path.is_file() else None


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


def run_native_proof(executable_path: str, fixture_path: str) -> NativeProofResult:
    """Run the pinned native oracle and return only fixed, privacy-safe fields."""

    executable = _resolve_regular_file(executable_path)
    if executable is None or executable.suffix.lower() != ".exe":
        return _result("invalid_executable", "The configured proof executable is invalid.")

    fixture = _resolve_regular_file(fixture_path)
    if fixture is None or fixture.name != EXPECTED_FIXTURE_NAME:
        return _result("invalid_fixture", "The configured proof fixture is invalid.")

    try:
        executable_hash = _sha256(executable)
    except OSError:
        return _result("invalid_executable", "The configured proof executable could not be read.")
    try:
        fixture_hash = _sha256(fixture)
    except OSError:
        return _result("invalid_fixture", "The configured proof fixture could not be read.")

    if fixture_hash != EXPECTED_FIXTURE_SHA256:
        return _result(
            "invalid_fixture",
            "The proof fixture does not match the pinned upstream source.",
            executable_sha256=executable_hash,
            fixture_sha256=fixture_hash,
        )

    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - explicit argv; shell is disabled
            [str(executable)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=PROOF_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            env=_child_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return _result(
            "timeout",
            "The native proof exceeded its fixed time limit.",
            elapsed_ms=elapsed_ms,
            executable_sha256=executable_hash,
            fixture_sha256=fixture_hash,
            stdout_bytes=len(exc.output or b""),
            stderr_bytes=len(exc.stderr or b""),
        )
    except OSError:
        return _result(
            "launch_failed",
            "The native proof executable could not be started.",
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            executable_sha256=executable_hash,
            fixture_sha256=fixture_hash,
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    common = {
        "exit_code": completed.returncode,
        "elapsed_ms": elapsed_ms,
        "executable_sha256": executable_hash,
        "fixture_sha256": fixture_hash,
        "stdout_bytes": len(completed.stdout),
        "stderr_bytes": len(completed.stderr),
    }
    if completed.returncode != 0:
        return _result("nonzero_exit", "The native proof exited unsuccessfully.", **common)

    match = _EXACT_OUTPUT.fullmatch(completed.stdout)
    if match is None:
        return _result("output_mismatch", "The native proof output did not match the oracle.", **common)

    return _result(
        "passed",
        "The native integer-dot kernel and driver matched the scalar oracle.",
        kernel=match.group(1).decode("ascii"),
        **common,
    )
