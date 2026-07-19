"""Phase-1 isolated Ollama server lifecycle and attestation.

This module is deliberately separate from performance artifact schemas.  It
never reads a model store and constructs the child environment from an empty
mapping.  The real lifecycle is gated by the CLI; tests use the injectable
Windows API and HTTP seams below.
"""

from __future__ import annotations

import ctypes
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Protocol


ATTESTATION_SCHEMA_VERSION = 1
ATTESTATION_ARTIFACT_KIND = "isolated_ollama_server_attestation"

# Generated exclusively by the launcher.  A caller cannot provide or
# override any of these keys, even with different casing (Windows environment
# names are case-insensitive).
FIXED_INTERNAL_ENV_KEYS = frozenset(
    {
        "SystemRoot",
        "SystemDrive",
        "PATH",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "NO_PROXY",
        "OLLAMA_HOST",
        "OLLAMA_MODELS",
        "OLLAMA_DEBUG_LOG_REQUESTS",
        "OLLAMA_NO_CLOUD",
        "OLLAMA_NOPRUNE",
        "OLLAMA_NUM_PARALLEL",
        "OLLAMA_MAX_LOADED_MODELS",
        "OLLAMA_MAX_QUEUE",
        "OLLAMA_DEBUG",
    }
)

# This is intentionally only the experiment-variable subset of schema-v2's
# SERVER_ENV_KEYS.  Phase-1 attestation normally passes an empty mapping.
USER_OVERRIDE_ENV_KEYS = frozenset(
    {
        "OLLAMA_FLASH_ATTENTION",
        "OLLAMA_KV_CACHE_TYPE",
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_CONTEXT_LENGTH",
    }
)

PHASE1_FAILURE_CATEGORIES = frozenset(
    {
        "platform_unsupported",
        "executable_not_found",
        "executable_identity_unavailable",
        "temp_space_failed",
        "port_bind_failed",
        "process_create_failed",
        "job_create_failed",
        "job_limit_configuration_failed",
        "job_assignment_failed",
        "process_resume_failed",
        "ownership_probe_unavailable",
        "port_hijacked",
        "startup_timeout",
        "startup_process_exit",
        "startup_log_overflow",
        "log_reader_failed",
        "runtime_identity_mismatch",
        "attestation_missing",
        "attestation_mismatch",
        "orphaned_runner",
        "unclean_shutdown",
        "port_not_closed",
        "teardown_incomplete",
    }
)

FAILURE_NUMERIC_METADATA_KEYS = frozenset(
    {"attempts", "bytes_observed", "exit_code", "timeout_ms", "win32_code"}
)
MAX_METADATA_NUMBER = 2**32 - 1
MAX_LOG_BYTES = 256 * 1024
FIRST_LOG_BYTES = 64 * 1024
LAST_LOG_BYTES = 192 * 1024
MAX_DURATION_MS = 24 * 60 * 60 * 1000

_SAFE_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER = (
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
_VERSION_LINE = re.compile(
    rf"^(?:(?:ollama\s+version)(?:\s+is)?\s+)?v?({_SEMVER})$",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class Phase1ContractError(ValueError):
    """Caller input violates the Phase-1 contract before side effects."""


class IsolatedServerFailure(RuntimeError):
    """A closed failure category with optional bounded numeric evidence."""

    def __init__(self, category: str, **numeric_metadata: int) -> None:
        if category not in PHASE1_FAILURE_CATEGORIES:
            raise ValueError(f"unknown Phase-1 failure category: {category}")
        clean: dict[str, int] = {}
        for key, value in numeric_metadata.items():
            if key not in FAILURE_NUMERIC_METADATA_KEYS:
                raise ValueError(f"unknown failure metadata key: {key}")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_METADATA_NUMBER:
                raise ValueError(f"failure metadata {key} is out of bounds")
            clean[key] = value
        super().__init__(category)
        self.category = category
        self.numeric_metadata = clean

    def as_record(self) -> dict[str, Any]:
        return {"category": self.category, "numeric_metadata": dict(self.numeric_metadata)}


def _casefold_map(keys: set[str] | frozenset[str]) -> dict[str, str]:
    return {key.casefold(): key for key in keys}


_FIXED_CASEFOLD = _casefold_map(FIXED_INTERNAL_ENV_KEYS)
_OVERRIDE_CASEFOLD = _casefold_map(USER_OVERRIDE_ENV_KEYS)


def validate_user_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    """Validate caller env before temp dirs, probes, sockets, or processes."""

    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise Phase1ContractError("user overrides must be an object")
    validated: dict[str, str] = {}
    for key, value in overrides.items():
        if not isinstance(key, str):
            raise Phase1ContractError("environment keys must be strings")
        folded = key.casefold()
        if folded in _FIXED_CASEFOLD:
            raise Phase1ContractError(f"caller cannot provide fixed internal key {_FIXED_CASEFOLD[folded]}")
        canonical = _OVERRIDE_CASEFOLD.get(folded)
        if canonical is None or key != canonical:
            raise Phase1ContractError(f"environment key is not an allowed user override: {key}")
        if not isinstance(value, str) or len(value) > 64 or "\x00" in value:
            raise Phase1ContractError(f"invalid value for {key}")
        if key == "OLLAMA_FLASH_ATTENTION" and value.casefold() not in {"0", "1", "true", "false", "auto"}:
            raise Phase1ContractError("invalid OLLAMA_FLASH_ATTENTION value")
        if key == "OLLAMA_KV_CACHE_TYPE" and value.casefold() not in {"f16", "q8_0", "q4_0"}:
            raise Phase1ContractError("invalid OLLAMA_KV_CACHE_TYPE value")
        if key == "OLLAMA_CONTEXT_LENGTH" and (
            not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 2**31 - 1
        ):
            raise Phase1ContractError("invalid OLLAMA_CONTEXT_LENGTH value")
        if key == "OLLAMA_KEEP_ALIVE" and not re.fullmatch(r"-?\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h)?", value):
            raise Phase1ContractError("invalid OLLAMA_KEEP_ALIVE value")
        validated[key] = value
    return validated


def normalize_ollama_version(raw: str | bytes) -> str:
    """Parse raw version output into one strict SemVer representation."""

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("version output is not UTF-8") from exc
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("version output is invalid")
    line = raw.strip()
    if "\n" in line or "\r" in line:
        raise ValueError("version output must be exactly one line")
    match = _VERSION_LINE.fullmatch(line)
    if match is None:
        raise ValueError("version output is not a supported Ollama version")
    # group 1 is the complete SemVer token.  Numeric components were already
    # checked for leading zeroes by _SEMVER.  Normalize textual identifiers to
    # lower case so command, API, and log comparisons have one representation.
    return match.group(1).lower()


@dataclass(frozen=True)
class BinaryIdentity:
    executable_basename: str
    executable_sha256: str
    binary_version: str


def probe_binary_identity(
    executable: Path,
    *,
    command_probe: Callable[[Path], str | bytes],
) -> BinaryIdentity:
    """Hash and version one resolved executable; raw output stays local."""

    try:
        resolved = executable.resolve(strict=True)
    except OSError as exc:
        raise IsolatedServerFailure("executable_not_found") from exc
    basename = resolved.name
    if not _SAFE_BASENAME.fullmatch(basename):
        raise IsolatedServerFailure("executable_identity_unavailable")
    try:
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        raw_output = command_probe(resolved)
        version = normalize_ollama_version(raw_output)
    except IsolatedServerFailure:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise IsolatedServerFailure("executable_identity_unavailable") from exc
    return BinaryIdentity(basename, digest.hexdigest(), version)


@dataclass(frozen=True)
class StartupDialect:
    """Fixture-reviewed regexes associated with an attested binary identity."""

    identity_sha256: str
    startup_version: re.Pattern[str] | None
    setting_patterns: Mapping[str, tuple[re.Pattern[str], str]]

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.identity_sha256):
            raise ValueError("dialect identity must be a SHA-256")
        unknown = set(self.setting_patterns) - ATTESTED_SETTING_KEYS
        if unknown:
            raise ValueError(f"unknown dialect settings: {sorted(unknown)}")


ATTESTED_SETTING_KEYS = frozenset(
    {"noprune", "no_cloud", "flash_attention", "kv_cache_type", "keep_alive", "context_length"}
)
_SETTING_SOURCES = frozenset({"startup_log", "runner_log"})


def _unattested_setting() -> dict[str, Any]:
    return {"state": "unattested", "value": None, "source": "unattested"}


def _normalize_attested_setting(key: str, raw: str) -> str | None:
    if not isinstance(raw, str) or not raw or len(raw) > 64:
        return None
    value = raw.casefold()
    if key in {"noprune", "no_cloud"}:
        return value if value in {"0", "1", "true", "false", "on", "off"} else None
    if key == "flash_attention":
        return value if value in {"on", "off", "auto"} else None
    if key == "kv_cache_type":
        return value if re.fullmatch(r"[a-z0-9_.-]{1,32}", value) else None
    if key == "keep_alive":
        return value if re.fullmatch(r"-?\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h)?", value) else None
    if key == "context_length":
        return value if value.isascii() and value.isdigit() and 1 <= int(value) <= 2**31 - 1 else None
    return None


def parse_startup_attestation(
    raw_capture: bytes,
    *,
    binary_identity: BinaryIdentity,
    api_version_raw: str | bytes,
    dialect: StartupDialect | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Bind normalized identity and parse typed settings in memory only."""

    try:
        api_version = normalize_ollama_version(api_version_raw)
    except ValueError as exc:
        raise IsolatedServerFailure("runtime_identity_mismatch") from exc
    if api_version != binary_identity.binary_version:
        raise IsolatedServerFailure("runtime_identity_mismatch")

    startup_state = "unattested"
    startup_value: str | None = None
    startup_source = "unattested"
    settings = {key: _unattested_setting() for key in sorted(ATTESTED_SETTING_KEYS)}

    if dialect is not None:
        if dialect.identity_sha256 != binary_identity.executable_sha256:
            raise IsolatedServerFailure("runtime_identity_mismatch")
        text = raw_capture.decode("utf-8", errors="replace")
        if dialect.startup_version is not None:
            match = dialect.startup_version.search(text)
            if match is not None:
                try:
                    startup_value = normalize_ollama_version(match.group(1))
                except (IndexError, ValueError) as exc:
                    raise IsolatedServerFailure("runtime_identity_mismatch") from exc
                if startup_value != binary_identity.binary_version:
                    raise IsolatedServerFailure("runtime_identity_mismatch")
                startup_state = "attested"
                startup_source = "startup_log"
        for key, (pattern, source) in dialect.setting_patterns.items():
            if source not in _SETTING_SOURCES:
                raise ValueError("invalid dialect source")
            match = pattern.search(text)
            if match is not None:
                value = _normalize_attested_setting(key, match.group(1))
                if value is not None:
                    settings[key] = {"state": "attested", "value": value, "source": source}

    identity = {
        "executable_basename": binary_identity.executable_basename,
        "executable_sha256": binary_identity.executable_sha256,
        "binary_version": binary_identity.binary_version,
        "api_version": api_version,
        "startup_version": {
            "state": startup_state,
            "value": startup_value,
            "source": startup_source,
        },
    }
    return identity, settings


def compare_requested_attestation(
    requested_overrides: Mapping[str, str], attested: Mapping[str, Mapping[str, Any]]
) -> list[IsolatedServerFailure]:
    """Compare only settings that Phase 1 requires or explicitly requests."""

    expected: dict[str, str] = {"noprune": "true", "no_cloud": "true"}
    if "OLLAMA_FLASH_ATTENTION" in requested_overrides:
        raw = requested_overrides["OLLAMA_FLASH_ATTENTION"].casefold()
        expected["flash_attention"] = {"1": "on", "true": "on", "0": "off", "false": "off"}.get(raw, raw)
    if "OLLAMA_KV_CACHE_TYPE" in requested_overrides:
        expected["kv_cache_type"] = requested_overrides["OLLAMA_KV_CACHE_TYPE"].casefold()
    if "OLLAMA_KEEP_ALIVE" in requested_overrides:
        expected["keep_alive"] = requested_overrides["OLLAMA_KEEP_ALIVE"].casefold()
    if "OLLAMA_CONTEXT_LENGTH" in requested_overrides:
        expected["context_length"] = requested_overrides["OLLAMA_CONTEXT_LENGTH"]

    missing = False
    mismatch = False
    truthy = {"1", "true", "on"}
    for key, wanted in expected.items():
        record = attested[key]
        if record["state"] != "attested":
            missing = True
            continue
        observed = str(record["value"]).casefold()
        if key in {"noprune", "no_cloud"}:
            if observed not in truthy:
                mismatch = True
        elif observed != wanted:
            mismatch = True
    failures: list[IsolatedServerFailure] = []
    if missing:
        failures.append(IsolatedServerFailure("attestation_missing"))
    if mismatch:
        failures.append(IsolatedServerFailure("attestation_mismatch"))
    return failures


@dataclass(frozen=True)
class SessionSpace:
    root: Path
    profile: Path
    local_app_data: Path
    scratch: Path
    model_store: Path


def create_session_space(parent: Path | None = None) -> SessionSpace:
    root: Path | None = None
    try:
        root = Path(tempfile.mkdtemp(prefix="odysseus-ollama-attest-", dir=parent))
        profile = root / "profile"
        local = profile / "AppData" / "Local"
        scratch = root / "temp"
        models = root / "empty-model-store"
        for directory in (profile, local, scratch, models):
            directory.mkdir(parents=True, exist_ok=True)
        return SessionSpace(root, profile, local, scratch, models)
    except OSError as exc:
        if root is not None:
            try:
                shutil.rmtree(root)
            except OSError:
                pass
        raise IsolatedServerFailure("temp_space_failed") from exc


def teardown_session_space(space: SessionSpace) -> None:
    try:
        shutil.rmtree(space.root)
    except OSError as exc:
        raise IsolatedServerFailure("teardown_incomplete") from exc
    if space.root.exists():
        raise IsolatedServerFailure("teardown_incomplete")


def build_child_environment(
    *,
    executable: Path,
    space: SessionSpace,
    port: int,
    user_overrides: Mapping[str, str] | None,
    parent_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the complete child environment from empty."""

    overrides = validate_user_overrides(user_overrides)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise Phase1ContractError("candidate port is invalid")
    inherited = os.environ if parent_environment is None else parent_environment
    system_root = inherited.get("SystemRoot") or inherited.get("SYSTEMROOT")
    system_drive = inherited.get("SystemDrive") or inherited.get("SYSTEMDRIVE")
    if not system_root or not system_drive:
        raise IsolatedServerFailure("platform_unsupported")
    drive, tail = os.path.splitdrive(str(space.profile))
    if not drive:
        drive, tail = system_drive, str(space.profile)
    install_dir = executable.parent
    runner_dir = install_dir / "lib" / "ollama"
    system32 = Path(system_root) / "System32"
    env = {
        "SystemRoot": system_root,
        "SystemDrive": system_drive,
        "PATH": os.pathsep.join((str(install_dir), str(runner_dir), str(system32))),
        "USERPROFILE": str(space.profile),
        "HOMEDRIVE": drive,
        "HOMEPATH": tail,
        "HOME": str(space.profile),
        "TEMP": str(space.scratch),
        "TMP": str(space.scratch),
        "LOCALAPPDATA": str(space.local_app_data),
        "NO_PROXY": "127.0.0.1,localhost",
        "OLLAMA_HOST": f"127.0.0.1:{port}",
        "OLLAMA_MODELS": str(space.model_store),
        "OLLAMA_DEBUG_LOG_REQUESTS": "0",
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_NOPRUNE": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_MAX_QUEUE": "1",
        "OLLAMA_DEBUG": "0",
    }
    env.update(overrides)
    if set(env) != FIXED_INTERNAL_ENV_KEYS | set(overrides):
        raise AssertionError("child environment is not closed")
    return env


def choose_loopback_port(exclusions: set[int] | frozenset[int], attempts: int = 8) -> int:
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 64:
        raise Phase1ContractError("port attempts must be between 1 and 64")
    if any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 for port in exclusions):
        raise Phase1ContractError("excluded ports are invalid")
    for _ in range(attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                host, port = probe.getsockname()
                if not ipaddress.ip_address(host).is_loopback:
                    continue
                if port not in exclusions:
                    return int(port)
        except OSError:
            continue
    raise IsolatedServerFailure("port_bind_failed", attempts=attempts)


class BoundedLogCapture:
    """Thread-safe first-64-KiB plus last-192-KiB memory-only capture."""

    def __init__(self) -> None:
        self._first = bytearray()
        self._last = bytearray()
        self._observed = 0
        self._lock = threading.Lock()
        self._reader_failed = False

    def feed(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("log chunks must be bytes")
        with self._lock:
            self._observed += len(data)
            remaining = FIRST_LOG_BYTES - len(self._first)
            if remaining > 0:
                self._first.extend(data[:remaining])
                data = data[remaining:]
            if data:
                self._last.extend(data)
                if len(self._last) > LAST_LOG_BYTES:
                    del self._last[: len(self._last) - LAST_LOG_BYTES]

    def drain(self, pipe: BinaryIO) -> None:
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                self.feed(chunk)
        except (OSError, ValueError, TypeError):
            with self._lock:
                self._reader_failed = True

    @property
    def observed(self) -> int:
        with self._lock:
            return self._observed

    @property
    def truncated(self) -> bool:
        return self.observed > MAX_LOG_BYTES

    @property
    def reader_failed(self) -> bool:
        with self._lock:
            return self._reader_failed

    def bytes(self) -> bytes:
        with self._lock:
            return bytes(self._first + self._last)

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            return {
                "bytes_observed": min(self._observed, MAX_METADATA_NUMBER),
                "bytes_captured": len(self._first) + len(self._last),
                "truncated": self._observed > MAX_LOG_BYTES,
            }


@dataclass
class CreatedProcess:
    process_id: int
    process_handle: Any
    thread_handle: Any
    stdout: BinaryIO
    stderr: BinaryIO


class LifecycleApi(Protocol):
    def create_suspended(self, executable: Path, environment: Mapping[str, str]) -> CreatedProcess: ...
    def create_job(self) -> Any: ...
    def configure_kill_on_close(self, job: Any) -> None: ...
    def assign_process(self, job: Any, process: CreatedProcess) -> None: ...
    def verify_job_assignment(self, job: Any, process: CreatedProcess) -> bool: ...
    def resume_process(self, process: CreatedProcess) -> None: ...
    def process_exit_code(self, process: CreatedProcess) -> int | None: ...
    def listener_owner(self, port: int) -> int | None: ...
    def process_id_in_job(self, job: Any, process_id: int) -> bool: ...
    def terminate_job(self, job: Any) -> None: ...
    def terminate_process(self, process: CreatedProcess) -> None: ...
    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool: ...
    def descendant_process_ids(self, process_id: int) -> set[int]: ...
    def close_handle(self, handle: Any) -> None: ...


class _WinSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


class _WinStartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32), ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32), ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p),
    ]


class _WinProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _WinIoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _WinBasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32),
    ]


class _WinExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WinBasicLimit), ("IoInfo", _WinIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WinProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class WindowsLifecycleApi:
    """Checked Windows API operations used by the real gated path."""

    CREATE_SUSPENDED = 0x00000004
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    STARTF_USESTDHANDLES = 0x00000100
    HANDLE_FLAG_INHERIT = 0x00000001
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    STILL_ACTIVE = 259
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    TH32CS_SNAPPROCESS = 0x00000002
    ERROR_NO_MORE_FILES = 18
    ERROR_INSUFFICIENT_BUFFER = 122
    NO_ERROR = 0
    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self) -> None:
        if os.name != "nt":
            raise IsolatedServerFailure("platform_unsupported")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        void_p = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        self.kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(void_p),
            ctypes.POINTER(void_p),
            ctypes.POINTER(_WinSecurityAttributes),
            uint32,
        ]
        self.kernel32.CreatePipe.restype = ctypes.c_int
        self.kernel32.SetHandleInformation.argtypes = [void_p, uint32, uint32]
        self.kernel32.SetHandleInformation.restype = ctypes.c_int
        self.kernel32.CreateProcessW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, void_p, void_p, ctypes.c_int,
            uint32, void_p, ctypes.c_wchar_p, ctypes.POINTER(_WinStartupInfo),
            ctypes.POINTER(_WinProcessInformation),
        ]
        self.kernel32.CreateProcessW.restype = ctypes.c_int
        self.kernel32.CreateJobObjectW.argtypes = [void_p, ctypes.c_wchar_p]
        self.kernel32.CreateJobObjectW.restype = void_p
        self.kernel32.SetInformationJobObject.argtypes = [void_p, ctypes.c_int, void_p, uint32]
        self.kernel32.SetInformationJobObject.restype = ctypes.c_int
        self.kernel32.AssignProcessToJobObject.argtypes = [void_p, void_p]
        self.kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        self.kernel32.IsProcessInJob.argtypes = [void_p, void_p, ctypes.POINTER(ctypes.c_int)]
        self.kernel32.IsProcessInJob.restype = ctypes.c_int
        self.kernel32.OpenProcess.argtypes = [uint32, ctypes.c_int, uint32]
        self.kernel32.OpenProcess.restype = void_p
        self.kernel32.ResumeThread.argtypes = [void_p]
        self.kernel32.ResumeThread.restype = uint32
        self.kernel32.GetExitCodeProcess.argtypes = [void_p, ctypes.POINTER(uint32)]
        self.kernel32.GetExitCodeProcess.restype = ctypes.c_int
        self.kernel32.TerminateJobObject.argtypes = [void_p, uint32]
        self.kernel32.TerminateJobObject.restype = ctypes.c_int
        self.kernel32.TerminateProcess.argtypes = [void_p, uint32]
        self.kernel32.TerminateProcess.restype = ctypes.c_int
        self.kernel32.WaitForSingleObject.argtypes = [void_p, uint32]
        self.kernel32.WaitForSingleObject.restype = uint32
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [uint32, uint32]
        self.kernel32.CreateToolhelp32Snapshot.restype = void_p
        self.kernel32.Process32FirstW.argtypes = [void_p, ctypes.POINTER(_WinProcessEntry)]
        self.kernel32.Process32FirstW.restype = ctypes.c_int
        self.kernel32.Process32NextW.argtypes = [void_p, ctypes.POINTER(_WinProcessEntry)]
        self.kernel32.Process32NextW.restype = ctypes.c_int
        self.kernel32.CloseHandle.argtypes = [void_p]
        self.kernel32.CloseHandle.restype = ctypes.c_int
        self.iphlpapi.GetExtendedTcpTable.argtypes = [
            void_p,
            ctypes.POINTER(uint32),
            ctypes.c_int,
            uint32,
            ctypes.c_int,
            uint32,
        ]
        self.iphlpapi.GetExtendedTcpTable.restype = uint32

    @staticmethod
    def _winerror() -> int:
        return ctypes.get_last_error() & MAX_METADATA_NUMBER

    def _pipe(self) -> tuple[Any, BinaryIO]:
        import msvcrt

        read_handle = ctypes.c_void_p()
        write_handle = ctypes.c_void_p()
        attrs = _WinSecurityAttributes(ctypes.sizeof(_WinSecurityAttributes), None, 1)
        if not self.kernel32.CreatePipe(ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attrs), 0):
            raise IsolatedServerFailure("process_create_failed", win32_code=self._winerror())
        if not self.kernel32.SetHandleInformation(read_handle, self.HANDLE_FLAG_INHERIT, 0):
            code = self._winerror()
            self.close_handle(read_handle)
            self.close_handle(write_handle)
            raise IsolatedServerFailure("process_create_failed", win32_code=code)
        try:
            fd = msvcrt.open_osfhandle(int(read_handle.value), os.O_RDONLY | os.O_BINARY)
            return write_handle, os.fdopen(fd, "rb", buffering=0)
        except (OSError, ValueError) as exc:
            self.close_handle(read_handle)
            self.close_handle(write_handle)
            raise IsolatedServerFailure("process_create_failed") from exc

    def create_suspended(self, executable: Path, environment: Mapping[str, str]) -> CreatedProcess:
        stdout_write, stdout_read = self._pipe()
        try:
            stderr_write, stderr_read = self._pipe()
        except BaseException:
            stdout_read.close()
            self.close_handle(stdout_write)
            raise
        startup = _WinStartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = self.STARTF_USESTDHANDLES
        startup.hStdInput = None
        startup.hStdOutput = stdout_write
        startup.hStdError = stderr_write
        proc = _WinProcessInformation()
        env_block = (
            "\x00".join(
                f"{key}={value}"
                for key, value in sorted(environment.items(), key=lambda item: item[0].casefold())
            )
            + "\x00\x00"
        )
        env_buffer = ctypes.create_unicode_buffer(env_block)
        command = ctypes.create_unicode_buffer(f'"{executable}" serve')
        ok = self.kernel32.CreateProcessW(
            str(executable), command, None, None, True,
            self.CREATE_SUSPENDED | self.CREATE_UNICODE_ENVIRONMENT,
            ctypes.cast(env_buffer, ctypes.c_void_p), str(executable.parent),
            ctypes.byref(startup), ctypes.byref(proc),
        )
        code = self._winerror() if not ok else 0
        self.close_handle(stdout_write)
        self.close_handle(stderr_write)
        if not ok:
            stdout_read.close()
            stderr_read.close()
            raise IsolatedServerFailure("process_create_failed", win32_code=code)
        return CreatedProcess(proc.dwProcessId, proc.hProcess, proc.hThread, stdout_read, stderr_read)

    def create_job(self) -> Any:
        handle = self.kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise IsolatedServerFailure("job_create_failed", win32_code=self._winerror())
        return handle

    def configure_kill_on_close(self, job: Any) -> None:
        info = _WinExtendedLimit()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(
            job,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise IsolatedServerFailure("job_limit_configuration_failed", win32_code=self._winerror())

    def assign_process(self, job: Any, process: CreatedProcess) -> None:
        if not self.kernel32.AssignProcessToJobObject(job, process.process_handle):
            raise IsolatedServerFailure("job_assignment_failed", win32_code=self._winerror())

    def verify_job_assignment(self, job: Any, process: CreatedProcess) -> bool:
        result = ctypes.c_int()
        if not self.kernel32.IsProcessInJob(process.process_handle, job, ctypes.byref(result)):
            raise IsolatedServerFailure("job_assignment_failed", win32_code=self._winerror())
        return bool(result.value)

    def resume_process(self, process: CreatedProcess) -> None:
        result = self.kernel32.ResumeThread(process.thread_handle)
        if result == 0xFFFFFFFF:
            raise IsolatedServerFailure("process_resume_failed", win32_code=self._winerror())

    def process_exit_code(self, process: CreatedProcess) -> int | None:
        code = ctypes.c_uint32()
        if not self.kernel32.GetExitCodeProcess(process.process_handle, ctypes.byref(code)):
            raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=self._winerror())
        return None if code.value == self.STILL_ACTIVE else int(code.value)

    def listener_owner(self, port: int) -> int | None:
        size = ctypes.c_uint32(0)
        result = self.iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, self.AF_INET, self.TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if result not in {self.ERROR_INSUFFICIENT_BUFFER, self.NO_ERROR}:
            raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=int(result))
        buffer = ctypes.create_string_buffer(size.value)
        result = self.iphlpapi.GetExtendedTcpTable(
            buffer, ctypes.byref(size), False, self.AF_INET, self.TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if result != self.NO_ERROR:
            raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=int(result))
        count = ctypes.c_uint32.from_buffer_copy(buffer.raw[:4]).value
        row_size = 24
        for index in range(count):
            offset = 4 + index * row_size
            row = (ctypes.c_uint32 * 6).from_buffer_copy(buffer.raw[offset : offset + row_size])
            local_port = socket.ntohs(int(row[2]) & 0xFFFF)
            if local_port == port:
                return int(row[5])
        return None

    def process_id_in_job(self, job: Any, process_id: int) -> bool:
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=self._winerror())
        result = ctypes.c_int()
        probe_error: IsolatedServerFailure | None = None
        try:
            if not self.kernel32.IsProcessInJob(handle, job, ctypes.byref(result)):
                probe_error = IsolatedServerFailure("ownership_probe_unavailable", win32_code=self._winerror())
        finally:
            if not self.kernel32.CloseHandle(handle) and probe_error is None:
                probe_error = IsolatedServerFailure("ownership_probe_unavailable", win32_code=self._winerror())
        if probe_error is not None:
            raise probe_error
        return bool(result.value)

    def terminate_job(self, job: Any) -> None:
        if not self.kernel32.TerminateJobObject(job, 0):
            raise IsolatedServerFailure("unclean_shutdown", win32_code=self._winerror())

    def terminate_process(self, process: CreatedProcess) -> None:
        if not self.kernel32.TerminateProcess(process.process_handle, 0):
            raise IsolatedServerFailure("unclean_shutdown", win32_code=self._winerror())

    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool:
        result = self.kernel32.WaitForSingleObject(process.process_handle, timeout_ms)
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        raise IsolatedServerFailure("unclean_shutdown", win32_code=self._winerror())

    def descendant_process_ids(self, process_id: int) -> set[int]:
        invalid = ctypes.c_void_p(-1).value
        snapshot = self.kernel32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if snapshot == invalid:
            raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=self._winerror())
        parents: dict[int, int] = {}
        entry = _WinProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            if not self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                code = self._winerror()
                if code != self.ERROR_NO_MORE_FILES:
                    raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=code)
                return set()
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not self.kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    code = self._winerror()
                    if code != self.ERROR_NO_MORE_FILES:
                        raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=code)
                    break
        finally:
            self.close_handle(snapshot)
        descendants: set[int] = set()
        changed = True
        while changed:
            changed = False
            for child, parent in parents.items():
                if child not in descendants and (parent == process_id or parent in descendants):
                    descendants.add(child)
                    changed = True
        return descendants

    def close_handle(self, handle: Any) -> None:
        if handle and not self.kernel32.CloseHandle(handle):
            raise IsolatedServerFailure("teardown_incomplete", win32_code=self._winerror())


def _default_command_probe(executable: Path) -> bytes:
    import subprocess

    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolatedServerFailure("executable_identity_unavailable") from exc
    if completed.returncode != 0:
        raise IsolatedServerFailure(
            "executable_identity_unavailable",
            exit_code=max(0, min(completed.returncode, MAX_METADATA_NUMBER)),
        )
    return completed.stdout


def _default_api_version(port: int, timeout: float) -> bytes:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/version", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - proved loopback owner first
        payload = json.loads(response.read(4096).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise ValueError("malformed version response")
    return payload["version"].encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _requested_settings(overrides: Mapping[str, str]) -> dict[str, Any]:
    return {
        "endpoint": {"loopback": True},
        "model_store": {"kind": "empty_temp"},
        "fixed": {
            "debug_log_requests": False,
            "no_cloud": True,
            "noprune": True,
            "num_parallel": 1,
            "max_loaded_models": 1,
            "max_queue": 1,
            "debug_level": 0,
        },
        "user_overrides": dict(overrides),
    }


def empty_attestation_artifact(overrides: Mapping[str, str] | None = None) -> dict[str, Any]:
    clean = validate_user_overrides(overrides)
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "artifact_kind": ATTESTATION_ARTIFACT_KIND,
        "captured_at": _utc_now(),
        "runtime_identity": {
            "executable_basename": "unavailable",
            "executable_sha256": "unavailable",
            "binary_version": "unavailable",
            "api_version": "unavailable",
            "startup_version": {"state": "unavailable", "value": None, "source": "unavailable"},
        },
        "requested_settings": _requested_settings(clean),
        "attested_settings": {key: _unattested_setting() for key in sorted(ATTESTED_SETTING_KEYS)},
        "endpoint_owner_verified": False,
        "job_assignment_verified": False,
        "logs": {"bytes_observed": 0, "bytes_captured": 0, "truncated": False},
        "readiness_duration_ms": None,
        "shutdown": {"method": "not_started", "duration_ms": None},
        "orphan_verification": "not_run",
        "port_closed": False,
        "temporary_space_torn_down": False,
        "failures": [],
        "overall_diagnostic_evidence_state": "failed",
    }


class IsolatedOllamaServer:
    """Own one empty-store attestation lifecycle."""

    def __init__(
        self,
        executable: str | Path,
        *,
        user_overrides: Mapping[str, str] | None = None,
        excluded_ports: set[int] | frozenset[int] = frozenset(),
        startup_timeout_seconds: float = 30.0,
        api: LifecycleApi | None = None,
        command_probe: Callable[[Path], str | bytes] = _default_command_probe,
        api_version_probe: Callable[[int, float], str | bytes] = _default_api_version,
        dialect: StartupDialect | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        temp_parent: Path | None = None,
    ) -> None:
        # Contract validation is intentionally first: no path resolution or
        # other observable operation precedes fixed-key rejection.
        self.user_overrides = validate_user_overrides(user_overrides)
        if (
            not isinstance(startup_timeout_seconds, (int, float))
            or isinstance(startup_timeout_seconds, bool)
            or not 0.1 <= startup_timeout_seconds <= 300
        ):
            raise Phase1ContractError("startup timeout must be between 0.1 and 300 seconds")
        if any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535 for p in excluded_ports):
            raise Phase1ContractError("excluded ports are invalid")
        self.executable_input = Path(executable)
        self.excluded_ports = frozenset(excluded_ports)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.api = api
        self.command_probe = command_probe
        self.api_version_probe = api_version_probe
        self.dialect = dialect
        self.clock = clock
        self.sleeper = sleeper
        self.temp_parent = temp_parent

    def run(self) -> dict[str, Any]:
        artifact = empty_attestation_artifact(self.user_overrides)
        space: SessionSpace | None = None
        process: CreatedProcess | None = None
        job: Any = None
        job_assigned = False
        log_capture = BoundedLogCapture()
        port: int | None = None
        api_raw: str | bytes | None = None
        reader_threads: list[threading.Thread] = []
        started = self.clock()
        shutdown_started: float | None = None
        lifecycle_api = self.api
        try:
            if os.name != "nt" and lifecycle_api is None:
                raise IsolatedServerFailure("platform_unsupported")
            executable = self._resolve_executable()
            binary = probe_binary_identity(executable, command_probe=self.command_probe)
            artifact["runtime_identity"].update(
                {
                    "executable_basename": binary.executable_basename,
                    "executable_sha256": binary.executable_sha256,
                    "binary_version": binary.binary_version,
                }
            )
            space = create_session_space(self.temp_parent)
            port = choose_loopback_port(set(self.excluded_ports))
            env = build_child_environment(
                executable=executable,
                space=space,
                port=port,
                user_overrides=self.user_overrides,
            )
            lifecycle_api = lifecycle_api or WindowsLifecycleApi()
            process = lifecycle_api.create_suspended(executable, env)
            job = lifecycle_api.create_job()
            lifecycle_api.configure_kill_on_close(job)
            lifecycle_api.assign_process(job, process)
            job_assigned = True
            if not lifecycle_api.verify_job_assignment(job, process):
                raise IsolatedServerFailure("job_assignment_failed")
            artifact["job_assignment_verified"] = True
            stdout_thread = threading.Thread(target=log_capture.drain, args=(process.stdout,), daemon=True)
            stderr_thread = threading.Thread(target=log_capture.drain, args=(process.stderr,), daemon=True)
            reader_threads = [stdout_thread, stderr_thread]
            stdout_thread.start()
            stderr_thread.start()
            lifecycle_api.resume_process(process)

            deadline = started + self.startup_timeout_seconds
            while self.clock() < deadline:
                if log_capture.reader_failed:
                    raise IsolatedServerFailure("log_reader_failed")
                if log_capture.truncated:
                    raise IsolatedServerFailure(
                        "startup_log_overflow",
                        bytes_observed=min(log_capture.observed, MAX_METADATA_NUMBER),
                    )
                exit_code = lifecycle_api.process_exit_code(process)
                if exit_code is not None:
                    raise IsolatedServerFailure(
                        "startup_process_exit", exit_code=max(0, min(exit_code, MAX_METADATA_NUMBER))
                    )
                owner = lifecycle_api.listener_owner(port)
                if owner is None:
                    self.sleeper(0.05)
                    continue
                if owner != process.process_id and not lifecycle_api.process_id_in_job(job, owner):
                    raise IsolatedServerFailure("port_hijacked")
                artifact["endpoint_owner_verified"] = True
                try:
                    api_raw = self.api_version_probe(port, min(1.0, max(0.1, deadline - self.clock())))
                except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                    self.sleeper(0.05)
                    continue
                artifact["readiness_duration_ms"] = min(int((self.clock() - started) * 1000), MAX_DURATION_MS)
                break
            else:
                raise IsolatedServerFailure("startup_timeout", timeout_ms=int(self.startup_timeout_seconds * 1000))
            if api_raw is None:
                raise IsolatedServerFailure("startup_timeout", timeout_ms=int(self.startup_timeout_seconds * 1000))

            # Give already-readable pipe bytes one scheduler turn to reach the
            # shared capture.  This does not wait for EOF; the child is alive.
            self.sleeper(0)
            raw_logs = log_capture.bytes()
            identity, attested = parse_startup_attestation(
                raw_logs,
                binary_identity=binary,
                api_version_raw=api_raw,
                dialect=self.dialect,
            )
            artifact["runtime_identity"] = identity
            artifact["attested_settings"] = attested
            artifact["failures"].extend(
                failure.as_record()
                for failure in compare_requested_attestation(self.user_overrides, attested)
            )
        except IsolatedServerFailure as exc:
            artifact["failures"].append(exc.as_record())
        finally:
            if process is not None and lifecycle_api is not None:
                shutdown_started = self.clock()
                method = "terminated"
                try:
                    if job is not None and job_assigned:
                        lifecycle_api.terminate_job(job)
                    else:
                        lifecycle_api.terminate_process(process)
                    if not lifecycle_api.wait_process(process, 5000):
                        method = "job_killed"
                        shutdown_failure = IsolatedServerFailure("unclean_shutdown", timeout_ms=5000)
                        artifact["failures"].append(shutdown_failure.as_record())
                    for reader in reader_threads:
                        reader.join(timeout=1.0)
                    if any(reader.is_alive() for reader in reader_threads) or log_capture.reader_failed:
                        artifact["failures"].append(IsolatedServerFailure("log_reader_failed").as_record())
                    artifact["shutdown"] = {
                        "method": method,
                        "duration_ms": min(int((self.clock() - shutdown_started) * 1000), MAX_DURATION_MS),
                    }
                    descendants = lifecycle_api.descendant_process_ids(process.process_id)
                    if descendants:
                        artifact["orphan_verification"] = "survivor_detected"
                        artifact["failures"].append(IsolatedServerFailure("orphaned_runner").as_record())
                    else:
                        artifact["orphan_verification"] = "clean"
                    if port is not None:
                        if lifecycle_api.listener_owner(port) is None:
                            artifact["port_closed"] = True
                        else:
                            artifact["failures"].append(IsolatedServerFailure("port_not_closed").as_record())
                except IsolatedServerFailure as exc:
                    artifact["failures"].append(exc.as_record())
                finally:
                    for stream in (process.stdout, process.stderr):
                        try:
                            stream.close()
                        except OSError:
                            artifact["failures"].append(IsolatedServerFailure("teardown_incomplete").as_record())
                    for handle in (process.thread_handle, process.process_handle, job):
                        if handle is not None:
                            try:
                                lifecycle_api.close_handle(handle)
                            except IsolatedServerFailure as exc:
                                artifact["failures"].append(exc.as_record())
            if space is not None:
                try:
                    teardown_session_space(space)
                    artifact["temporary_space_torn_down"] = True
                except IsolatedServerFailure as exc:
                    artifact["failures"].append(exc.as_record())

        artifact["logs"] = log_capture.evidence()
        artifact["failures"] = _deduplicate_failures(artifact["failures"])
        complete = (
            not artifact["failures"]
            and artifact["endpoint_owner_verified"]
            and artifact["job_assignment_verified"]
            and artifact["port_closed"]
            and artifact["temporary_space_torn_down"]
            and artifact["orphan_verification"] == "clean"
            and not artifact["logs"]["truncated"]
        )
        artifact["overall_diagnostic_evidence_state"] = (
            "complete" if complete else ("incomplete" if process is not None else "failed")
        )
        problems = validate_attestation_artifact(artifact)
        if problems:
            raise AssertionError(f"internal attestation artifact invalid: {problems}")
        return artifact

    def _resolve_executable(self) -> Path:
        raw = self.executable_input
        if raw.parent != Path(".") or raw.is_absolute():
            candidate = raw
        else:
            located = shutil.which(str(raw))
            if located is None:
                raise IsolatedServerFailure("executable_not_found")
            candidate = Path(located)
        try:
            return candidate.resolve(strict=True)
        except OSError as exc:
            raise IsolatedServerFailure("executable_not_found") from exc


def _deduplicate_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for failure in failures:
        category = failure["category"]
        if category not in seen:
            result.append(failure)
            seen.add(category)
    return result


_TOP_KEYS = frozenset(
    {
        "schema_version", "artifact_kind", "captured_at", "runtime_identity",
        "requested_settings", "attested_settings", "endpoint_owner_verified",
        "job_assignment_verified", "logs", "readiness_duration_ms", "shutdown",
        "orphan_verification", "port_closed", "temporary_space_torn_down",
        "failures", "overall_diagnostic_evidence_state",
    }
)
_RUNTIME_KEYS = frozenset(
    {"executable_basename", "executable_sha256", "binary_version", "api_version", "startup_version"}
)
_TYPED_VERSION_KEYS = frozenset({"state", "value", "source"})
_REQUESTED_KEYS = frozenset({"endpoint", "model_store", "fixed", "user_overrides"})
_FIXED_REQUEST_KEYS = frozenset(
    {
        "debug_log_requests",
        "no_cloud",
        "noprune",
        "num_parallel",
        "max_loaded_models",
        "max_queue",
        "debug_level",
    }
)
_SETTING_KEYS = frozenset({"state", "value", "source"})


def _closed(value: Any, keys: frozenset[str] | set[str], where: str, problems: list[str]) -> bool:
    if not isinstance(value, dict):
        problems.append(f"{where} must be an object")
        return False
    if set(value) != set(keys):
        problems.append(f"{where} must have exactly {sorted(keys)}")
        return False
    return True


def _bounded_duration(value: Any, where: str, problems: list[str], nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_DURATION_MS:
        problems.append(f"{where} must be bounded milliseconds")


def validate_attestation_artifact(artifact: Any) -> list[str]:
    """Validate the standalone closed Phase-1 artifact schema."""

    problems: list[str] = []
    if not _closed(artifact, _TOP_KEYS, "artifact", problems):
        return problems
    if artifact["schema_version"] != ATTESTATION_SCHEMA_VERSION:
        problems.append("unsupported attestation schema_version")
    if artifact["artifact_kind"] != ATTESTATION_ARTIFACT_KIND:
        problems.append("unsupported artifact_kind")
    if not isinstance(artifact["captured_at"], str) or not _ISO_UTC.fullmatch(artifact["captured_at"]):
        problems.append("captured_at must be a UTC timestamp")
    else:
        try:
            datetime.strptime(artifact["captured_at"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            problems.append("captured_at must be a valid UTC timestamp")
    runtime = artifact["runtime_identity"]
    if _closed(runtime, _RUNTIME_KEYS, "runtime_identity", problems):
        basename = runtime["executable_basename"]
        if basename != "unavailable" and (not isinstance(basename, str) or not _SAFE_BASENAME.fullmatch(basename)):
            problems.append("runtime executable basename is invalid")
        digest = runtime["executable_sha256"]
        if digest != "unavailable" and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            problems.append("runtime executable hash is invalid")
        for key in ("binary_version", "api_version"):
            value = runtime[key]
            if value != "unavailable":
                try:
                    if normalize_ollama_version(value) != value:
                        problems.append(f"runtime_identity.{key} is not normalized")
                except ValueError:
                    problems.append(f"runtime_identity.{key} is invalid")
        startup = runtime["startup_version"]
        if _closed(startup, _TYPED_VERSION_KEYS, "runtime_identity.startup_version", problems):
            allowed = {
                "attested": ("startup_log", str),
                "unattested": ("unattested", type(None)),
                "unavailable": ("unavailable", type(None)),
            }
            if startup["state"] not in allowed:
                problems.append("invalid startup version state")
            else:
                source, value_type = allowed[startup["state"]]
                if startup["source"] != source or not isinstance(startup["value"], value_type):
                    problems.append("startup version state is inconsistent")
                if startup["state"] == "attested":
                    try:
                        if normalize_ollama_version(startup["value"]) != startup["value"]:
                            problems.append("startup version is not normalized")
                    except ValueError:
                        problems.append("startup version is invalid")
        available_versions = [
            runtime[key]
            for key in ("binary_version", "api_version")
            if runtime[key] != "unavailable"
        ]
        if len(available_versions) == 2 and len(set(available_versions)) != 1:
            problems.append("binary and API versions must match")

    requested = artifact["requested_settings"]
    if _closed(requested, _REQUESTED_KEYS, "requested_settings", problems):
        if requested["endpoint"] != {"loopback": True}:
            problems.append("requested endpoint must be loopback marker")
        if requested["model_store"] != {"kind": "empty_temp"}:
            problems.append("requested model store must be empty_temp marker")
        fixed = requested["fixed"]
        if not _closed(fixed, _FIXED_REQUEST_KEYS, "requested_settings.fixed", problems) or fixed != {
            "debug_log_requests": False, "no_cloud": True, "noprune": True,
            "num_parallel": 1, "max_loaded_models": 1, "max_queue": 1, "debug_level": 0,
        }:
            problems.append("requested fixed settings are invalid")
        try:
            if validate_user_overrides(requested["user_overrides"]) != requested["user_overrides"]:
                problems.append("requested user overrides are not normalized")
        except Phase1ContractError as exc:
            problems.append(str(exc))

    settings = artifact["attested_settings"]
    if not _closed(settings, ATTESTED_SETTING_KEYS, "attested_settings", problems):
        pass
    else:
        for key, record in settings.items():
            if not _closed(record, _SETTING_KEYS, f"attested_settings.{key}", problems):
                continue
            if record["state"] == "unattested":
                if record != _unattested_setting():
                    problems.append(f"attested_settings.{key} unattested state is inconsistent")
            elif record["state"] == "attested":
                if (
                    record["source"] not in _SETTING_SOURCES
                    or not isinstance(record["value"], str)
                    or _normalize_attested_setting(key, record["value"]) != record["value"]
                ):
                    problems.append(f"attested_settings.{key} attested value is invalid")
            else:
                problems.append(f"attested_settings.{key} state is invalid")
    for key in ("endpoint_owner_verified", "job_assignment_verified", "port_closed", "temporary_space_torn_down"):
        if not isinstance(artifact[key], bool):
            problems.append(f"{key} must be boolean")
    logs = artifact["logs"]
    if _closed(logs, {"bytes_observed", "bytes_captured", "truncated"}, "logs", problems):
        for key in ("bytes_observed", "bytes_captured"):
            if (
                isinstance(logs[key], bool)
                or not isinstance(logs[key], int)
                or not 0 <= logs[key] <= MAX_METADATA_NUMBER
            ):
                problems.append(f"logs.{key} must be bounded")
        if not isinstance(logs["truncated"], bool):
            problems.append("logs.truncated must be boolean")
    _bounded_duration(artifact["readiness_duration_ms"], "readiness_duration_ms", problems, nullable=True)
    shutdown = artifact["shutdown"]
    if _closed(shutdown, {"method", "duration_ms"}, "shutdown", problems):
        if shutdown["method"] not in {"not_started", "terminated", "job_killed"}:
            problems.append("shutdown.method is invalid")
        _bounded_duration(shutdown["duration_ms"], "shutdown.duration_ms", problems, nullable=True)
    if artifact["orphan_verification"] not in {"not_run", "clean", "survivor_detected", "unavailable"}:
        problems.append("orphan_verification is invalid")
    failures = artifact["failures"]
    if not isinstance(failures, list) or len(failures) > len(PHASE1_FAILURE_CATEGORIES):
        problems.append("failures must be a bounded list")
    else:
        seen: set[str] = set()
        for index, failure in enumerate(failures):
            if not _closed(failure, {"category", "numeric_metadata"}, f"failures[{index}]", problems):
                continue
            category = failure["category"]
            if category not in PHASE1_FAILURE_CATEGORIES or category in seen:
                problems.append(f"failures[{index}] category is invalid or duplicated")
            seen.add(category)
            metadata = failure["numeric_metadata"]
            if not isinstance(metadata, dict) or set(metadata) - FAILURE_NUMERIC_METADATA_KEYS:
                problems.append(f"failures[{index}] metadata is not closed")
            elif any(
                isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_METADATA_NUMBER
                for v in metadata.values()
            ):
                problems.append(f"failures[{index}] metadata is out of bounds")
    if artifact["overall_diagnostic_evidence_state"] not in {"complete", "incomplete", "failed"}:
        problems.append("overall diagnostic evidence state is invalid")
    elif artifact["overall_diagnostic_evidence_state"] == "complete":
        if artifact["failures"]:
            problems.append("complete evidence cannot carry failures")
        if not all(
            artifact[key]
            for key in (
                "endpoint_owner_verified", "job_assignment_verified",
                "port_closed", "temporary_space_torn_down",
            )
        ):
            problems.append("complete evidence requires every lifecycle proof")
        if artifact["orphan_verification"] != "clean":
            problems.append("complete evidence requires clean orphan verification")
        shutdown_record = artifact["shutdown"] if isinstance(artifact["shutdown"], dict) else {}
        log_record = artifact["logs"] if isinstance(artifact["logs"], dict) else {}
        if artifact["readiness_duration_ms"] is None or shutdown_record.get("duration_ms") is None:
            problems.append("complete evidence requires readiness and shutdown durations")
        if log_record.get("truncated") is not False:
            problems.append("complete evidence cannot use truncated startup logs")
        if not isinstance(runtime, dict) or any(
            runtime.get(key) is None or runtime.get(key) == "unavailable"
            for key in ("executable_basename", "executable_sha256", "binary_version", "api_version")
        ):
            problems.append("complete evidence requires the full runtime identity")
        for key in ("noprune", "no_cloud"):
            record = settings.get(key, {}) if isinstance(settings, dict) else {}
            if record.get("state") != "attested" or str(record.get("value", "")).casefold() not in {"1", "true", "on"}:
                problems.append(f"complete evidence requires attested {key}")

    # Defense in depth: no durable key or string may contain the forbidden
    # identity-bearing surfaces.  SHA-256, normalized versions, timestamps,
    # enum values, and basenames all remain separator-free.
    forbidden_key_fragments = (
        "pid",
        "process_id",
        "port_number",
        "path",
        "handle",
        "command_line",
        "prompt",
        "generated_output",
        "raw_log",
        "raw_output",
    )

    def inspect(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                folded = str(key).casefold()
                if any(fragment in folded for fragment in forbidden_key_fragments):
                    problems.append(f"{where} contains forbidden key")
                inspect(child, f"{where}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{where}[{index}]")
        elif isinstance(value, str) and ("\\" in value or "/" in value or "\x00" in value):
            problems.append(f"{where} contains path-like or raw data")

    inspect(artifact, "artifact")
    return problems


def build_dry_run_plan(
    *,
    user_overrides: Mapping[str, str] | None = None,
    startup_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Return a closed, privacy-safe plan without probing or allocating."""

    overrides = validate_user_overrides(user_overrides)
    if (
        not isinstance(startup_timeout_seconds, (int, float))
        or isinstance(startup_timeout_seconds, bool)
        or not 0.1 <= startup_timeout_seconds <= 300
    ):
        raise Phase1ContractError("startup timeout must be between 0.1 and 300 seconds")
    return {
        "mode": "dry_run",
        "would_spawn": False,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "artifact_kind": ATTESTATION_ARTIFACT_KIND,
        "environment": {
            "construction": "from_empty",
            "fixed_internal_keys": sorted(FIXED_INTERNAL_ENV_KEYS),
            "user_override_keys": sorted(overrides),
        },
        "requested_settings": _requested_settings(overrides),
        "startup_timeout_ms": int(startup_timeout_seconds * 1000),
        "model_access": "none_empty_temp_store",
        "network_scope": "loopback_only",
    }
