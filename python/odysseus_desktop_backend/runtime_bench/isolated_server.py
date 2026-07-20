"""Phase-1 isolated Ollama server lifecycle and attestation.

Revision 6 architecture: command-version binding runs only after the
isolated server's endpoint is proven owned (post-readiness, S12); TCP
ownership is address-aware with a closed OWNED / NOT_PRESENT / FOREIGN /
AMBIGUOUS / UNCERTAIN result model and a stable held-handle foreign-owner
identity; and stdout/stderr are drained through benchmark-owned
overlapped named-pipe reads under absolute deadlines — no helper thread
exists anywhere in the I/O path and no unbounded wait exists anywhere in
the lifecycle.

This module is deliberately separate from performance artifact schemas.
It never reads a model store and constructs the child environment from an
empty mapping.  The real lifecycle is gated by the CLI; tests use the
injectable Windows API and HTTP seams below.
"""

from __future__ import annotations

import ctypes
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import struct
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol


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
        "attestation_dialect_unavailable",
        "temp_space_failed",
        "port_bind_failed",
        "process_create_failed",
        "process_attribute_list_failed",
        "process_attribute_list_cleanup_failed",
        "job_create_failed",
        "job_limit_configuration_failed",
        "job_assignment_failed",
        "process_resume_failed",
        "log_io_setup_failed",
        "ownership_probe_unavailable",
        "port_ownership_ambiguous",
        "owner_identity_unavailable",
        "owner_identity_changed",
        "port_hijacked",
        "startup_timeout",
        "startup_process_exit",
        "startup_log_overflow",
        "log_read_failed",
        "version_probe_timeout",
        "version_probe_output_overflow",
        "version_probe_failed",
        "version_output_malformed",
        "version_endpoint_ownership_failed",
        "version_probe_cleanup_failed",
        "io_cancellation_failed",
        "pending_io_cleanup_timeout",
        "model_residency_probe_failed",
        "unexpected_model_residency",
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
MAX_VERSION_OUTPUT_BYTES = 4 * 1024
MAX_MODEL_RESIDENCY_RESPONSE_BYTES = 8 * 1024
MAX_DIALECT_PATTERN_LENGTH = 512
DEFAULT_SERVER_LAUNCH_ATTEMPTS = 3
MODEL_RESIDENCY_TIMEOUT_SECONDS = 1.0
MAX_DURATION_MS = 24 * 60 * 60 * 1000
PIPE_READ_CHUNK = 8192
_WAIT_SLICE_MS = 50

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

# Buffers of overlapped operations that could not be cancelled or confirmed
# stay referenced for the remaining process lifetime so a late kernel
# completion can never write into reclaimed memory.  Entries are never
# removed and never reused.
_QUARANTINED_PENDING_IO: list[Any] = []


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
class RuntimeIdentity:
    """The §2.6 normalized post-readiness identity bind (memory-only paths)."""

    executable_basename: str
    executable_sha256: str
    client_version: str
    command_server_version: str
    api_version: str


def hash_executable(executable: Path) -> tuple[str, str]:
    """Return privacy-safe basename and SHA-256 for one executable."""
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
    except IsolatedServerFailure:
        raise
    except OSError as exc:
        raise IsolatedServerFailure("executable_identity_unavailable") from exc
    return basename, digest.hexdigest()


@dataclass(frozen=True)
class StartupDialect:
    """Fixture-reviewed regexes associated with an attested binary identity."""

    identity_sha256: str
    startup_version: re.Pattern[str] | None
    setting_patterns: Mapping[str, tuple[re.Pattern[str], str]]

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.identity_sha256):
            raise ValueError("dialect identity must be a SHA-256")
        if self.startup_version is not None:
            _validate_dialect_pattern(self.startup_version, "startup_version")
        if not isinstance(self.setting_patterns, Mapping):
            raise ValueError("dialect setting_patterns must be a mapping")
        names = set(self.setting_patterns)
        unknown = names - ATTESTED_SETTING_KEYS
        missing = MANDATORY_PHASE1_ATTESTATION_KEYS - names
        if unknown or missing:
            raise ValueError(
                f"dialect settings must be closed; unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
        closed_patterns: dict[str, tuple[re.Pattern[str], str]] = {}
        for name, definition in self.setting_patterns.items():
            if not isinstance(definition, tuple) or len(definition) != 2:
                raise ValueError(f"dialect setting {name} must be a pattern/source tuple")
            pattern, source = definition
            _validate_dialect_pattern(pattern, name)
            if source not in _SETTING_SOURCES:
                raise ValueError(f"dialect setting {name} has an invalid source")
            closed_patterns[name] = (pattern, source)
        object.__setattr__(self, "setting_patterns", MappingProxyType(closed_patterns))


@dataclass(frozen=True)
class VersionOutputDialect:
    """The closed §2.6/§2.7 model of one binary's ``--version`` output.

    Which lines exist, how the client and command-reported server versions
    are derived, and the exact connection-warning marker are properties of
    the installed binary; nothing here assumes a universal format.
    """

    identity_sha256: str
    server_version_pattern: re.Pattern[str]
    client_version_pattern: re.Pattern[str]
    connection_warning_marker: str
    lone_server_line_attests_client: bool

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.identity_sha256):
            raise ValueError("version-output dialect identity must be a SHA-256")
        _validate_dialect_pattern(self.server_version_pattern, "server_version")
        _validate_dialect_pattern(self.client_version_pattern, "client_version")
        if (
            not isinstance(self.connection_warning_marker, str)
            or not 1 <= len(self.connection_warning_marker) <= MAX_DIALECT_PATTERN_LENGTH
        ):
            raise ValueError("version-output dialect connection-warning marker is invalid")
        if not isinstance(self.lone_server_line_attests_client, bool):
            raise ValueError("version-output dialect client-derivation rule must be boolean")


@dataclass(frozen=True)
class ReviewedDialectEntry:
    """One registry entry: startup and version-output dialects together."""

    identity_sha256: str
    startup: StartupDialect
    version_output: VersionOutputDialect

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.identity_sha256):
            raise ValueError("dialect entry identity must be a SHA-256")
        if not isinstance(self.startup, StartupDialect) or self.startup.identity_sha256 != self.identity_sha256:
            raise ValueError("dialect entry startup identity does not match")
        if (
            not isinstance(self.version_output, VersionOutputDialect)
            or self.version_output.identity_sha256 != self.identity_sha256
        ):
            raise ValueError("dialect entry version-output identity does not match")


ATTESTED_SETTING_KEYS = frozenset(
    {"noprune", "no_cloud", "flash_attention", "kv_cache_type", "keep_alive", "context_length"}
)
MANDATORY_PHASE1_ATTESTATION_KEYS = frozenset({"noprune", "no_cloud"})
_SETTING_SOURCES = frozenset({"startup_log", "runner_log"})


def _validate_dialect_pattern(pattern: Any, where: str) -> None:
    if not isinstance(pattern, re.Pattern):
        raise ValueError(f"dialect {where} must use a compiled regular expression")
    if not 1 <= len(pattern.pattern) <= MAX_DIALECT_PATTERN_LENGTH:
        raise ValueError(f"dialect {where} pattern length is out of bounds")
    if pattern.groups != 1:
        raise ValueError(f"dialect {where} pattern must contain exactly one capture group")


# Intentionally empty in this correction cycle.  Registering an installed
# binary requires a separately reviewed committed entry carrying both the
# startup and version-output dialects; the CLI accepts no caller-supplied
# regex or fixture path.
REVIEWED_DIALECT_REGISTRY: Mapping[str, ReviewedDialectEntry] = MappingProxyType({})


def validate_dialect_registry(registry: Mapping[str, ReviewedDialectEntry]) -> None:
    if not isinstance(registry, Mapping):
        raise ValueError("dialect registry must be a mapping")
    for digest, entry in registry.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("dialect registry key must be a SHA-256")
        if not isinstance(entry, ReviewedDialectEntry) or entry.identity_sha256 != digest:
            raise ValueError("dialect registry entry does not match its SHA-256 key")


def reviewed_dialect_for_hash(digest: str) -> ReviewedDialectEntry:
    try:
        validate_dialect_registry(REVIEWED_DIALECT_REGISTRY)
    except ValueError as exc:
        raise IsolatedServerFailure("attestation_dialect_unavailable") from exc
    entry = REVIEWED_DIALECT_REGISTRY.get(digest)
    if entry is None:
        raise IsolatedServerFailure("attestation_dialect_unavailable")
    return entry


def parse_version_output(raw: bytes, dialect: VersionOutputDialect) -> tuple[str, str]:
    """Parse owned version-child output through the reviewed dialect.

    Returns ``(client version, command-reported server version)``, both
    normalized.  Fails closed per the §2.6 table: the connection-warning
    marker is ``version_endpoint_ownership_failed``; everything the dialect
    cannot fully account for is ``version_output_malformed``.
    """

    if not isinstance(raw, bytes) or len(raw) > MAX_VERSION_OUTPUT_BYTES:
        raise IsolatedServerFailure("version_output_malformed")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IsolatedServerFailure("version_output_malformed") from exc
    if dialect.connection_warning_marker in text:
        # The child could not reach the owned endpoint.
        raise IsolatedServerFailure("version_endpoint_ownership_failed")
    server_raw: str | None = None
    client_raw: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        server_match = dialect.server_version_pattern.fullmatch(stripped)
        client_match = dialect.client_version_pattern.fullmatch(stripped)
        if server_match is not None:
            if server_raw is not None:
                raise IsolatedServerFailure("version_output_malformed")
            server_raw = server_match.group(1)
        elif client_match is not None:
            if client_raw is not None:
                raise IsolatedServerFailure("version_output_malformed")
            client_raw = client_match.group(1)
        else:
            # An unexpected line is not parseable under the reviewed dialect.
            raise IsolatedServerFailure("version_output_malformed")
    if server_raw is None:
        raise IsolatedServerFailure("version_output_malformed")
    try:
        server_version = normalize_ollama_version(server_raw)
    except ValueError as exc:
        raise IsolatedServerFailure("version_output_malformed") from exc
    if client_raw is None:
        if not dialect.lone_server_line_attests_client:
            raise IsolatedServerFailure("version_output_malformed")
        # Reviewed dialect rule: a lone server line with no client warning
        # attests client = server.
        return server_version, server_version
    try:
        client_version = normalize_ollama_version(client_raw)
    except ValueError as exc:
        raise IsolatedServerFailure("version_output_malformed") from exc
    return client_version, server_version


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
    identity: RuntimeIdentity,
    dialect: StartupDialect,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Parse the optional startup version and typed settings in memory only.

    Returns the typed startup-version record and the attested settings.  A
    parseable startup version disagreeing with the bound identity is
    ``runtime_identity_mismatch``; an absent or unparseable startup version
    stays typed ``unattested``.
    """

    if dialect.identity_sha256 != identity.executable_sha256:
        raise IsolatedServerFailure("runtime_identity_mismatch")
    text = raw_capture.decode("utf-8", errors="replace")

    startup_state = "unattested"
    startup_value: str | None = None
    startup_source = "unattested"
    settings = {key: _unattested_setting() for key in sorted(ATTESTED_SETTING_KEYS)}

    if dialect.startup_version is not None:
        match = dialect.startup_version.search(text)
        if match is not None:
            try:
                startup_value = normalize_ollama_version(match.group(1))
            except (IndexError, ValueError) as exc:
                raise IsolatedServerFailure("runtime_identity_mismatch") from exc
            if startup_value != identity.api_version:
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

    startup_record = {
        "state": startup_state,
        "value": startup_value,
        "source": startup_source,
    }
    return startup_record, settings


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
    endpoint_port: int,
    user_overrides: Mapping[str, str] | None,
    parent_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the complete child environment from empty.

    ``endpoint_port`` is the port placed in ``OLLAMA_HOST``: the server
    child receives its own candidate port; the version child receives the
    already-proven owned server endpoint (§2.6) while keeping its own
    temporary profile and empty store.
    """

    overrides = validate_user_overrides(user_overrides)
    if isinstance(endpoint_port, bool) or not isinstance(endpoint_port, int) or not 1 <= endpoint_port <= 65535:
        raise Phase1ContractError("endpoint port is invalid")
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
        "OLLAMA_HOST": f"127.0.0.1:{endpoint_port}",
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
        candidate = 49152 + secrets.randbelow(65536 - 49152)
        if candidate in exclusions:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", candidate))
                host, port = probe.getsockname()
                if not ipaddress.ip_address(host).is_loopback:
                    continue
                if port not in exclusions:
                    return int(port)
        except OSError:
            continue
    raise IsolatedServerFailure("port_bind_failed", attempts=attempts)


class BoundedLogCapture:
    """First-64-KiB plus last-192-KiB memory-only capture (single-threaded)."""

    def __init__(self) -> None:
        self._first = bytearray()
        self._last = bytearray()
        self._observed = 0

    def feed(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("log chunks must be bytes")
        self._observed += len(data)
        remaining = FIRST_LOG_BYTES - len(self._first)
        if remaining > 0:
            self._first.extend(data[:remaining])
            data = data[remaining:]
        if data:
            self._last.extend(data)
            if len(self._last) > LAST_LOG_BYTES:
                del self._last[: len(self._last) - LAST_LOG_BYTES]

    @property
    def observed(self) -> int:
        return self._observed

    @property
    def truncated(self) -> bool:
        return self._observed > MAX_LOG_BYTES

    def bytes(self) -> bytes:
        return bytes(self._first + self._last)

    def evidence(self) -> dict[str, Any]:
        return {
            "bytes_observed": min(self._observed, MAX_METADATA_NUMBER),
            "bytes_captured": len(self._first) + len(self._last),
            "truncated": self._observed > MAX_LOG_BYTES,
        }


class BoundedVersionCapture:
    """Combined memory-only version output with a hard 4 KiB ceiling."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._observed = 0

    def feed(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("version output chunks must be bytes")
        self._observed += len(data)
        remaining = MAX_VERSION_OUTPUT_BYTES - len(self._data)
        if remaining > 0:
            self._data.extend(data[:remaining])

    @property
    def observed(self) -> int:
        return self._observed

    @property
    def overflowed(self) -> bool:
        return self._observed > MAX_VERSION_OUTPUT_BYTES

    def bytes(self) -> bytes:
        return bytes(self._data)


@dataclass(frozen=True)
class TcpListenerRow:
    """One IPv4 listener-table row: local address, local port, owner PID."""

    local_address: str
    local_port: int
    owner_process_id: int


OWNERSHIP_OWNED = "owned"
OWNERSHIP_NOT_PRESENT = "not_present"
OWNERSHIP_FOREIGN = "foreign"
OWNERSHIP_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class EndpointOwnership:
    """Closed §3.3 ownership result for one benchmark endpoint probe.

    The UNCERTAIN member of the closed model is represented by the probe
    raising ``ownership_probe_unavailable`` / ``owner_identity_unavailable``
    — uncertainty always fails closed and never yields a usable result.
    """

    state: str
    foreign_owner_process_id: int | None = None


def classify_relevant_listener_rows(
    rows: tuple[TcpListenerRow, ...] | list[TcpListenerRow], port: int
) -> tuple[TcpListenerRow, ...]:
    """Return every relevant row for ``127.0.0.1:<port>`` (§3.3).

    Exact-loopback and wildcard rows are both relevant and never collapsed;
    same-port rows on other concrete interfaces are never relevant and never
    mistaken for the benchmark endpoint.
    """

    return tuple(
        row
        for row in rows
        if row.local_port == port and row.local_address in ("127.0.0.1", "0.0.0.0")
    )


def resolve_endpoint_ownership(
    api: "LifecycleApi",
    port: int,
    *,
    process: "CreatedProcess | None",
    job: Any,
) -> EndpointOwnership:
    """Resolve the closed ownership state from all relevant listener rows."""

    rows = api.tcp_listener_rows()
    relevant = classify_relevant_listener_rows(rows, port)
    if not relevant:
        return EndpointOwnership(OWNERSHIP_NOT_PRESENT)
    if len(relevant) > 1:
        return EndpointOwnership(OWNERSHIP_AMBIGUOUS)
    row = relevant[0]
    if process is not None and row.owner_process_id == process.process_id:
        return EndpointOwnership(OWNERSHIP_OWNED)
    if job is not None and api.process_id_in_job(job, row.owner_process_id):
        return EndpointOwnership(OWNERSHIP_OWNED)
    return EndpointOwnership(OWNERSHIP_FOREIGN, foreign_owner_process_id=row.owner_process_id)


@dataclass(eq=False)
class ForeignOwnerIdentity:
    """Stable foreign-owner identity: the (PID, creation time) pair pinned
    by a held checked handle.  Memory-only; never persisted."""

    process_id: int
    creation_time: int
    handle: Any
    handle_closed: bool = False


def capture_foreign_owner_identity(api: "LifecycleApi", process_id: int) -> ForeignOwnerIdentity:
    handle = api.open_limited_process_handle(process_id)
    try:
        creation_time = api.process_creation_time(handle)
    except IsolatedServerFailure:
        try:
            api.close_handle(handle)
        except IsolatedServerFailure:
            pass
        raise
    return ForeignOwnerIdentity(process_id, creation_time, handle)


@dataclass(eq=False)
class CreatedProcess:
    process_id: int
    process_handle: Any
    thread_handle: Any
    stdout: Any
    stderr: Any


class LifecycleApi(Protocol):
    def create_suspended(
        self, executable: Path, arguments: tuple[str, ...], environment: Mapping[str, str]
    ) -> CreatedProcess: ...
    def create_job(self) -> Any: ...
    def configure_kill_on_close(self, job: Any) -> None: ...
    def assign_process(self, job: Any, process: CreatedProcess) -> None: ...
    def verify_job_assignment(self, job: Any, process: CreatedProcess) -> bool: ...
    def process_image_matches(self, process: CreatedProcess, executable: Path) -> bool: ...
    def resume_process(self, process: CreatedProcess) -> None: ...
    def process_exit_code(self, process: CreatedProcess) -> int | None: ...
    def tcp_listener_rows(self) -> tuple[TcpListenerRow, ...]: ...
    def process_id_in_job(self, job: Any, process_id: int) -> bool: ...
    def open_limited_process_handle(self, process_id: int) -> Any: ...
    def process_creation_time(self, handle: Any) -> int: ...
    def query_pid_creation_time(self, process_id: int) -> int: ...
    def handle_signaled(self, handle: Any) -> bool: ...
    def terminate_job(self, job: Any) -> None: ...
    def terminate_process(self, process: CreatedProcess) -> None: ...
    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool: ...
    def descendant_process_ids(self, process_id: int) -> set[int]: ...
    def post_overlapped_read(self, pipe: Any) -> None: ...
    def finish_overlapped_read(self, pipe: Any) -> tuple[str, bytes]: ...
    def cancel_overlapped_read(self, pipe: Any) -> None: ...
    def wait_for_completion(
        self, pipes: tuple[Any, ...], process: CreatedProcess | None, timeout_ms: int
    ) -> None: ...
    def close_pipe(self, pipe: Any) -> None: ...
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


class _WinStartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _WinStartupInfo), ("lpAttributeList", ctypes.c_void_p)]


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


class _WinOverlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


class _WinFileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


@dataclass
class _ProcessAttributeList:
    buffer: Any
    pointer: ctypes.c_void_p
    inherited_handles: Any
    deleted: bool = False


@dataclass(eq=False)
class _OverlappedPipe:
    """Parent side of one benchmark-owned overlapped named pipe.

    The pipe name is random, memory-only, and never persisted.  The
    OVERLAPPED block and read buffer stay referenced for the life of this
    object so a late completion can never touch freed memory."""

    handle: Any
    event: Any
    overlapped: Any
    buffer: Any
    pending: bool = False
    eof: bool = False


class WindowsLifecycleApi:
    """Checked Windows API operations used by the real gated path."""

    CREATE_SUSPENDED = 0x00000004
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    STARTF_USESTDHANDLES = 0x00000100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    STILL_ACTIVE = 259
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    WAIT_FAILED = 0xFFFFFFFF
    TH32CS_SNAPPROCESS = 0x00000002
    ERROR_NO_MORE_FILES = 18
    ERROR_INSUFFICIENT_BUFFER = 122
    NO_ERROR = 0
    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    PIPE_ACCESS_INBOUND = 0x00000001
    FILE_FLAG_OVERLAPPED = 0x40000000
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    ERROR_IO_PENDING = 997
    ERROR_IO_INCOMPLETE = 996
    ERROR_BROKEN_PIPE = 109
    ERROR_HANDLE_EOF = 38
    ERROR_OPERATION_ABORTED = 995
    ERROR_PIPE_CONNECTED = 535
    ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if os.name != "nt":
            raise IsolatedServerFailure("platform_unsupported")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        void_p = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        self.kernel32.CreateNamedPipeW.argtypes = [
            ctypes.c_wchar_p, uint32, uint32, uint32, uint32, uint32, uint32,
            ctypes.POINTER(_WinSecurityAttributes),
        ]
        self.kernel32.CreateNamedPipeW.restype = void_p
        self.kernel32.ConnectNamedPipe.argtypes = [void_p, void_p]
        self.kernel32.ConnectNamedPipe.restype = ctypes.c_int
        self.kernel32.CreateEventW.argtypes = [
            ctypes.POINTER(_WinSecurityAttributes), ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p
        ]
        self.kernel32.CreateEventW.restype = void_p
        self.kernel32.ResetEvent.argtypes = [void_p]
        self.kernel32.ResetEvent.restype = ctypes.c_int
        self.kernel32.ReadFile.argtypes = [void_p, void_p, uint32, ctypes.POINTER(uint32), void_p]
        self.kernel32.ReadFile.restype = ctypes.c_int
        self.kernel32.GetOverlappedResult.argtypes = [void_p, void_p, ctypes.POINTER(uint32), ctypes.c_int]
        self.kernel32.GetOverlappedResult.restype = ctypes.c_int
        self.kernel32.CancelIoEx.argtypes = [void_p, void_p]
        self.kernel32.CancelIoEx.restype = ctypes.c_int
        self.kernel32.WaitForMultipleObjects.argtypes = [uint32, ctypes.POINTER(void_p), ctypes.c_int, uint32]
        self.kernel32.WaitForMultipleObjects.restype = uint32
        self.kernel32.GetProcessTimes.argtypes = [
            void_p,
            ctypes.POINTER(_WinFileTime),
            ctypes.POINTER(_WinFileTime),
            ctypes.POINTER(_WinFileTime),
            ctypes.POINTER(_WinFileTime),
        ]
        self.kernel32.GetProcessTimes.restype = ctypes.c_int
        self.kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            uint32,
            uint32,
            ctypes.POINTER(_WinSecurityAttributes),
            uint32,
            uint32,
            void_p,
        ]
        self.kernel32.CreateFileW.restype = void_p
        self.kernel32.CreateProcessW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, void_p, void_p, ctypes.c_int,
            uint32, void_p, ctypes.c_wchar_p, void_p,
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
        self.kernel32.InitializeProcThreadAttributeList.argtypes = [
            void_p,
            uint32,
            uint32,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.kernel32.InitializeProcThreadAttributeList.restype = ctypes.c_int
        self.kernel32.UpdateProcThreadAttribute.argtypes = [
            void_p,
            uint32,
            ctypes.c_size_t,
            void_p,
            ctypes.c_size_t,
            void_p,
            void_p,
        ]
        self.kernel32.UpdateProcThreadAttribute.restype = ctypes.c_int
        self.kernel32.DeleteProcThreadAttributeList.argtypes = [void_p]
        self.kernel32.DeleteProcThreadAttributeList.restype = None
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            void_p,
            uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(uint32),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
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

    def _create_overlapped_pipe(self) -> tuple[_OverlappedPipe, Any]:
        """Create one single-instance overlapped inbound named pipe.

        Returns the parent-side pipe state and the inheritable child write
        handle.  The random pipe name is memory-only and never persisted."""

        name = "\\\\.\\pipe\\odysseus-bench-" + secrets.token_hex(16)
        server = self.kernel32.CreateNamedPipeW(
            name,
            self.PIPE_ACCESS_INBOUND | self.FILE_FLAG_OVERLAPPED | self.FILE_FLAG_FIRST_PIPE_INSTANCE,
            self.PIPE_TYPE_BYTE | self.PIPE_READMODE_BYTE | self.PIPE_WAIT | self.PIPE_REJECT_REMOTE_CLIENTS,
            1,
            65536,
            65536,
            0,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if not server or server == invalid:
            raise IsolatedServerFailure("log_io_setup_failed", win32_code=self._winerror())
        event = self.kernel32.CreateEventW(None, True, False, None)
        if not event:
            code = self._winerror()
            self.kernel32.CloseHandle(server)
            raise IsolatedServerFailure("log_io_setup_failed", win32_code=code)
        attrs = _WinSecurityAttributes(ctypes.sizeof(_WinSecurityAttributes), None, 1)
        client = self.kernel32.CreateFileW(
            name,
            self.GENERIC_WRITE,
            0,
            ctypes.byref(attrs),
            self.OPEN_EXISTING,
            self.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not client or client == invalid:
            code = self._winerror()
            self.kernel32.CloseHandle(event)
            self.kernel32.CloseHandle(server)
            raise IsolatedServerFailure("log_io_setup_failed", win32_code=code)
        if not self.kernel32.ConnectNamedPipe(server, None):
            code = self._winerror()
            if code != self.ERROR_PIPE_CONNECTED:
                self.kernel32.CloseHandle(client)
                self.kernel32.CloseHandle(event)
                self.kernel32.CloseHandle(server)
                raise IsolatedServerFailure("log_io_setup_failed", win32_code=code)
        overlapped = _WinOverlapped()
        overlapped.hEvent = event
        pipe = _OverlappedPipe(
            handle=server,
            event=event,
            overlapped=overlapped,
            buffer=ctypes.create_string_buffer(PIPE_READ_CHUNK),
        )
        return pipe, client

    def post_overlapped_read(self, pipe: _OverlappedPipe) -> None:
        if pipe.eof or pipe.pending:
            return
        if not self.kernel32.ResetEvent(pipe.event):
            raise IsolatedServerFailure("log_io_setup_failed", win32_code=self._winerror())
        ok = self.kernel32.ReadFile(
            pipe.handle, pipe.buffer, PIPE_READ_CHUNK, None, ctypes.byref(pipe.overlapped)
        )
        if ok:
            pipe.pending = True
            return
        code = self._winerror()
        if code == self.ERROR_IO_PENDING:
            pipe.pending = True
            return
        if code == self.ERROR_BROKEN_PIPE:
            pipe.eof = True
            return
        raise IsolatedServerFailure("log_io_setup_failed", win32_code=code)

    def finish_overlapped_read(self, pipe: _OverlappedPipe) -> tuple[str, bytes]:
        if pipe.eof:
            return ("eof", b"")
        if not pipe.pending:
            return ("idle", b"")
        transferred = ctypes.c_uint32(0)
        ok = self.kernel32.GetOverlappedResult(
            pipe.handle, ctypes.byref(pipe.overlapped), ctypes.byref(transferred), False
        )
        if ok:
            pipe.pending = False
            return ("data", bytes(pipe.buffer.raw[: transferred.value]))
        code = self._winerror()
        if code == self.ERROR_IO_INCOMPLETE:
            return ("pending", b"")
        if code in (self.ERROR_BROKEN_PIPE, self.ERROR_HANDLE_EOF):
            pipe.pending = False
            pipe.eof = True
            return ("eof", b"")
        if code == self.ERROR_OPERATION_ABORTED:
            pipe.pending = False
            return ("aborted", b"")
        pipe.pending = False
        raise IsolatedServerFailure("log_read_failed", win32_code=code)

    def cancel_overlapped_read(self, pipe: _OverlappedPipe) -> None:
        if not pipe.pending:
            return
        # Checked CancelIoEx against exactly this pipe handle and operation.
        if not self.kernel32.CancelIoEx(pipe.handle, ctypes.byref(pipe.overlapped)):
            code = self._winerror()
            if code == self.ERROR_NOT_FOUND:
                return
            raise IsolatedServerFailure("io_cancellation_failed", win32_code=code)

    def wait_for_completion(
        self, pipes: tuple[Any, ...], process: CreatedProcess | None, timeout_ms: int
    ) -> None:
        handles: list[Any] = [pipe.event for pipe in pipes if pipe is not None and pipe.pending]
        if process is not None:
            handles.append(process.process_handle)
        timeout = max(0, min(int(timeout_ms), 0x7FFFFFFE))
        if not handles:
            time.sleep(min(timeout, _WAIT_SLICE_MS) / 1000.0)
            return
        array = (ctypes.c_void_p * len(handles))(*handles)
        result = self.kernel32.WaitForMultipleObjects(len(handles), array, False, timeout)
        if result == self.WAIT_FAILED:
            raise IsolatedServerFailure("log_read_failed", win32_code=self._winerror())

    def close_pipe(self, pipe: _OverlappedPipe) -> None:
        failure: IsolatedServerFailure | None = None
        for handle in (pipe.event, pipe.handle):
            if handle and not self.kernel32.CloseHandle(handle):
                failure = IsolatedServerFailure("teardown_incomplete", win32_code=self._winerror())
        pipe.event = None
        pipe.handle = None
        if failure is not None:
            raise failure

    def _open_null_stdin(self) -> Any:
        attrs = _WinSecurityAttributes(ctypes.sizeof(_WinSecurityAttributes), None, 1)
        handle = self.kernel32.CreateFileW(
            "NUL",
            self.GENERIC_READ,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE,
            ctypes.byref(attrs),
            self.OPEN_EXISTING,
            self.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise IsolatedServerFailure("process_create_failed", win32_code=self._winerror())
        return handle

    def _create_attribute_list(
        self, stdin_read: Any, stdout_write: Any, stderr_write: Any
    ) -> _ProcessAttributeList:
        size = ctypes.c_size_t(0)
        first = self.kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        first_error = self._winerror()
        if first or first_error != self.ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            raise IsolatedServerFailure("process_attribute_list_failed", win32_code=first_error)
        buffer = ctypes.create_string_buffer(size.value)
        pointer = ctypes.cast(buffer, ctypes.c_void_p)
        if not self.kernel32.InitializeProcThreadAttributeList(pointer, 1, 0, ctypes.byref(size)):
            raise IsolatedServerFailure("process_attribute_list_failed", win32_code=self._winerror())
        handles = (ctypes.c_void_p * 3)(stdin_read, stdout_write, stderr_write)
        attributes = _ProcessAttributeList(buffer, pointer, handles)
        if not self.kernel32.UpdateProcThreadAttribute(
            pointer,
            0,
            self.PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.cast(handles, ctypes.c_void_p),
            ctypes.sizeof(handles),
            None,
            None,
        ):
            code = self._winerror()
            self._delete_attribute_list(attributes)
            raise IsolatedServerFailure("process_attribute_list_failed", win32_code=code)
        return attributes

    def _delete_attribute_list(self, attributes: _ProcessAttributeList) -> None:
        if attributes.deleted:
            return
        try:
            # DeleteProcThreadAttributeList is a checked cleanup call with a
            # void Windows API return; successful invocation is its only
            # observable result.
            self.kernel32.DeleteProcThreadAttributeList(attributes.pointer)
        except (OSError, ValueError) as exc:
            raise IsolatedServerFailure("process_attribute_list_cleanup_failed") from exc
        attributes.deleted = True

    def create_suspended(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> CreatedProcess:
        import subprocess

        if any(not isinstance(argument, str) or "\x00" in argument for argument in arguments):
            raise IsolatedServerFailure("process_create_failed")
        stdin_read = self._open_null_stdin()
        stdout_pipe: _OverlappedPipe | None = None
        stderr_pipe: _OverlappedPipe | None = None
        stdout_write: Any = None
        try:
            stdout_pipe, stdout_write = self._create_overlapped_pipe()
            stderr_pipe, stderr_write = self._create_overlapped_pipe()
        except BaseException:
            if stdout_pipe is not None:
                try:
                    self.close_pipe(stdout_pipe)
                except IsolatedServerFailure:
                    pass
            if stdout_write:
                self.kernel32.CloseHandle(stdout_write)
            self.kernel32.CloseHandle(stdin_read)
            raise
        attributes: _ProcessAttributeList | None = None
        startup = _WinStartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = self.STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin_read
        startup.StartupInfo.hStdOutput = stdout_write
        startup.StartupInfo.hStdError = stderr_write
        proc = _WinProcessInformation()
        env_block = (
            "\x00".join(
                f"{key}={value}"
                for key, value in sorted(environment.items(), key=lambda item: item[0].casefold())
            )
            + "\x00\x00"
        )
        env_buffer = ctypes.create_unicode_buffer(env_block)
        command = ctypes.create_unicode_buffer(
            subprocess.list2cmdline((str(executable), *arguments))
        )
        ok = False
        code = 0
        creation_failure: IsolatedServerFailure | None = None
        cleanup_failure = False
        try:
            attributes = self._create_attribute_list(stdin_read, stdout_write, stderr_write)
            startup.lpAttributeList = attributes.pointer
            ok = self.kernel32.CreateProcessW(
                str(executable),
                command,
                None,
                None,
                True,
                self.CREATE_SUSPENDED
                | self.CREATE_UNICODE_ENVIRONMENT
                | self.EXTENDED_STARTUPINFO_PRESENT,
                ctypes.cast(env_buffer, ctypes.c_void_p),
                str(executable.parent),
                ctypes.byref(startup),
                ctypes.byref(proc),
            )
            code = self._winerror() if not ok else 0
        except IsolatedServerFailure as exc:
            creation_failure = exc
        except (OSError, ValueError) as exc:
            creation_failure = IsolatedServerFailure("process_create_failed")
            creation_failure.__cause__ = exc
        finally:
            if attributes is not None:
                try:
                    self._delete_attribute_list(attributes)
                except IsolatedServerFailure:
                    cleanup_failure = True
            for child_handle in (stdin_read, stdout_write, stderr_write):
                try:
                    self.close_handle(child_handle)
                except IsolatedServerFailure:
                    cleanup_failure = True

        def _close_parent_pipes() -> None:
            for parent_pipe in (stdout_pipe, stderr_pipe):
                if parent_pipe is not None:
                    try:
                        self.close_pipe(parent_pipe)
                    except IsolatedServerFailure:
                        pass

        if cleanup_failure:
            try:
                if ok:
                    self._abort_created_suspended(proc)
            finally:
                _close_parent_pipes()
            raise IsolatedServerFailure("process_attribute_list_cleanup_failed")
        if creation_failure is not None:
            _close_parent_pipes()
            raise creation_failure
        if not ok:
            _close_parent_pipes()
            raise IsolatedServerFailure("process_create_failed", win32_code=code)
        return CreatedProcess(proc.dwProcessId, proc.hProcess, proc.hThread, stdout_pipe, stderr_pipe)

    def _abort_created_suspended(self, process: _WinProcessInformation) -> None:
        failed = False
        if not self.kernel32.TerminateProcess(process.hProcess, 1):
            failed = True
        wait_result = self.kernel32.WaitForSingleObject(process.hProcess, 5000)
        if wait_result != self.WAIT_OBJECT_0:
            failed = True
        for handle in (process.hThread, process.hProcess):
            if handle and not self.kernel32.CloseHandle(handle):
                failed = True
        if failed:
            raise IsolatedServerFailure("process_attribute_list_cleanup_failed")

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

    def process_image_matches(self, process: CreatedProcess, executable: Path) -> bool:
        capacity = 32768
        buffer = ctypes.create_unicode_buffer(capacity)
        size = ctypes.c_uint32(capacity)
        if not self.kernel32.QueryFullProcessImageNameW(
            process.process_handle, 0, buffer, ctypes.byref(size)
        ):
            raise IsolatedServerFailure(
                "executable_identity_unavailable", win32_code=self._winerror()
            )
        try:
            observed = Path(buffer.value).resolve(strict=False)
            expected = executable.resolve(strict=True)
        except OSError as exc:
            raise IsolatedServerFailure("executable_identity_unavailable") from exc
        return os.path.normcase(str(observed)) == os.path.normcase(str(expected))

    def resume_process(self, process: CreatedProcess) -> None:
        result = self.kernel32.ResumeThread(process.thread_handle)
        if result == 0xFFFFFFFF:
            raise IsolatedServerFailure("process_resume_failed", win32_code=self._winerror())

    def process_exit_code(self, process: CreatedProcess) -> int | None:
        code = ctypes.c_uint32()
        if not self.kernel32.GetExitCodeProcess(process.process_handle, ctypes.byref(code)):
            raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=self._winerror())
        return None if code.value == self.STILL_ACTIVE else int(code.value)

    def tcp_listener_rows(self) -> tuple[TcpListenerRow, ...]:
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
        rows: list[TcpListenerRow] = []
        for index in range(count):
            offset = 4 + index * row_size
            row = (ctypes.c_uint32 * 6).from_buffer_copy(buffer.raw[offset : offset + row_size])
            # dwLocalAddr holds the in_addr bytes in network order; a native
            # little-endian read reversed them, so pack little-endian to
            # recover the original byte sequence for inet_ntoa.
            local_address = socket.inet_ntoa(struct.pack("<I", int(row[1])))
            local_port = socket.ntohs(int(row[2]) & 0xFFFF)
            rows.append(TcpListenerRow(local_address, local_port, int(row[5])))
        return tuple(rows)

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

    def open_limited_process_handle(self, process_id: int) -> Any:
        handle = self.kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE, False, process_id
        )
        if not handle:
            raise IsolatedServerFailure("owner_identity_unavailable", win32_code=self._winerror())
        return handle

    def process_creation_time(self, handle: Any) -> int:
        creation = _WinFileTime()
        exit_time = _WinFileTime()
        kernel_time = _WinFileTime()
        user_time = _WinFileTime()
        if not self.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise IsolatedServerFailure("owner_identity_unavailable", win32_code=self._winerror())
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)

    def query_pid_creation_time(self, process_id: int) -> int:
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            raise IsolatedServerFailure("owner_identity_unavailable", win32_code=self._winerror())
        failure: IsolatedServerFailure | None = None
        creation_time = 0
        try:
            creation_time = self.process_creation_time(handle)
        except IsolatedServerFailure as exc:
            failure = exc
        finally:
            if not self.kernel32.CloseHandle(handle) and failure is None:
                failure = IsolatedServerFailure("owner_identity_unavailable", win32_code=self._winerror())
        if failure is not None:
            raise failure
        return creation_time

    def handle_signaled(self, handle: Any) -> bool:
        result = self.kernel32.WaitForSingleObject(handle, 0)
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        raise IsolatedServerFailure("owner_identity_unavailable", win32_code=self._winerror())

    def terminate_job(self, job: Any) -> None:
        if not self.kernel32.TerminateJobObject(job, 0):
            raise IsolatedServerFailure("unclean_shutdown", win32_code=self._winerror())

    def terminate_process(self, process: CreatedProcess) -> None:
        if not self.kernel32.TerminateProcess(process.process_handle, 0):
            raise IsolatedServerFailure("unclean_shutdown", win32_code=self._winerror())

    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool:
        result = self.kernel32.WaitForSingleObject(process.process_handle, max(0, int(timeout_ms)))
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


def verify_suspended_executable(
    api: LifecycleApi,
    process: CreatedProcess,
    executable: Path,
    expected_basename: str,
    expected_sha256: str,
) -> None:
    """Close both the path-query and pre-hash/CreateProcess identity windows."""

    if not api.process_image_matches(process, executable):
        raise IsolatedServerFailure("runtime_identity_mismatch")
    basename, digest = hash_executable(executable)
    if basename != expected_basename or digest != expected_sha256:
        raise IsolatedServerFailure("runtime_identity_mismatch")


class OverlappedLogPump:
    """Single-lifecycle-thread drain of one child's overlapped pipes.

    No Python helper thread exists here or anywhere in the I/O path; every
    wait is bounded by the caller-supplied timeout derived from an absolute
    deadline."""

    def __init__(self, api: LifecycleApi, process: CreatedProcess, capture: Any) -> None:
        self.api = api
        self.process = process
        self.capture = capture
        self.pipes: tuple[Any, ...] = (process.stdout, process.stderr)
        self._finished: set[int] = set()

    def post_initial(self) -> None:
        for pipe in self.pipes:
            self.api.post_overlapped_read(pipe)

    def service(self) -> None:
        """Confirm completed reads, feed the bounded capture, repost."""

        for index, pipe in enumerate(self.pipes):
            if index in self._finished:
                continue
            while True:
                status, data = self.api.finish_overlapped_read(pipe)
                if status == "data":
                    if data:
                        self.capture.feed(data)
                    try:
                        self.api.post_overlapped_read(pipe)
                    except IsolatedServerFailure as exc:
                        if exc.category == "log_io_setup_failed":
                            wrapped = IsolatedServerFailure(
                                "log_read_failed", **exc.numeric_metadata
                            )
                            wrapped.__cause__ = exc
                            raise wrapped
                        raise
                    continue
                if status in ("eof", "aborted"):
                    self._finished.add(index)
                break

    @property
    def all_finished(self) -> bool:
        return len(self._finished) == len(self.pipes)

    def wait(self, timeout_ms: int) -> None:
        pending = tuple(
            pipe for index, pipe in enumerate(self.pipes) if index not in self._finished
        )
        self.api.wait_for_completion(pending, self.process, max(0, int(timeout_ms)))


def cancel_pending_pipe_io(
    api: LifecycleApi,
    pipes: tuple[Any, ...],
    *,
    deadline: float,
    clock: Callable[[], float],
    capture: Any = None,
) -> list[IsolatedServerFailure]:
    """The §5 cancellation sequence, steps 2–5, under the cleanup deadline.

    Both failure modes are fixed cleanup failures: the attempt is never
    retried, evidence is never declared complete, and affected buffers stay
    referenced (quarantined) for the remaining process lifetime."""

    failures: list[IsolatedServerFailure] = []
    for pipe in pipes:
        if pipe is None:
            continue
        # Step 2: drain any already-completed reads (checked results).
        status = "idle"
        try:
            while True:
                status, data = api.finish_overlapped_read(pipe)
                if status == "data":
                    if capture is not None and data:
                        capture.feed(data)
                    continue
                break
        except IsolatedServerFailure as exc:
            # The operation completed with an unrecoverable error: it is
            # finished, so the handle can still be closed below.
            failures.append(exc)
            status = "eof"
        if status == "pending":
            # Step 3: checked CancelIoEx against exactly this operation.
            try:
                api.cancel_overlapped_read(pipe)
            except IsolatedServerFailure as exc:
                failures.append(exc)
                _QUARANTINED_PENDING_IO.append(pipe)
                continue
            # Step 4: bounded confirmation via the operation's event.
            confirmed = False
            while True:
                try:
                    status, data = api.finish_overlapped_read(pipe)
                except IsolatedServerFailure as exc:
                    failures.append(exc)
                    confirmed = True
                    break
                if status == "data":
                    if capture is not None and data:
                        capture.feed(data)
                    continue
                if status in ("eof", "aborted", "idle"):
                    confirmed = True
                    break
                remaining_ms = int((deadline - clock()) * 1000)
                if remaining_ms <= 0:
                    failures.append(IsolatedServerFailure("pending_io_cleanup_timeout"))
                    _QUARANTINED_PENDING_IO.append(pipe)
                    break
                api.wait_for_completion((pipe,), None, min(_WAIT_SLICE_MS, remaining_ms))
            if not confirmed:
                continue
        # Step 5: close pipe and event handles with checked results.
        try:
            api.close_pipe(pipe)
        except IsolatedServerFailure as exc:
            failures.append(exc)
    return failures


_VERSION_CHILD_PASSTHROUGH = frozenset(
    {
        "version_probe_timeout",
        "version_probe_output_overflow",
        "version_probe_failed",
        "runtime_identity_mismatch",
    }
)


def _as_version_probe_failure(exc: IsolatedServerFailure) -> IsolatedServerFailure:
    if exc.category in _VERSION_CHILD_PASSTHROUGH:
        return exc
    wrapped = IsolatedServerFailure("version_probe_failed", **exc.numeric_metadata)
    wrapped.__cause__ = exc
    return wrapped


def run_owned_version_probe(
    *,
    executable: Path,
    environment: Mapping[str, str],
    api: LifecycleApi,
    expected_basename: str,
    expected_sha256: str,
    timeout_seconds: float,
    cleanup_timeout_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """Run ``--version`` suspended, job-owned, overlapped-drained, bounded.

    The version child keeps every isolation property of the server child
    (§2.6): suspended-image verification and re-hash, its own verified
    kill-on-close job, bounded overlapped I/O with no reader threads, an
    absolute deadline, and complete verified cleanup.  The caller supplies
    the environment (with ``OLLAMA_HOST`` bound to the already-proven owned
    endpoint) and owns the child's temporary space."""

    process: CreatedProcess | None = None
    job: Any = None
    job_assigned = False
    capture = BoundedVersionCapture()
    primary_failure: IsolatedServerFailure | None = None
    cleanup_failed = False
    try:
        try:
            process = api.create_suspended(executable, ("--version",), environment)
            verify_suspended_executable(
                api, process, executable, expected_basename, expected_sha256
            )
            job = api.create_job()
            api.configure_kill_on_close(job)
            api.assign_process(job, process)
            job_assigned = True
            if not api.verify_job_assignment(job, process):
                raise IsolatedServerFailure("job_assignment_failed")
            pump = OverlappedLogPump(api, process, capture)
            pump.post_initial()
        except IsolatedServerFailure as exc:
            raise _as_version_probe_failure(exc)
        deadline = clock() + timeout_seconds
        try:
            api.resume_process(process)
        except IsolatedServerFailure as exc:
            raise _as_version_probe_failure(exc)
        exited = False
        while True:
            try:
                pump.service()
            except IsolatedServerFailure as exc:
                raise _as_version_probe_failure(exc)
            if capture.overflowed:
                raise IsolatedServerFailure(
                    "version_probe_output_overflow",
                    bytes_observed=min(capture.observed, MAX_METADATA_NUMBER),
                )
            if not exited:
                try:
                    exit_code = api.process_exit_code(process)
                except IsolatedServerFailure as exc:
                    raise _as_version_probe_failure(exc)
                if exit_code is not None:
                    exited = True
                    if exit_code != 0:
                        raise IsolatedServerFailure(
                            "version_probe_failed",
                            exit_code=max(0, min(exit_code, MAX_METADATA_NUMBER)),
                        )
            if exited and pump.all_finished:
                break
            remaining = deadline - clock()
            if remaining <= 0:
                raise IsolatedServerFailure(
                    "version_probe_timeout", timeout_ms=int(timeout_seconds * 1000)
                )
            pump.wait(int(min(_WAIT_SLICE_MS, remaining * 1000)))
    except IsolatedServerFailure as exc:
        primary_failure = exc
    finally:
        if process is not None:
            cleanup_deadline = clock() + cleanup_timeout_seconds
            try:
                if job is not None and job_assigned:
                    api.terminate_job(job)
                else:
                    api.terminate_process(process)
                wait_ms = int(max(0.0, cleanup_deadline - clock()) * 1000)
                if not api.wait_process(process, min(5000, wait_ms)):
                    cleanup_failed = True
            except IsolatedServerFailure:
                cleanup_failed = True
            if cancel_pending_pipe_io(
                api,
                (process.stdout, process.stderr),
                deadline=cleanup_deadline,
                clock=clock,
                capture=capture,
            ):
                cleanup_failed = True
            try:
                if api.descendant_process_ids(process.process_id):
                    cleanup_failed = True
            except IsolatedServerFailure:
                cleanup_failed = True
            for handle in (process.thread_handle, process.process_handle, job):
                if handle is not None:
                    try:
                        api.close_handle(handle)
                    except IsolatedServerFailure:
                        cleanup_failed = True
    if cleanup_failed:
        raise IsolatedServerFailure("version_probe_cleanup_failed")
    if primary_failure is not None:
        raise primary_failure
    if capture.overflowed:
        raise IsolatedServerFailure(
            "version_probe_output_overflow",
            bytes_observed=min(capture.observed, MAX_METADATA_NUMBER),
        )
    raw_output = capture.bytes()
    if not raw_output:
        raise IsolatedServerFailure("version_probe_failed")
    return raw_output


def _default_api_version(port: int, timeout: float) -> bytes:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/version", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - proved loopback owner first
        payload = json.loads(response.read(4096).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise ValueError("malformed version response")
    return payload["version"].encode("utf-8")


def _default_model_residency(port: int, timeout: float) -> bytes:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/ps", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - owner reverified first
        payload = response.read(MAX_MODEL_RESIDENCY_RESPONSE_BYTES + 1)
    return payload


def verify_empty_model_residency(raw: str | bytes) -> None:
    """Accept only a bounded, closed ``{"models": []}`` response."""

    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes):
        raise IsolatedServerFailure("model_residency_probe_failed")
    if len(raw) > MAX_MODEL_RESIDENCY_RESPONSE_BYTES:
        raise IsolatedServerFailure(
            "model_residency_probe_failed",
            bytes_observed=min(len(raw), MAX_METADATA_NUMBER),
        )
    try:
        text = raw.decode("utf-8", errors="strict")
        pairs = json.loads(text, object_pairs_hook=lambda value: value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsolatedServerFailure("model_residency_probe_failed") from exc
    if (
        not isinstance(pairs, list)
        or any(not isinstance(pair, tuple) or len(pair) != 2 for pair in pairs)
        or [key for key, _ in pairs] != ["models"]
    ):
        raise IsolatedServerFailure("model_residency_probe_failed")
    models = pairs[0][1]
    if not isinstance(models, list):
        raise IsolatedServerFailure("model_residency_probe_failed")
    if models:
        raise IsolatedServerFailure("unexpected_model_residency")


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
            "client_version": "unavailable",
            "command_server_version": "unavailable",
            "api_version": "unavailable",
            "startup_version": {"state": "unavailable", "value": None, "source": "unavailable"},
        },
        "requested_settings": _requested_settings(clean),
        "attested_settings": {key: _unattested_setting() for key in sorted(ATTESTED_SETTING_KEYS)},
        "endpoint_owner_verified": False,
        "job_assignment_verified": False,
        "model_residency_verified_empty": False,
        "logs": {"bytes_observed": 0, "bytes_captured": 0, "truncated": False},
        "readiness_duration_ms": None,
        "shutdown": {"method": "not_started", "duration_ms": None},
        "orphan_verification": "not_run",
        "port_closed": False,
        "temporary_space_torn_down": False,
        "failures": [],
        "overall_diagnostic_evidence_state": "failed",
    }


class _CandidatePortRace(RuntimeError):
    """Internal retry signal carrying the stable captured foreign identity."""

    def __init__(self, identity: ForeignOwnerIdentity) -> None:
        super().__init__("candidate port race")
        self.identity = identity


@dataclass
class _ServerAttempt:
    space: SessionSpace
    port: int
    capture: BoundedLogCapture
    process: CreatedProcess | None = None
    job: Any = None
    job_assigned: bool = False
    pump: OverlappedLogPump | None = None
    foreign_identity: ForeignOwnerIdentity | None = None


@dataclass
class _CleanupEvidence:
    failures: list[dict[str, Any]]
    shutdown: dict[str, Any]
    orphan_verification: str
    port_closed: bool
    temporary_space_torn_down: bool


def _required_attestation_keys(overrides: Mapping[str, str]) -> frozenset[str]:
    mapping = {
        "OLLAMA_FLASH_ATTENTION": "flash_attention",
        "OLLAMA_KV_CACHE_TYPE": "kv_cache_type",
        "OLLAMA_KEEP_ALIVE": "keep_alive",
        "OLLAMA_CONTEXT_LENGTH": "context_length",
    }
    return frozenset(MANDATORY_PHASE1_ATTESTATION_KEYS | {mapping[key] for key in overrides})


class IsolatedOllamaServer:
    """Own one empty-store attestation lifecycle (§12 state machine)."""

    def __init__(
        self,
        executable: str | Path,
        *,
        user_overrides: Mapping[str, str] | None = None,
        excluded_ports: set[int] | frozenset[int] = frozenset(),
        startup_timeout_seconds: float = 30.0,
        attestation_timeout_seconds: float = 10.0,
        version_timeout_seconds: float = 10.0,
        cleanup_timeout_seconds: float = 10.0,
        launch_attempts: int = DEFAULT_SERVER_LAUNCH_ATTEMPTS,
        api: LifecycleApi | None = None,
        api_version_probe: Callable[[int, float], str | bytes] = _default_api_version,
        model_residency_probe: Callable[[int, float], str | bytes] = _default_model_residency,
        clock: Callable[[], float] = time.monotonic,
        temp_parent: Path | None = None,
    ) -> None:
        # Contract validation (S0) is intentionally first: no path resolution
        # or other observable operation precedes fixed-key rejection.
        self.user_overrides = validate_user_overrides(user_overrides)
        self.startup_timeout_seconds = self._timeout(startup_timeout_seconds, "startup")
        self.attestation_timeout_seconds = self._timeout(
            attestation_timeout_seconds, "attestation"
        )
        self.version_timeout_seconds = self._timeout(version_timeout_seconds, "version")
        self.cleanup_timeout_seconds = self._timeout(cleanup_timeout_seconds, "cleanup")
        if any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in excluded_ports
        ):
            raise Phase1ContractError("excluded ports are invalid")
        if (
            isinstance(launch_attempts, bool)
            or not isinstance(launch_attempts, int)
            or not 1 <= launch_attempts <= 8
        ):
            raise Phase1ContractError("launch attempts must be between 1 and 8")
        self.executable_input = Path(executable)
        self.excluded_ports = frozenset(excluded_ports)
        self.launch_attempts = launch_attempts
        self.api = api
        self.api_version_probe = api_version_probe
        self.model_residency_probe = model_residency_probe
        self.clock = clock
        self.temp_parent = temp_parent

    @staticmethod
    def _timeout(value: float, name: str) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0.1 <= value <= 300
        ):
            raise Phase1ContractError(f"{name} timeout must be between 0.1 and 300 seconds")
        return float(value)

    def run(self) -> dict[str, Any]:
        artifact = empty_attestation_artifact(self.user_overrides)
        lifecycle_api = self.api
        server_process_created = False
        last_capture = BoundedLogCapture()
        try:
            if os.name != "nt" and lifecycle_api is None:
                raise IsolatedServerFailure("platform_unsupported")
            lifecycle_api = lifecycle_api or WindowsLifecycleApi()
            # S1: executable resolution, basename, SHA-256.
            executable = self._resolve_executable()
            basename, digest = hash_executable(executable)
            artifact["runtime_identity"].update(
                {"executable_basename": basename, "executable_sha256": digest}
            )
            # S2: closed-registry dialect lookup (startup + version-output).
            dialect_entry = reviewed_dialect_for_hash(digest)
            required_keys = _required_attestation_keys(self.user_overrides)
            if not required_keys <= set(dialect_entry.startup.setting_patterns):
                raise IsolatedServerFailure("attestation_dialect_unavailable")

            exclusions = set(self.excluded_ports)
            for attempt_index in range(1, self.launch_attempts + 1):
                # S3 (session space) + S4 (candidate endpoint selection).
                attempt = self._new_attempt(exclusions)
                last_capture = attempt.capture
                race: ForeignOwnerIdentity | None = None
                primary_failure: IsolatedServerFailure | None = None
                try:
                    # S5–S8: suspended creation, revalidation, job, log I/O.
                    self._create_owned_server(
                        attempt, lifecycle_api, executable, basename, digest
                    )
                    server_process_created = True
                    artifact["job_assignment_verified"] = True
                    # S9–S11: resume, address-aware readiness, /api/version.
                    api_version, readiness_ms = self._wait_for_readiness(
                        attempt, lifecycle_api
                    )
                    artifact["endpoint_owner_verified"] = True
                    artifact["readiness_duration_ms"] = readiness_ms
                    # S12: post-readiness command-version binding.
                    identity = self._bind_command_version(
                        attempt, lifecycle_api, executable, basename, digest,
                        dialect_entry, api_version,
                    )
                    artifact["runtime_identity"].update(
                        {
                            "client_version": identity.client_version,
                            "command_server_version": identity.command_server_version,
                            "api_version": identity.api_version,
                        }
                    )
                    # S13: mandatory startup attestation.
                    startup_record, attested, attestation_failure = (
                        self._wait_for_attestation(
                            attempt, lifecycle_api, identity,
                            dialect_entry.startup, required_keys,
                        )
                    )
                    artifact["runtime_identity"]["startup_version"] = startup_record
                    artifact["attested_settings"] = attested
                    if attestation_failure is not None:
                        raise attestation_failure
                    mismatches = compare_requested_attestation(self.user_overrides, attested)
                    if mismatches:
                        raise mismatches[0]
                    # S14–S15: ownership revalidation, bounded /api/ps.
                    self._verify_empty_model_residency(attempt, lifecycle_api)
                    artifact["model_residency_verified_empty"] = True
                except _CandidatePortRace as exc:
                    race = exc.identity
                except IsolatedServerFailure as exc:
                    primary_failure = exc

                # S16–S20: the mandatory cleanup chain, always executed.
                cleanup = self._cleanup_attempt(attempt, lifecycle_api, race=race)
                self._apply_cleanup(artifact, cleanup)
                retry_allowed = (
                    race is not None
                    and primary_failure is None
                    and not cleanup.failures
                    and cleanup.port_closed
                    and cleanup.orphan_verification == "clean"
                    and cleanup.temporary_space_torn_down
                )
                if retry_allowed:
                    # The single S10 → S4 backward edge: a proved race with
                    # every benchmark-owned resource verifiably gone.
                    exclusions.add(attempt.port)
                    if attempt_index < self.launch_attempts:
                        continue
                    artifact["failures"].append(
                        IsolatedServerFailure(
                            "port_bind_failed", attempts=attempt_index
                        ).as_record()
                    )
                    break
                if primary_failure is not None:
                    artifact["failures"].append(primary_failure.as_record())
                artifact["failures"].extend(cleanup.failures)
                break
        except IsolatedServerFailure as exc:
            artifact["failures"].append(exc.as_record())

        artifact["logs"] = last_capture.evidence()
        artifact["failures"] = _deduplicate_failures(artifact["failures"])
        complete = (
            not artifact["failures"]
            and artifact["endpoint_owner_verified"]
            and artifact["job_assignment_verified"]
            and artifact["model_residency_verified_empty"]
            and artifact["port_closed"]
            and artifact["temporary_space_torn_down"]
            and artifact["orphan_verification"] == "clean"
            and not artifact["logs"]["truncated"]
        )
        artifact["overall_diagnostic_evidence_state"] = (
            "complete"
            if complete
            else ("incomplete" if server_process_created else "failed")
        )
        # S21: closed-schema artifact validation.
        problems = validate_attestation_artifact(artifact)
        if problems:
            raise AssertionError(f"internal attestation artifact invalid: {problems}")
        return artifact

    def _new_attempt(self, exclusions: set[int]) -> _ServerAttempt:
        space = create_session_space(self.temp_parent)
        try:
            port = choose_loopback_port(exclusions)
        except BaseException:
            teardown_session_space(space)
            raise
        return _ServerAttempt(space=space, port=port, capture=BoundedLogCapture())

    def _create_owned_server(
        self,
        attempt: _ServerAttempt,
        api: LifecycleApi,
        executable: Path,
        basename: str,
        digest: str,
    ) -> None:
        environment = build_child_environment(
            executable=executable,
            space=attempt.space,
            endpoint_port=attempt.port,
            user_overrides=self.user_overrides,
        )
        # S5: suspended creation with overlapped pipes and NUL stdin.
        attempt.process = api.create_suspended(executable, ("serve",), environment)
        # S6: suspended-image verification and executable re-hash.
        verify_suspended_executable(api, attempt.process, executable, basename, digest)
        # S7: verified kill-on-close job assignment.
        attempt.job = api.create_job()
        api.configure_kill_on_close(attempt.job)
        api.assign_process(attempt.job, attempt.process)
        attempt.job_assigned = True
        if not api.verify_job_assignment(attempt.job, attempt.process):
            raise IsolatedServerFailure("job_assignment_failed")
        # S8: post the initial overlapped reads on both pipes.
        attempt.pump = OverlappedLogPump(api, attempt.process, attempt.capture)
        attempt.pump.post_initial()

    def _check_capture(self, attempt: _ServerAttempt) -> None:
        if attempt.capture.truncated:
            raise IsolatedServerFailure(
                "startup_log_overflow",
                bytes_observed=min(attempt.capture.observed, MAX_METADATA_NUMBER),
            )

    def _wait_for_readiness(
        self, attempt: _ServerAttempt, api: LifecycleApi
    ) -> tuple[str, int]:
        """S9–S11: resume, address-aware ownership readiness, /api/version.

        Returns the normalized API version and the readiness duration."""

        assert attempt.process is not None and attempt.pump is not None
        started = self.clock()
        deadline = started + self.startup_timeout_seconds
        api.resume_process(attempt.process)
        while True:
            attempt.pump.service()
            self._check_capture(attempt)
            ownership = resolve_endpoint_ownership(
                api, attempt.port, process=attempt.process, job=attempt.job
            )
            exit_code = api.process_exit_code(attempt.process)
            if ownership.state == OWNERSHIP_OWNED:
                # S11: an HTTP request is sent only from the OWNED state,
                # verified immediately above, within the readiness deadline.
                try:
                    api_raw = self.api_version_probe(
                        attempt.port,
                        min(1.0, max(0.1, deadline - self.clock())),
                    )
                except (urllib.error.URLError, OSError):
                    api_raw = None
                except (ValueError, json.JSONDecodeError) as exc:
                    raise IsolatedServerFailure("runtime_identity_mismatch") from exc
                if api_raw is not None:
                    try:
                        normalized = normalize_ollama_version(api_raw)
                    except ValueError as exc:
                        raise IsolatedServerFailure("runtime_identity_mismatch") from exc
                    duration = min(
                        int((self.clock() - started) * 1000), MAX_DURATION_MS
                    )
                    return normalized, duration
            elif ownership.state == OWNERSHIP_FOREIGN:
                assert ownership.foreign_owner_process_id is not None
                identity = capture_foreign_owner_identity(
                    api, ownership.foreign_owner_process_id
                )
                attempt.foreign_identity = identity
                if exit_code is not None or self._child_exited_promptly(attempt, api):
                    raise _CandidatePortRace(identity)
                raise IsolatedServerFailure("port_hijacked")
            elif ownership.state == OWNERSHIP_AMBIGUOUS:
                raise IsolatedServerFailure("port_ownership_ambiguous")
            else:
                if exit_code is not None:
                    raise IsolatedServerFailure(
                        "startup_process_exit",
                        exit_code=max(0, min(exit_code, MAX_METADATA_NUMBER)),
                    )
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise IsolatedServerFailure(
                    "startup_timeout",
                    timeout_ms=int(self.startup_timeout_seconds * 1000),
                )
            attempt.pump.wait(int(min(_WAIT_SLICE_MS, remaining * 1000)))

    def _child_exited_promptly(self, attempt: _ServerAttempt, api: LifecycleApi) -> bool:
        # A foreign listener alone is a hijack.  It becomes a proven bind
        # race only if our suspended-owned child promptly exits while that
        # listener holds the formerly free candidate.
        assert attempt.process is not None
        for _ in range(5):
            if api.process_exit_code(attempt.process) is not None:
                return True
            api.wait_for_completion((), attempt.process, 20)
        return False

    def _bind_command_version(
        self,
        attempt: _ServerAttempt,
        api: LifecycleApi,
        executable: Path,
        basename: str,
        digest: str,
        dialect_entry: ReviewedDialectEntry,
        api_version: str,
    ) -> RuntimeIdentity:
        """S12: the §2.6 post-readiness command-version identity bind."""

        assert attempt.process is not None
        # Ownership recheck immediately before the version child starts.
        try:
            ownership = resolve_endpoint_ownership(
                api, attempt.port, process=attempt.process, job=attempt.job
            )
        except IsolatedServerFailure as exc:
            failure = IsolatedServerFailure(
                "version_endpoint_ownership_failed", **exc.numeric_metadata
            )
            failure.__cause__ = exc
            raise failure
        if ownership.state != OWNERSHIP_OWNED:
            raise IsolatedServerFailure("version_endpoint_ownership_failed")
        # The version child's own temporary home and empty model store.
        try:
            space = create_session_space(self.temp_parent)
        except IsolatedServerFailure as exc:
            raise _as_version_probe_failure(exc)
        raw: bytes | None = None
        primary: IsolatedServerFailure | None = None
        try:
            environment = build_child_environment(
                executable=executable,
                space=space,
                endpoint_port=attempt.port,
                user_overrides=self.user_overrides,
            )
            raw = run_owned_version_probe(
                executable=executable,
                environment=environment,
                api=api,
                expected_basename=basename,
                expected_sha256=digest,
                timeout_seconds=self.version_timeout_seconds,
                cleanup_timeout_seconds=self.cleanup_timeout_seconds,
                clock=self.clock,
            )
        except IsolatedServerFailure as exc:
            primary = exc
        try:
            teardown_session_space(space)
        except IsolatedServerFailure as exc:
            failure = IsolatedServerFailure("version_probe_cleanup_failed")
            failure.__cause__ = exc
            raise failure
        if primary is not None:
            raise primary
        assert raw is not None
        client_version, command_server_version = parse_version_output(
            raw, dialect_entry.version_output
        )
        if client_version != api_version or command_server_version != api_version:
            raise IsolatedServerFailure("runtime_identity_mismatch")
        return RuntimeIdentity(
            executable_basename=basename,
            executable_sha256=digest,
            client_version=client_version,
            command_server_version=command_server_version,
            api_version=api_version,
        )

    def _wait_for_attestation(
        self,
        attempt: _ServerAttempt,
        api: LifecycleApi,
        identity: RuntimeIdentity,
        dialect: StartupDialect,
        required_keys: frozenset[str],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], IsolatedServerFailure | None]:
        """S13: drain logs under the separate absolute attestation clock."""

        assert attempt.process is not None and attempt.pump is not None
        deadline = self.clock() + self.attestation_timeout_seconds
        while True:
            attempt.pump.service()
            self._check_capture(attempt)
            exit_code = api.process_exit_code(attempt.process)
            if exit_code is not None:
                raise IsolatedServerFailure(
                    "startup_process_exit",
                    exit_code=max(0, min(exit_code, MAX_METADATA_NUMBER)),
                )
            startup_record, attested = parse_startup_attestation(
                attempt.capture.bytes(), identity=identity, dialect=dialect
            )
            if all(attested[key]["state"] == "attested" for key in required_keys):
                return startup_record, attested, None
            remaining = deadline - self.clock()
            if remaining <= 0:
                return (
                    startup_record,
                    attested,
                    IsolatedServerFailure(
                        "attestation_missing",
                        timeout_ms=int(self.attestation_timeout_seconds * 1000),
                    ),
                )
            attempt.pump.wait(int(min(_WAIT_SLICE_MS, remaining * 1000)))

    def _verify_empty_model_residency(
        self, attempt: _ServerAttempt, api: LifecycleApi
    ) -> None:
        """S14 (ownership revalidation) + S15 (bounded owned /api/ps)."""

        assert attempt.process is not None
        self._check_capture(attempt)
        exit_code = api.process_exit_code(attempt.process)
        if exit_code is not None:
            raise IsolatedServerFailure(
                "startup_process_exit",
                exit_code=max(0, min(exit_code, MAX_METADATA_NUMBER)),
            )
        ownership = resolve_endpoint_ownership(
            api, attempt.port, process=attempt.process, job=attempt.job
        )
        if ownership.state == OWNERSHIP_AMBIGUOUS:
            raise IsolatedServerFailure("port_ownership_ambiguous")
        if ownership.state == OWNERSHIP_FOREIGN:
            raise IsolatedServerFailure("port_hijacked")
        if ownership.state != OWNERSHIP_OWNED:
            raise IsolatedServerFailure("ownership_probe_unavailable")
        try:
            raw = self.model_residency_probe(
                attempt.port, MODEL_RESIDENCY_TIMEOUT_SECONDS
            )
        except (
            OSError,
            ValueError,
            TypeError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            raise IsolatedServerFailure("model_residency_probe_failed") from exc
        verify_empty_model_residency(raw)

    def _cleanup_attempt(
        self,
        attempt: _ServerAttempt,
        api: LifecycleApi,
        *,
        race: ForeignOwnerIdentity | None = None,
    ) -> _CleanupEvidence:
        """S16–S20: shutdown, pending-I/O cancellation, orphan verification,
        address-aware endpoint closure with race-identity comparison, and
        temporary-space teardown — all bounded by the absolute cleanup
        deadline computed once here."""

        failures: list[dict[str, Any]] = []
        shutdown = {"method": "not_started", "duration_ms": None}
        orphan = "not_run"
        port_closed = False
        temp_clean = False
        cleanup_deadline = self.clock() + self.cleanup_timeout_seconds
        if attempt.process is not None:
            started = self.clock()
            method = "terminated"
            # S16: terminate the job; wait bounded on the process handle.
            try:
                if attempt.job is not None and attempt.job_assigned:
                    api.terminate_job(attempt.job)
                else:
                    api.terminate_process(attempt.process)
                wait_ms = int(max(0.0, cleanup_deadline - self.clock()) * 1000)
                if not api.wait_process(attempt.process, min(5000, wait_ms)):
                    method = "job_killed"
                    failures.append(
                        IsolatedServerFailure("unclean_shutdown", timeout_ms=5000).as_record()
                    )
            except IsolatedServerFailure as exc:
                failures.append(exc.as_record())
            # S17: the §5 cancellation sequence for both pipes.
            for io_failure in cancel_pending_pipe_io(
                api,
                (attempt.process.stdout, attempt.process.stderr),
                deadline=cleanup_deadline,
                clock=self.clock,
                capture=attempt.capture,
            ):
                failures.append(io_failure.as_record())
            shutdown = {
                "method": method,
                "duration_ms": min(int((self.clock() - started) * 1000), MAX_DURATION_MS),
            }
            # S18: orphan verification (read-only snapshot, parentage-based).
            try:
                descendants = api.descendant_process_ids(attempt.process.process_id)
                if descendants:
                    orphan = "survivor_detected"
                    failures.append(IsolatedServerFailure("orphaned_runner").as_record())
                else:
                    orphan = "clean"
            except IsolatedServerFailure as exc:
                orphan = "unavailable"
                failures.append(exc.as_record())
            # S19: address-aware endpoint closure and identity comparison.
            port_closed = self._verify_endpoint_closure(attempt, api, race, failures)
            self._close_identity_handle(attempt, api, failures)
            for handle in (
                attempt.process.thread_handle,
                attempt.process.process_handle,
                attempt.job,
            ):
                if handle is not None:
                    try:
                        api.close_handle(handle)
                    except IsolatedServerFailure as exc:
                        failures.append(exc.as_record())
        else:
            port_closed = self._verify_endpoint_closure(attempt, api, race, failures)
            self._close_identity_handle(attempt, api, failures)
        # S20: temporary-space teardown.
        try:
            teardown_session_space(attempt.space)
            temp_clean = True
        except IsolatedServerFailure as exc:
            failures.append(exc.as_record())
        return _CleanupEvidence(
            _deduplicate_failures(failures), shutdown, orphan, port_closed, temp_clean
        )

    def _verify_endpoint_closure(
        self,
        attempt: _ServerAttempt,
        api: LifecycleApi,
        race: ForeignOwnerIdentity | None,
        failures: list[dict[str, Any]],
    ) -> bool:
        """S19 classification: NOT_PRESENT required; the only tolerated
        non-empty result is the unchanged proven foreign owner."""

        identity = attempt.foreign_identity
        try:
            ownership = resolve_endpoint_ownership(
                api, attempt.port, process=attempt.process, job=attempt.job
            )
        except IsolatedServerFailure as exc:
            failures.append(exc.as_record())
            return False
        if ownership.state == OWNERSHIP_NOT_PRESENT:
            if race is not None:
                # The raced owner vanished: its identity can no longer be
                # proven unchanged, so the attempt fails closed, no retry.
                failures.append(
                    IsolatedServerFailure("owner_identity_changed").as_record()
                )
                return False
            return True
        if ownership.state == OWNERSHIP_AMBIGUOUS:
            failures.append(
                IsolatedServerFailure("port_ownership_ambiguous").as_record()
            )
            return False
        if ownership.state == OWNERSHIP_OWNED:
            failures.append(IsolatedServerFailure("port_not_closed").as_record())
            return False
        observed_pid = ownership.foreign_owner_process_id
        assert observed_pid is not None
        if identity is None or observed_pid != identity.process_id:
            failures.append(IsolatedServerFailure("owner_identity_changed").as_record())
            return False
        try:
            fresh_creation_time = api.query_pid_creation_time(observed_pid)
            signaled = api.handle_signaled(identity.handle)
        except IsolatedServerFailure as exc:
            failures.append(exc.as_record())
            return False
        if fresh_creation_time != identity.creation_time or signaled:
            # Same PID with a different creation time, or a signaled held
            # handle while a same-PID row exists, is a reused PID.
            failures.append(IsolatedServerFailure("owner_identity_changed").as_record())
            return False
        # The identical unchanged foreign owner: memory-only evidence that
        # the benchmark endpoint holds nothing of ours.
        return True

    @staticmethod
    def _close_identity_handle(
        attempt: _ServerAttempt, api: LifecycleApi, failures: list[dict[str, Any]]
    ) -> None:
        identity = attempt.foreign_identity
        if identity is None or identity.handle_closed:
            return
        try:
            api.close_handle(identity.handle)
        except IsolatedServerFailure as exc:
            failures.append(exc.as_record())
        identity.handle_closed = True

    @staticmethod
    def _apply_cleanup(artifact: dict[str, Any], cleanup: _CleanupEvidence) -> None:
        artifact["shutdown"] = cleanup.shutdown
        artifact["orphan_verification"] = cleanup.orphan_verification
        artifact["port_closed"] = cleanup.port_closed
        artifact["temporary_space_torn_down"] = cleanup.temporary_space_torn_down

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
        "job_assignment_verified", "model_residency_verified_empty", "logs",
        "readiness_duration_ms", "shutdown",
        "orphan_verification", "port_closed", "temporary_space_torn_down",
        "failures", "overall_diagnostic_evidence_state",
    }
)
_RUNTIME_KEYS = frozenset(
    {
        "executable_basename",
        "executable_sha256",
        "client_version",
        "command_server_version",
        "api_version",
        "startup_version",
    }
)
_RUNTIME_VERSION_KEYS = ("client_version", "command_server_version", "api_version")
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
    settings = artifact["attested_settings"]
    if _closed(runtime, _RUNTIME_KEYS, "runtime_identity", problems):
        basename = runtime["executable_basename"]
        if basename != "unavailable" and (not isinstance(basename, str) or not _SAFE_BASENAME.fullmatch(basename)):
            problems.append("runtime executable basename is invalid")
        digest = runtime["executable_sha256"]
        if digest != "unavailable" and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            problems.append("runtime executable hash is invalid")
        for key in _RUNTIME_VERSION_KEYS:
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
            runtime[key] for key in _RUNTIME_VERSION_KEYS if runtime[key] != "unavailable"
        ]
        if len(available_versions) >= 2 and len(set(available_versions)) != 1:
            problems.append("client, command-reported server, and API versions must match")

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
    for key in (
        "endpoint_owner_verified",
        "job_assignment_verified",
        "model_residency_verified_empty",
        "port_closed",
        "temporary_space_torn_down",
    ):
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
                "model_residency_verified_empty",
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
            for key in (
                "executable_basename",
                "executable_sha256",
                "client_version",
                "command_server_version",
                "api_version",
            )
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
        "local_address",
        "creation_time",
        "pipe_name",
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
    attestation_timeout_seconds: float = 10.0,
    version_timeout_seconds: float = 10.0,
    cleanup_timeout_seconds: float = 10.0,
    launch_attempts: int = DEFAULT_SERVER_LAUNCH_ATTEMPTS,
) -> dict[str, Any]:
    """Return a closed, privacy-safe plan without probing or allocating."""

    overrides = validate_user_overrides(user_overrides)
    timeouts = {
        "startup": startup_timeout_seconds,
        "attestation": attestation_timeout_seconds,
        "version": version_timeout_seconds,
        "cleanup": cleanup_timeout_seconds,
    }
    for name, value in timeouts.items():
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0.1 <= value <= 300
        ):
            raise Phase1ContractError(
                f"{name} timeout must be between 0.1 and 300 seconds"
            )
    if (
        isinstance(launch_attempts, bool)
        or not isinstance(launch_attempts, int)
        or not 1 <= launch_attempts <= 8
    ):
        raise Phase1ContractError("launch attempts must be between 1 and 8")
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
        "deadlines_ms": {
            "server_readiness": int(startup_timeout_seconds * 1000),
            "startup_attestation": int(attestation_timeout_seconds * 1000),
            "version_binding": int(version_timeout_seconds * 1000),
            "cleanup": int(cleanup_timeout_seconds * 1000),
        },
        "server_launch_attempts": launch_attempts,
        "reviewed_dialect_required": True,
        "reviewed_dialect_registry_entries": len(REVIEWED_DIALECT_REGISTRY),
        "version_binding": "post_readiness_owned_endpoint_only",
        "tcp_ownership": "address_aware_closed_result_model",
        "log_io": "overlapped_no_helper_threads",
        "model_access": "none_empty_temp_store",
        "model_residency_proof": "bounded_owned_get_api_ps_requires_empty",
        "network_scope": "loopback_only",
    }
