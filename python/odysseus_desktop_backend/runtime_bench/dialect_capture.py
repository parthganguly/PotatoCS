"""Bounded, redacted evidence capture for an unreviewed Ollama dialect.

This module deliberately does not consult or mutate the reviewed dialect
registry.  Its result is observational evidence only and is structurally
incompatible with the Phase-1 attestation artifact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from odysseus_desktop_backend.runtime_bench.isolated_server import (
    FAILURE_NUMERIC_METADATA_KEYS,
    MAX_METADATA_NUMBER,
    MAX_MODEL_RESIDENCY_RESPONSE_BYTES,
    MAX_VERSION_OUTPUT_BYTES,
    MODEL_RESIDENCY_TIMEOUT_SECONDS,
    OWNERSHIP_AMBIGUOUS,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NOT_PRESENT,
    OWNERSHIP_OWNED,
    PHASE1_FAILURE_CATEGORIES,
    BoundedLogCapture,
    BoundedVersionCapture,
    IsolatedServerFailure,
    LifecycleApi,
    OverlappedLogPump,
    Phase1ContractError,
    SessionSpace,
    WindowsLifecycleApi,
    build_child_environment,
    cancel_pending_pipe_io,
    choose_loopback_port,
    create_session_space,
    hash_executable,
    normalize_ollama_version,
    resolve_endpoint_ownership,
    run_owned_version_probe,
    teardown_session_space,
    verify_empty_model_residency,
    verify_suspended_executable,
)


__all__ = [
    "EVIDENCE_STATE",
    "MAX_STARTUP_EVIDENCE_LINES",
    "MAX_EVIDENCE_LINE_CHARS",
    "STARTUP_EVIDENCE_KEYWORDS",
    "redact_evidence_line",
    "select_startup_evidence_lines",
    "select_version_command_lines",
    "DialectCaptureSession",
    "build_capture_dry_run_plan",
    "validate_capture_result",
]


EVIDENCE_STATE = "unreviewed_capture"
MAX_STARTUP_EVIDENCE_LINES = 32
MAX_EVIDENCE_LINE_CHARS = 512
STARTUP_EVIDENCE_KEYWORDS = frozenset(
    {
        "version",
        "starting server",
        "listening",
        "listen",
        "ready",
        "readiness",
        "noprune",
        "pruning disabled",
        "no_cloud",
        "cloud disabled",
    }
)

_WAIT_SLICE_MS = 50
_STARTUP_DRAIN_MAX_ATTEMPTS = 3
_STARTUP_DRAIN_WAIT_MS = 10
_SAFE_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = frozenset(
    {"schema_version", "evidence_state", "executable", "observations", "proofs", "truncation", "failures"}
)
_EXECUTABLE_KEYS = frozenset({"basename", "sha256"})
_OBSERVATION_KEYS = frozenset(
    {"api_version", "version_command_lines", "startup_evidence_lines", "api_ps_empty"}
)
_PROOF_KEYS = frozenset(
    {
        "executable_image_verified",
        "endpoint_owned_before_api_version",
        "endpoint_owned_before_version_command",
        "empty_model_store_used",
        "api_ps_exactly_empty",
        "shutdown_confirmed",
        "orphan_check_clean",
        "endpoint_closed",
        "temporary_space_removed",
    }
)
_TRUNCATION_KEYS = frozenset(
    {"startup_evidence_truncated", "version_command_truncated"}
)

_REPLACEMENT_TOKENS = r"<(?:PATH|USER|HOST|PORT|URL|SECRET|PID|PIPE|HANDLE)>"
_REPLACEMENT_TOKEN_FULLMATCH = re.compile(rf"^{_REPLACEMENT_TOKENS}$")

_QUOTED_PATH = re.compile(
    r'"[^"\r\n]*[\\/][^"\r\n]*"' r"|'[^'\r\n]*[\\/][^'\r\n]*'"
)
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
_PIPE = re.compile(r"(?i)\\\\\.\\pipe\\[^\s,;]+")
_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization\s*:\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd)\s*(?:=|:)?\s*)([^\s,;]+)"
)
_PROXY_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:https?|all|wss)_proxy\s*(?:=|:)?\s*)([^\s,;]+)"
)
_PID = re.compile(
    r"(?i)(\(\s*pid\s+|\bprocess[_ ]id\s*(?:=|:)?\s*|\bpid\s*(?:=|:)?\s*)(\d+)(\)?)"
)
_HANDLE = re.compile(
    r"(?i)(\bprocess[_ ]handle\s*(?:=|:)?\s*|\bhandle\s*(?:=|:)?\s*)(?:0x[0-9a-f]+|\d+)"
)
_USER = re.compile(r"(?i)(\b(?:username|user)\s*=\s*)[^\s,;]+")
_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:\\[^\s,;]+")
_UNC_PATH = re.compile(r"\\\\[^\\\s]+\\[^\s,;]+")
_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9_:])/(?:[^\s/,;]+/)*[^\s,;]*")
_BRACKETED_IPV6 = re.compile(
    r"\[[0-9A-Fa-f:.]+(?:%[0-9A-Za-z]+)?\](?::\d{1,5})?"
)
_IPV4 = re.compile(
    r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?::\d{1,5})?(?![\d.])"
)
_IPV6 = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4}"
    r"(?:%[0-9A-Za-z]+)?(?![0-9A-Za-z:])"
)
_HOST_WITH_PORT = re.compile(r"(?i)\b(?:localhost|[a-z0-9][a-z0-9.-]*):\d{1,5}\b")
_PORT_CONTEXT = re.compile(
    r"(?i)\b(port|listen(?:ing)?)\b((?:\s+on)?[\s:=]*)(\d{1,5})\b"
)
_DOTTED_HOST = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]*[a-z0-9])?\b"
)
_HOST_ASSIGNMENT = re.compile(r"(?i)(\bhost(?:name)?\s*=\s*)(?:localhost|[a-z0-9][a-z0-9.-]*)")
_HOST_CONTEXT = re.compile(
    r"(?i)(\b(?:on|host|hostname)\s+)(?:localhost|[a-z0-9][a-z0-9.-]*)"
)
_LOCALHOST = re.compile(r"(?i)\blocalhost\b")
_PORT = re.compile(r":\d{1,5}\b")


def _replace_url(match: re.Match[str]) -> str:
    value = match.group(0)
    suffix = ""
    while value and value[-1] in ".,;)]}":
        suffix = value[-1] + suffix
        value = value[:-1]
    return "<URL>" + suffix


def _replace_pid(match: re.Match[str]) -> str:
    prefix = match.group(1)
    suffix = match.group(3) or ""
    return f"{prefix}<PID>{suffix}"


def _replace_host_with_optional_port(match: re.Match[str]) -> str:
    value = match.group(0)
    has_port = "]:" in value if value.startswith("[") else value.count(":") == 1
    return "<HOST>:<PORT>" if has_port else "<HOST>"


def _replace_port_context(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}<PORT>"


def _replace_assignment_secret(match: re.Match[str]) -> str:
    prefix, value = match.group(1), match.group(2)
    if _REPLACEMENT_TOKEN_FULLMATCH.match(value):
        return match.group(0)
    return prefix + "<SECRET>"


def redact_evidence_line(line: str) -> str:
    """Redact identity- and secret-bearing values while preserving grammar.

    Quoted values and explicit secret/credential assignments are redacted
    first so a prior substitution (e.g. collapsing a whole URL to
    ``<URL>``) cannot expose fragments to the later, more generic
    hostname/path passes. Replacement tokens are never themselves matched
    by a later pass.
    """

    if not isinstance(line, str):
        raise TypeError("evidence lines must be strings")
    value = _QUOTED_PATH.sub("<PATH>", line)
    value = _URL.sub(_replace_url, value)
    value = _PIPE.sub("<PIPE>", value)
    value = _AUTHORIZATION.sub(lambda match: match.group(1) + "<SECRET>", value)
    value = _BEARER.sub("<SECRET>", value)
    value = _SECRET_ASSIGNMENT.sub(_replace_assignment_secret, value)
    value = _PROXY_ASSIGNMENT.sub(_replace_assignment_secret, value)
    value = _PID.sub(_replace_pid, value)
    value = _HANDLE.sub(lambda match: match.group(1) + "<HANDLE>", value)
    value = _USER.sub(lambda match: match.group(1) + "<USER>", value)
    value = _WINDOWS_PATH.sub("<PATH>", value)
    value = _UNC_PATH.sub("<PATH>", value)
    value = _UNIX_PATH.sub("<PATH>", value)
    value = _BRACKETED_IPV6.sub(_replace_host_with_optional_port, value)
    value = _IPV4.sub(_replace_host_with_optional_port, value)
    value = _IPV6.sub("<HOST>", value)
    value = _PORT_CONTEXT.sub(_replace_port_context, value)
    value = _HOST_WITH_PORT.sub("<HOST>:<PORT>", value)
    value = _DOTTED_HOST.sub("<HOST>", value)
    value = _HOST_ASSIGNMENT.sub(lambda match: match.group(1) + "<HOST>", value)
    value = _HOST_CONTEXT.sub(lambda match: match.group(1) + "<HOST>", value)
    value = _LOCALHOST.sub("<HOST>", value)
    value = _PORT.sub(":<PORT>", value)
    return value


def _contains_unredacted_sensitive_data(value: str) -> bool:
    """Fail closed on a recognizable raw sensitive form.

    Independently re-derives the answer from the same redaction rules
    instead of trusting that ``redact_evidence_line`` was ever called on
    this value: a string is clean exactly when redacting it is a no-op.
    Designated tokens are already stable fixed points of every pattern
    (see ``test_replacement_tokens_survive_the_full_pipeline_unmodified``),
    and none of the patterns match plain version strings or ordinary
    punctuation without an explicit sensitive marker (a scheme, an IP
    shape, or a host/port/pid/handle keyword), so neither is flagged.
    """

    if "\x00" in value:
        return True
    return redact_evidence_line(value) != value


def _decode_lines(raw: bytes) -> list[str]:
    if not isinstance(raw, bytes):
        raise TypeError("evidence input must be bytes")
    return raw.decode("utf-8", errors="replace").splitlines()


def select_startup_evidence_lines(raw: bytes) -> tuple[list[str], bool]:
    """Select allow-listed, redacted startup lines under fixed caps."""

    selected: list[str] = []
    truncated = False
    for line in _decode_lines(raw):
        if not any(keyword in line.casefold() for keyword in STARTUP_EVIDENCE_KEYWORDS):
            continue
        if len(selected) >= MAX_STARTUP_EVIDENCE_LINES:
            truncated = True
            continue
        redacted = redact_evidence_line(line)
        if len(line) > MAX_EVIDENCE_LINE_CHARS or len(redacted) > MAX_EVIDENCE_LINE_CHARS:
            truncated = True
        selected.append(redacted[:MAX_EVIDENCE_LINE_CHARS])
    return selected, truncated


def select_version_command_lines(raw: bytes) -> tuple[list[str], bool]:
    """Bound, redact, and retain every version-command output line."""

    bounded = BoundedVersionCapture()
    bounded.feed(raw)
    truncated = bounded.overflowed
    selected: list[str] = []
    for line in _decode_lines(bounded.bytes()):
        redacted = redact_evidence_line(line)
        if len(line) > MAX_EVIDENCE_LINE_CHARS or len(redacted) > MAX_EVIDENCE_LINE_CHARS:
            truncated = True
        selected.append(redacted[:MAX_EVIDENCE_LINE_CHARS])
    return selected, truncated


def _default_api_version(port: int, timeout: float) -> bytes:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/version", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - ownership proved first
        payload = json.loads(response.read(4096).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise ValueError("malformed version response")
    return payload["version"].encode("utf-8")


def _default_model_residency(port: int, timeout: float) -> bytes:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/ps", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - ownership proved first
        return response.read(MAX_MODEL_RESIDENCY_RESPONSE_BYTES + 1)


def _empty_capture_result() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evidence_state": EVIDENCE_STATE,
        "executable": {"basename": "unavailable", "sha256": "unavailable"},
        "observations": {
            "api_version": None,
            "version_command_lines": [],
            "startup_evidence_lines": [],
            "api_ps_empty": False,
        },
        "proofs": {key: False for key in sorted(_PROOF_KEYS)},
        "truncation": {
            "startup_evidence_truncated": False,
            "version_command_truncated": False,
        },
        "failures": [],
    }


def _deduplicate_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for failure in failures:
        if failure["category"] not in seen:
            result.append(failure)
            seen.add(failure["category"])
    return result


class DialectCaptureSession:
    def __init__(
        self,
        executable: str | Path,
        *,
        startup_timeout_seconds: float = 30.0,
        version_timeout_seconds: float = 10.0,
        cleanup_timeout_seconds: float = 10.0,
        api: LifecycleApi | None = None,
        api_version_probe: Callable[[int, float], str | bytes] = _default_api_version,
        model_residency_probe: Callable[[int, float], str | bytes] = _default_model_residency,
        clock: Callable[[], float] = time.monotonic,
        temp_parent: Path | None = None,
    ) -> None:
        self.startup_timeout_seconds = self._timeout(startup_timeout_seconds, "startup")
        self.version_timeout_seconds = self._timeout(version_timeout_seconds, "version")
        self.cleanup_timeout_seconds = self._timeout(cleanup_timeout_seconds, "cleanup")
        self.executable_input = Path(executable)
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

    @staticmethod
    def _record_failure(result: dict[str, Any], failure: IsolatedServerFailure) -> None:
        result["failures"].append(failure.as_record())

    def _drain_startup_evidence(
        self,
        *,
        process: Any,
        pump: OverlappedLogPump,
        capture: BoundedLogCapture,
        api: LifecycleApi,
    ) -> None:
        """Bounded, best-effort drain of already-available output.

        A healthy server's pipes may never reach EOF, so this never waits
        for ``pump.all_finished``: it consumes whatever is already
        complete and performs only a small fixed number of short waits.
        The pump itself is left running so later lifecycle steps and
        final cleanup keep draining safely.
        """

        pump.service()
        for _ in range(_STARTUP_DRAIN_MAX_ATTEMPTS):
            if pump.all_finished:
                break
            exit_code = api.process_exit_code(process)
            if exit_code is not None:
                raise IsolatedServerFailure(
                    "startup_process_exit",
                    exit_code=max(0, min(exit_code, MAX_METADATA_NUMBER)),
                )
            pump.wait(_STARTUP_DRAIN_WAIT_MS)
            pump.service()

    def _run_version_command(
        self,
        *,
        executable: Path,
        basename: str,
        digest: str,
        port: int,
        api: LifecycleApi,
    ) -> bytes:
        space = create_session_space(self.temp_parent)
        primary: IsolatedServerFailure | None = None
        raw: bytes | None = None
        try:
            environment = build_child_environment(
                executable=executable,
                space=space,
                endpoint_port=port,
                user_overrides=None,
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
        return raw

    def _cleanup(
        self,
        *,
        result: dict[str, Any],
        api: LifecycleApi | None,
        space: SessionSpace | None,
        port: int | None,
        process: Any,
        job: Any,
        job_assigned: bool,
        capture: BoundedLogCapture,
    ) -> None:
        cleanup_failures: list[IsolatedServerFailure] = []
        shutdown_clean = False
        if api is not None and process is not None:
            deadline = self.clock() + self.cleanup_timeout_seconds
            try:
                if job is not None and job_assigned:
                    api.terminate_job(job)
                else:
                    api.terminate_process(process)
                wait_ms = int(max(0.0, deadline - self.clock()) * 1000)
                if api.wait_process(process, min(5000, wait_ms)):
                    shutdown_clean = True
                else:
                    cleanup_failures.append(
                        IsolatedServerFailure("unclean_shutdown", timeout_ms=5000)
                    )
            except IsolatedServerFailure as exc:
                cleanup_failures.append(exc)
            io_failures = cancel_pending_pipe_io(
                api,
                (process.stdout, process.stderr),
                deadline=deadline,
                clock=self.clock,
                capture=capture,
            )
            cleanup_failures.extend(io_failures)
            if io_failures:
                shutdown_clean = False
            try:
                if api.descendant_process_ids(process.process_id):
                    cleanup_failures.append(IsolatedServerFailure("orphaned_runner"))
                else:
                    result["proofs"]["orphan_check_clean"] = True
            except IsolatedServerFailure as exc:
                cleanup_failures.append(exc)
            if port is not None:
                try:
                    ownership = resolve_endpoint_ownership(api, port, process=process, job=job)
                    if ownership.state == OWNERSHIP_NOT_PRESENT:
                        result["proofs"]["endpoint_closed"] = True
                    elif ownership.state == OWNERSHIP_AMBIGUOUS:
                        cleanup_failures.append(IsolatedServerFailure("port_ownership_ambiguous"))
                    else:
                        cleanup_failures.append(IsolatedServerFailure("port_not_closed"))
                except IsolatedServerFailure as exc:
                    cleanup_failures.append(exc)
            for handle in (process.thread_handle, process.process_handle, job):
                if handle is not None:
                    try:
                        api.close_handle(handle)
                    except IsolatedServerFailure as exc:
                        cleanup_failures.append(exc)
                        shutdown_clean = False
        elif api is not None and port is not None:
            try:
                ownership = resolve_endpoint_ownership(api, port, process=None, job=None)
                if ownership.state == OWNERSHIP_NOT_PRESENT:
                    result["proofs"]["endpoint_closed"] = True
                elif ownership.state == OWNERSHIP_AMBIGUOUS:
                    cleanup_failures.append(IsolatedServerFailure("port_ownership_ambiguous"))
                else:
                    cleanup_failures.append(IsolatedServerFailure("port_not_closed"))
            except IsolatedServerFailure as exc:
                cleanup_failures.append(exc)
        result["proofs"]["shutdown_confirmed"] = shutdown_clean
        if space is not None:
            try:
                teardown_session_space(space)
                result["proofs"]["temporary_space_removed"] = True
            except IsolatedServerFailure as exc:
                cleanup_failures.append(exc)
        for failure in cleanup_failures:
            self._record_failure(result, failure)

    def run(self) -> dict[str, Any]:
        result = _empty_capture_result()
        lifecycle_api = self.api
        executable: Path | None = None
        basename = "unavailable"
        digest = "unavailable"
        space: SessionSpace | None = None
        port: int | None = None
        process: Any = None
        job: Any = None
        job_assigned = False
        capture = BoundedLogCapture()
        pump: OverlappedLogPump | None = None
        try:
            if os.name != "nt" and lifecycle_api is None:
                raise IsolatedServerFailure("platform_unsupported")
            lifecycle_api = lifecycle_api or WindowsLifecycleApi()
            executable = self._resolve_executable()
            basename, digest = hash_executable(executable)
            result["executable"] = {"basename": basename, "sha256": digest}

            space = create_session_space(self.temp_parent)
            port = choose_loopback_port(frozenset())
            environment = build_child_environment(
                executable=executable,
                space=space,
                endpoint_port=port,
                user_overrides=None,
            )
            process = lifecycle_api.create_suspended(executable, ("serve",), environment)
            result["proofs"]["empty_model_store_used"] = True
            verify_suspended_executable(lifecycle_api, process, executable, basename, digest)
            result["proofs"]["executable_image_verified"] = True
            job = lifecycle_api.create_job()
            lifecycle_api.configure_kill_on_close(job)
            lifecycle_api.assign_process(job, process)
            job_assigned = True
            if not lifecycle_api.verify_job_assignment(job, process):
                raise IsolatedServerFailure("job_assignment_failed")
            pump = OverlappedLogPump(lifecycle_api, process, capture)
            pump.post_initial()

            started = self.clock()
            deadline = started + self.startup_timeout_seconds
            lifecycle_api.resume_process(process)
            while True:
                pump.service()
                ownership = resolve_endpoint_ownership(
                    lifecycle_api, port, process=process, job=job
                )
                exit_code = lifecycle_api.process_exit_code(process)
                if ownership.state == OWNERSHIP_OWNED:
                    result["proofs"]["endpoint_owned_before_api_version"] = True
                    break
                if ownership.state == OWNERSHIP_FOREIGN:
                    raise IsolatedServerFailure("port_hijacked")
                if ownership.state == OWNERSHIP_AMBIGUOUS:
                    raise IsolatedServerFailure("port_ownership_ambiguous")
                if exit_code is not None:
                    raise IsolatedServerFailure(
                        "startup_process_exit",
                        exit_code=max(0, min(exit_code, MAX_METADATA_NUMBER)),
                    )
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise IsolatedServerFailure(
                        "startup_timeout", timeout_ms=int(self.startup_timeout_seconds * 1000)
                    )
                pump.wait(int(min(_WAIT_SLICE_MS, remaining * 1000)))

            remaining = deadline - self.clock()
            if remaining <= 0:
                raise IsolatedServerFailure(
                    "startup_timeout", timeout_ms=int(self.startup_timeout_seconds * 1000)
                )
            try:
                raw_api_version = self.api_version_probe(port, min(1.0, remaining))
                result["observations"]["api_version"] = normalize_ollama_version(
                    raw_api_version
                )
            except (
                IsolatedServerFailure,
                OSError,
                TypeError,
                ValueError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as exc:
                if isinstance(exc, IsolatedServerFailure) and exc.category == "version_probe_failed":
                    raise
                raise IsolatedServerFailure("version_probe_failed") from exc

            self._drain_startup_evidence(
                process=process,
                pump=pump,
                capture=capture,
                api=lifecycle_api,
            )

            ownership = resolve_endpoint_ownership(
                lifecycle_api, port, process=process, job=job
            )
            if ownership.state != OWNERSHIP_OWNED:
                raise IsolatedServerFailure("version_endpoint_ownership_failed")
            result["proofs"]["endpoint_owned_before_version_command"] = True
            raw_version = self._run_version_command(
                executable=executable,
                basename=basename,
                digest=digest,
                port=port,
                api=lifecycle_api,
            )
            version_lines, version_truncated = select_version_command_lines(raw_version)
            result["observations"]["version_command_lines"] = version_lines
            result["truncation"]["version_command_truncated"] = version_truncated

            ownership = resolve_endpoint_ownership(
                lifecycle_api, port, process=process, job=job
            )
            if ownership.state == OWNERSHIP_AMBIGUOUS:
                raise IsolatedServerFailure("port_ownership_ambiguous")
            if ownership.state == OWNERSHIP_FOREIGN:
                raise IsolatedServerFailure("port_hijacked")
            if ownership.state != OWNERSHIP_OWNED:
                raise IsolatedServerFailure("ownership_probe_unavailable")
            try:
                raw_ps = self.model_residency_probe(
                    port, MODEL_RESIDENCY_TIMEOUT_SECONDS
                )
                verify_empty_model_residency(raw_ps)
            except IsolatedServerFailure:
                raise
            except (
                OSError,
                TypeError,
                ValueError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as exc:
                raise IsolatedServerFailure("model_residency_probe_failed") from exc
            result["observations"]["api_ps_empty"] = True
            result["proofs"]["api_ps_exactly_empty"] = True
        except IsolatedServerFailure as exc:
            if exc.category == "version_probe_output_overflow":
                result["truncation"]["version_command_truncated"] = True
            self._record_failure(result, exc)
        finally:
            self._cleanup(
                result=result,
                api=lifecycle_api,
                space=space,
                port=port,
                process=process,
                job=job,
                job_assigned=job_assigned,
                capture=capture,
            )
            startup_lines, startup_truncated = select_startup_evidence_lines(capture.bytes())
            result["observations"]["startup_evidence_lines"] = startup_lines
            result["truncation"]["startup_evidence_truncated"] = (
                startup_truncated or capture.truncated
            )
        result["failures"] = _deduplicate_failures(result["failures"])
        problems = validate_capture_result(result)
        if problems:
            raise AssertionError(f"internal capture result invalid: {problems}")
        return result


def build_capture_dry_run_plan(
    *,
    startup_timeout_seconds: float = 30.0,
    version_timeout_seconds: float = 10.0,
    cleanup_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Return the deterministic process-free G-ISO-D0 capture plan."""

    timeouts = {
        "startup": startup_timeout_seconds,
        "version": version_timeout_seconds,
        "cleanup": cleanup_timeout_seconds,
    }
    for name, value in timeouts.items():
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0.1 <= value <= 300
        ):
            raise Phase1ContractError(f"{name} timeout must be between 0.1 and 300 seconds")
    return {
        "mode": "dry_run",
        "would_spawn": False,
        "schema_version": 1,
        "evidence_state": EVIDENCE_STATE,
        "environment": {
            "construction": "from_empty",
            "user_overrides": False,
            "model_store": "empty_temp",
        },
        "deadlines_ms": {
            "startup": int(startup_timeout_seconds * 1000),
            "version": int(version_timeout_seconds * 1000),
            "cleanup": int(cleanup_timeout_seconds * 1000),
        },
        "evidence_limits": {
            "startup_lines": MAX_STARTUP_EVIDENCE_LINES,
            "line_chars": MAX_EVIDENCE_LINE_CHARS,
            "startup_log_bytes": 256 * 1024,
            "version_command_bytes": MAX_VERSION_OUTPUT_BYTES,
            "model_residency_response_bytes": MAX_MODEL_RESIDENCY_RESPONSE_BYTES,
        },
        "gates": {
            "approval_flag": "--approved-g-iso-d0",
            "interactive_terminal_required": True,
        },
        "server_launch_attempts": 1,
        "reviewed_dialect_required": False,
        "network_scope": "loopback_only",
        "raw_evidence_persistence": "none",
    }


def _closed(value: Any, keys: frozenset[str], where: str, problems: list[str]) -> bool:
    if not isinstance(value, dict):
        problems.append(f"{where} must be an object")
        return False
    if set(value) != set(keys):
        problems.append(f"{where} must have exactly {sorted(keys)}")
        return False
    return True


def validate_capture_result(result: Any) -> list[str]:
    """Validate the closed, privacy-safe dialect-capture result schema."""

    problems: list[str] = []
    if not _closed(result, _TOP_KEYS, "result", problems):
        return problems
    if isinstance(result["schema_version"], bool) or result["schema_version"] != 1:
        problems.append("unsupported capture schema_version")
    if result["evidence_state"] != EVIDENCE_STATE:
        problems.append("evidence_state must be unreviewed_capture")

    executable = result["executable"]
    if _closed(executable, _EXECUTABLE_KEYS, "executable", problems):
        basename = executable["basename"]
        if basename != "unavailable" and (
            not isinstance(basename, str) or not _SAFE_BASENAME.fullmatch(basename)
        ):
            problems.append("executable.basename is invalid")
        digest = executable["sha256"]
        if digest != "unavailable" and (
            not isinstance(digest, str) or not _SHA256.fullmatch(digest)
        ):
            problems.append("executable.sha256 is invalid")

    observations = result["observations"]
    if _closed(observations, _OBSERVATION_KEYS, "observations", problems):
        api_version = observations["api_version"]
        if api_version is not None:
            try:
                if not isinstance(api_version, str) or normalize_ollama_version(api_version) != api_version:
                    problems.append("observations.api_version is not normalized")
            except ValueError:
                problems.append("observations.api_version is invalid")
        for key, limit in (
            ("version_command_lines", None),
            ("startup_evidence_lines", MAX_STARTUP_EVIDENCE_LINES),
        ):
            lines = observations[key]
            if (
                not isinstance(lines, list)
                or (limit is not None and len(lines) > limit)
                or any(not isinstance(line, str) or len(line) > MAX_EVIDENCE_LINE_CHARS for line in lines)
            ):
                problems.append(f"observations.{key} is invalid")
            elif key == "version_command_lines":
                aggregate_bytes = sum(len(line.encode("utf-8")) for line in lines)
                if aggregate_bytes > MAX_VERSION_OUTPUT_BYTES:
                    problems.append(
                        "observations.version_command_lines exceeds the aggregate byte cap"
                    )
        if not isinstance(observations["api_ps_empty"], bool):
            problems.append("observations.api_ps_empty must be boolean")

    proofs = result["proofs"]
    if _closed(proofs, _PROOF_KEYS, "proofs", problems):
        if any(not isinstance(value, bool) for value in proofs.values()):
            problems.append("proofs must contain only booleans")

    truncation = result["truncation"]
    if _closed(truncation, _TRUNCATION_KEYS, "truncation", problems):
        if any(not isinstance(value, bool) for value in truncation.values()):
            problems.append("truncation must contain only booleans")

    failures = result["failures"]
    if not isinstance(failures, list) or len(failures) > len(PHASE1_FAILURE_CATEGORIES):
        problems.append("failures must be a bounded list")
    else:
        seen: set[str] = set()
        for index, failure in enumerate(failures):
            if not isinstance(failure, dict) or set(failure) != {"category", "numeric_metadata"}:
                problems.append(f"failures[{index}] must be closed")
                continue
            category = failure["category"]
            if (
                not isinstance(category, str)
                or category not in PHASE1_FAILURE_CATEGORIES
                or category in seen
            ):
                problems.append(f"failures[{index}] category is invalid or duplicated")
            if isinstance(category, str):
                seen.add(category)
            metadata = failure["numeric_metadata"]
            if not isinstance(metadata, dict) or set(metadata) - FAILURE_NUMERIC_METADATA_KEYS:
                problems.append(f"failures[{index}] metadata is not closed")
            elif any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_METADATA_NUMBER
                for value in metadata.values()
            ):
                problems.append(f"failures[{index}] metadata is out of bounds")

    if isinstance(observations, dict) and isinstance(proofs, dict):
        if observations.get("api_ps_empty") != proofs.get("api_ps_exactly_empty"):
            problems.append("api_ps observation and proof must agree")
        if observations.get("api_version") is not None and not proofs.get(
            "endpoint_owned_before_api_version"
        ):
            problems.append("api_version requires endpoint ownership proof")
        if failures == [] and not all(proofs.get(key) is True for key in _PROOF_KEYS):
            problems.append("failure-free capture requires every proof")
        if failures and all(proofs.get(key) is True for key in _PROOF_KEYS):
            problems.append("capture with failures cannot have every proof")

    def inspect(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                inspect(child, f"{where}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{where}[{index}]")
        elif isinstance(value, str):
            remainder = value.replace("<PATH>", "")
            if "\\" in remainder or "/" in remainder or "\x00" in remainder:
                problems.append(f"{where} contains unredacted path-like data")

    inspect(result, "result")

    # Independently re-derive privacy safety for every evidence-bearing
    # string (never trusting that redact_evidence_line was previously
    # called). Scoped to the fields that actually carry captured
    # evidence -- executable.basename/sha256 and the fixed category
    # enum are validated by their own dedicated checks above and are not
    # redaction targets.
    if isinstance(observations, dict):
        api_version = observations.get("api_version")
        if isinstance(api_version, str) and _contains_unredacted_sensitive_data(api_version):
            problems.append("observations.api_version contains unredacted sensitive data")
        for key in ("version_command_lines", "startup_evidence_lines"):
            lines = observations.get(key)
            if isinstance(lines, list):
                for index, entry in enumerate(lines):
                    if isinstance(entry, str) and _contains_unredacted_sensitive_data(entry):
                        problems.append(
                            f"observations.{key}[{index}] contains unredacted sensitive data"
                        )
    if isinstance(failures, list):
        for index, failure in enumerate(failures):
            if isinstance(failure, dict):
                category = failure.get("category")
                if isinstance(category, str) and _contains_unredacted_sensitive_data(category):
                    problems.append(
                        f"failures[{index}].category contains unredacted sensitive data"
                    )
    return problems
