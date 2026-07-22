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
# A "value" counts as already fully redacted if it is one designated
# token, or a short run of them joined by ":" (e.g. the "<HOST>:<PORT>"
# pair a prior host/IP pass may have produced). Either shape must be a
# stable fixed point of every later assignment-style substitution.
_REPLACEMENT_TOKEN_FULLMATCH = re.compile(
    rf"^{_REPLACEMENT_TOKENS}(?::{_REPLACEMENT_TOKENS})*$"
)

_QUOTED_PATH = re.compile(
    r'"[^"\r\n]*[\\/][^"\r\n]*"' r"|'[^'\r\n]*[\\/][^'\r\n]*'"
)
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
_PIPE = re.compile(r"(?i)\\\\\.\\pipe\\[^\s,;]+")
_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization\s*:\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")

# Secret/credential keywords that only ever take an *explicit* separator
# (":" or "="). Bare whitespace is deliberately unsupported here: an
# optional/absent separator is exactly what let a keyword like
# "password" swallow the next ordinary word ("password policy loaded")
# and let "secret" swallow a hyphen-glued suffix ("secret-value") in the
# previous design.
_SECRET_ASSIGNMENT_EXPLICIT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|secret|password|passwd)\b\s*[:=]\s*)([^\s,;]+)"
)
# "token" alone additionally supports bare whitespace, since that form is
# explicitly required ("token abc"). The bare branch consumes exactly
# one whitespace-delimited value token (the value group already stops at
# the next space/comma/semicolon) and leaves everything after it
# untouched, so "token abc ready" redacts only "abc" -- an ordinary
# sentence like "tokenizer ready" is still never mistaken for an
# assignment, since "tokenizer" is excluded by \b before the bare branch
# is even considered.
_TOKEN_ASSIGNMENT = re.compile(
    r"(?i)(\btoken\b)(?:(\s*[:=]\s*)([^\s,;]+)|(\s+)([^\s,;]+))"
)
_PROXY_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:(?:https?|all|wss)_proxy|proxy_url|proxy)\b)"
    r"(?:(\s*[:=]\s*)([^\s,;]+)|(\s+)([^\s,;]+))"
)
_PID = re.compile(
    r"(?i)(\(\s*pid\s+|\bprocess[_ ]id\s*(?:=|:)?\s*|\bpid\s*(?:=|:)?\s*)(\d+)(\)?)"
)
_HANDLE = re.compile(
    r"(?i)(\bprocess[_ ]handle\s*(?:=|:)?\s*|\bhandle\s*(?:=|:)?\s*)(?:0x[0-9a-f]+|\d+)"
)
_USER_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:username|user)\b)(?:(\s*[:=]\s*)([^\s,;]+)|(\s+)([^\s,;]+))"
)
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
# Only "localhost" is treated as a bare, unqualified hostname worth
# pairing with a following ":port" -- a plain alphanumeric token (e.g.
# the leading "12" of a bare "12:34:56" timestamp) is not structurally a
# validated hostname, so it must not be accepted here. Real bare
# hostnames (e.g. "workstation") are only ever redacted through an
# explicit host/hostname/server/listening keyword context below.
_HOST_WITH_PORT = re.compile(r"(?i)\blocalhost:\d{1,5}\b")
_PORT_CONTEXT = re.compile(
    r"(?i)\b(port|listen(?:ing)?)\b((?:\s+on)?[\s:=]*)(\d{1,5})\b"
)
_DOTTED_HOST = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]*[a-z0-9])?\b"
)
# host/hostname support both explicit and bare-whitespace forms; server
# only takes an explicit separator, so "server started"/"server ready"
# are never mistaken for "server=<value>".
_HOST_ASSIGNMENT_EXPLICIT = re.compile(
    r"(?i)(\b(?:host(?:name)?|server)\b\s*[:=]\s*)([^\s,;]+)"
)
# Bare "host"/"hostname" take exactly one following value token. A bare
# "on" is deliberately NOT a generic hostname context (treating any
# "on <word>" as a host swallowed ordinary phrases like "version on
# startup"); only the structurally meaningful "listen(ing) on <value>"
# form is recognized.
_HOST_CONTEXT = re.compile(
    r"(?i)(\blisten(?:ing)?\s+on\s+|\bhost(?:name)?\s+)([^\s,;]+)"
)
_LOCALHOST = re.compile(r"(?i)\blocalhost\b")


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


_IPV6_MARKER = re.compile(r"(?i)[a-f]|::")


def _replace_bare_ipv6(match: re.Match[str]) -> str:
    """A bare (unbracketed) hex-colon run is only genuinely IPv6 shaped
    if it has a hex letter or a "::" compression -- digits 0-9 are also
    valid hex, so without this guard a plain decimal timestamp like
    "12:34:56" or "00:01:30" would be misread as an address."""

    value = match.group(0)
    if not _IPV6_MARKER.search(value):
        return value
    return "<HOST>"


def _replace_assignment(token: str) -> Callable[[re.Match[str]], str]:
    """Build a (prefix, value) substitution callback for the given token.

    A value that is already a designated token (or run of them) is left
    untouched, so a later pass can never re-tag an earlier one.
    """

    def _replace(match: re.Match[str]) -> str:
        prefix, value = match.group(1), match.group(2)
        if _REPLACEMENT_TOKEN_FULLMATCH.match(value):
            return match.group(0)
        return prefix + token

    return _replace


def _replace_explicit_or_bare_assignment(token: str) -> Callable[[re.Match[str]], str]:
    """Callback for the (keyword)(explicit-sep, value | bare-sep, value)
    four-slot pattern shared by the token/proxy/user assignments."""

    def _replace(match: re.Match[str]) -> str:
        keyword = match.group(1)
        explicit_sep, explicit_value = match.group(2), match.group(3)
        bare_sep, bare_value = match.group(4), match.group(5)
        if explicit_sep is not None:
            sep, value = explicit_sep, explicit_value
        else:
            sep, value = bare_sep, bare_value
        if _REPLACEMENT_TOKEN_FULLMATCH.match(value):
            return match.group(0)
        return f"{keyword}{sep}{token}"

    return _replace


_replace_secret_explicit = _replace_assignment("<SECRET>")
_replace_token_assignment = _replace_explicit_or_bare_assignment("<SECRET>")
_replace_proxy_assignment = _replace_explicit_or_bare_assignment("<SECRET>")
_replace_user_assignment = _replace_explicit_or_bare_assignment("<USER>")
_replace_host = _replace_assignment("<HOST>")


def redact_evidence_line(line: str) -> str:
    """Redact identity- and secret-bearing values while preserving grammar.

    Quoted values and explicit secret/credential assignments are redacted
    first so a prior substitution (e.g. collapsing a whole URL to
    ``<URL>``) cannot expose fragments to the later, more generic
    hostname/path passes. Replacement tokens are never themselves matched
    by a later pass. Keywords that only support an explicit ":"/"="
    separator (secret/password/passwd/api_key/access_token, and server)
    never consume an unrelated following word; keywords that also
    support a bare-whitespace form (token, the proxy family,
    user/username, host/hostname, "listen(ing) on") consume exactly one
    whitespace-delimited value token and leave everything after it
    untouched, so multi-field or prose-like log lines are not fully
    swallowed. There is no generic "colon followed by digits" port rule:
    a port is only redacted when structurally established by a
    validated IPv4/bracketed-IPv6 address, the literal word
    "localhost", or an explicit port/listen(ing) keyword context --
    timestamps and durations like "12:34:56" are left alone.
    """

    if not isinstance(line, str):
        raise TypeError("evidence lines must be strings")
    value = _QUOTED_PATH.sub("<PATH>", line)
    value = _URL.sub(_replace_url, value)
    value = _PIPE.sub("<PIPE>", value)
    value = _AUTHORIZATION.sub(lambda match: match.group(1) + "<SECRET>", value)
    value = _BEARER.sub("<SECRET>", value)
    value = _SECRET_ASSIGNMENT_EXPLICIT.sub(_replace_secret_explicit, value)
    value = _TOKEN_ASSIGNMENT.sub(_replace_token_assignment, value)
    value = _PROXY_ASSIGNMENT.sub(_replace_proxy_assignment, value)
    value = _PID.sub(_replace_pid, value)
    value = _HANDLE.sub(lambda match: match.group(1) + "<HANDLE>", value)
    value = _USER_ASSIGNMENT.sub(_replace_user_assignment, value)
    value = _WINDOWS_PATH.sub("<PATH>", value)
    value = _UNC_PATH.sub("<PATH>", value)
    value = _UNIX_PATH.sub("<PATH>", value)
    value = _BRACKETED_IPV6.sub(_replace_host_with_optional_port, value)
    value = _IPV4.sub(_replace_host_with_optional_port, value)
    value = _IPV6.sub(_replace_bare_ipv6, value)
    value = _PORT_CONTEXT.sub(_replace_port_context, value)
    value = _HOST_WITH_PORT.sub("<HOST>:<PORT>", value)
    value = _DOTTED_HOST.sub("<HOST>", value)
    value = _HOST_ASSIGNMENT_EXPLICIT.sub(_replace_host, value)
    value = _HOST_CONTEXT.sub(_replace_host, value)
    value = _LOCALHOST.sub("<HOST>", value)
    return value


_TOKEN_MASK = re.compile(rf"{_REPLACEMENT_TOKENS}(?::{_REPLACEMENT_TOKENS})*")


def _mask_replacement_tokens(value: str) -> str:
    """Blank designated tokens to a single space before independent
    detection, so a masked-out token can never let two unrelated fields
    on either side of it read as one concatenated value.

    A run of tokens joined by ":" (e.g. an already-redacted
    "<HOST>:<PORT>" pair) is masked as a single unit, not one space per
    token -- otherwise the connecting ":" would survive masking and a
    bare-whitespace detector could bridge across it (e.g. reading
    "listening on <HOST>:<PORT>" as "listening on" followed by a stray
    ":" value once both tokens are blanked independently).
    """

    return _TOKEN_MASK.sub(" ", value)


# Separately authored raw-content detectors used only by the validator.
# These deliberately do not call redact_evidence_line and do not reuse
# its substitution callbacks: a validator that can only recognize what
# the redactor already recognizes gives no independent verification, so
# every pattern below is its own, independently written regex. Keyword
# detectors use a bounded "\s?" around the separator (never an unbounded
# "\s*") specifically because a masked-out token can leave a short run
# of whitespace behind; an unbounded quantifier would bridge across that
# gap and misread the *next* field's keyword as this field's value.
_RAW_WINDOWS_PATH = re.compile(r"(?i)[A-Z]:\\[^\s,;]+")
_RAW_UNC_PATH = re.compile(r"\\\\[^\\\s]+\\[^\s,;]+")
_RAW_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9_:])/[^\s,;]+")
_RAW_PIPE = re.compile(r"(?i)\\\\\.\\pipe\\[^\s,;]+")
_RAW_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_RAW_URL_CREDENTIALS = re.compile(r"(?i)://[^\s/@]+:[^\s/@]+@")
_RAW_QUERY_STRING = re.compile(r"\?[A-Za-z0-9_.-]+=")
_RAW_IPV4 = re.compile(
    r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])"
)
_RAW_IPV6_SHAPE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4}"
    r"(?:%[0-9A-Za-z]+)?(?![0-9A-Za-z:])"
)


def _detect_bare_ipv6(masked: str) -> bool:
    """Same hex-letter-or-"::" guard as the redactor's bare-IPv6 pass: a
    plain decimal run of digits and colons (a timestamp like "12:34:56")
    is not IPv6 shaped just because 0-9 happen to also be valid hex."""

    match = _RAW_IPV6_SHAPE.search(masked)
    return match is not None and _IPV6_MARKER.search(match.group(0)) is not None
# Bare "on" is deliberately not a generic hostname marker here either --
# only "listen(ing) on <value>" is structurally meaningful. A bare
# "localhost:port" is also independently flagged, mirroring the
# redactor's restriction of unqualified hostname:port pairs to the
# literal word "localhost" (a plain alphanumeric prefix like the "12" of
# "12:34:56" is not a validated hostname).
#
# Bare-whitespace branches use a single literal " " (never "\s+"), for
# the same reason the separator uses a bounded "\s?": masking a token
# leaves the *original* spaces on both sides of it intact, so a field
# like "on <HOST>:<PORT> path=" can mask down to "on   path=" (three
# spaces). An unbounded "\s+" would bridge straight through and misread
# the next field's keyword as this field's value; requiring exactly one
# space means the match simply fails once more than one space remains.
_RAW_HOST_CONTEXT = re.compile(
    r"(?i)\bhost(?:name)?\b(?:\s?[:=]\s?[^\s,;]+| [^\s,;]+)"
    r"|\blisten(?:ing)? on [^\s,;]+"
    r"|\bserver\b\s?[:=]\s?[^\s,;]+"
    r"|\blocalhost:\d{1,5}\b"
)
# The optional " on" mirrors the redactor's handling of "listening on
# <port>", where an actual word separates the keyword from the digits
# (not just whitespace).
_RAW_PORT_CONTEXT = re.compile(
    r"(?i)\b(?:port|listen(?:ing)?)\b(?: on)?\s?[:=]?\s?\d"
)
_RAW_USER_CONTEXT = re.compile(
    r"(?i)\b(?:username|user)\b(?:\s?[:=]\s?[^\s,;]+| [^\s,;]+)"
)
_RAW_PID_CONTEXT = re.compile(
    r"(?i)\b(?:process[_ ]id|pid)\b\s?[:=]?\s?\d|\(\s*pid\s+\d"
)
_RAW_HANDLE_CONTEXT = re.compile(
    r"(?i)\b(?:process[_ ]handle|handle)\b\s?[:=]?\s?(?:0x[0-9a-f]|\d)"
)
_RAW_AUTH = re.compile(r"(?i)\bauthorization\b\s?:\s?\S|\bbearer\b\s+\S")
_RAW_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\btoken\b(?:\s?[:=]\s?[^\s,;]+| [^\s,;]+)"
    r"|\b(?:api[_-]?key|access[_-]?token|secret|password|passwd)\b\s?[:=]\s?[^\s,;]+"
)
_RAW_PROXY_ASSIGNMENT = re.compile(
    r"(?i)\b(?:(?:https?|all|wss)_proxy|proxy_url|proxy)\b"
    r"(?:\s?[:=]\s?[^\s,;]+| [^\s,;]+)"
)

_RAW_DETECTORS: tuple[Callable[[str], bool], ...] = (
    lambda masked: _RAW_WINDOWS_PATH.search(masked) is not None,
    lambda masked: _RAW_UNC_PATH.search(masked) is not None,
    lambda masked: _RAW_UNIX_PATH.search(masked) is not None,
    lambda masked: _RAW_PIPE.search(masked) is not None,
    lambda masked: _RAW_URL.search(masked) is not None,
    lambda masked: _RAW_URL_CREDENTIALS.search(masked) is not None,
    lambda masked: _RAW_QUERY_STRING.search(masked) is not None,
    lambda masked: _RAW_IPV4.search(masked) is not None,
    _detect_bare_ipv6,
    lambda masked: _RAW_HOST_CONTEXT.search(masked) is not None,
    lambda masked: _RAW_PORT_CONTEXT.search(masked) is not None,
    lambda masked: _RAW_USER_CONTEXT.search(masked) is not None,
    lambda masked: _RAW_PID_CONTEXT.search(masked) is not None,
    lambda masked: _RAW_HANDLE_CONTEXT.search(masked) is not None,
    lambda masked: _RAW_AUTH.search(masked) is not None,
    lambda masked: _RAW_SECRET_ASSIGNMENT.search(masked) is not None,
    lambda masked: _RAW_PROXY_ASSIGNMENT.search(masked) is not None,
)


def _contains_unredacted_sensitive_data(value: str) -> bool:
    """Independently detect a recognizable raw sensitive form.

    This never calls ``redact_evidence_line`` and never reuses its
    substitution callbacks -- it is a separately authored set of
    detection patterns, so a blind spot in the redactor is not
    automatically a blind spot here too. Designated tokens are masked to
    a single space first (not deleted), so a masked-out token can never
    let unrelated adjacent evidence be misread as its value. None of the
    detectors match a plain version string (``0.5.7``, ``v0.5.7``,
    ``0.5.7-rc1``) or ordinary punctuation, since every one of them
    requires an explicit sensitive marker: a URI scheme, an IP-address
    shape, or a host/port/user/pid/handle/secret/proxy keyword.
    """

    if "\x00" in value:
        return True
    masked = _mask_replacement_tokens(value)
    return any(detect(masked) for detect in _RAW_DETECTORS)


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
    """Bound, redact, and retain version-command output.

    Redaction can expand a line (e.g. a long path collapsing to
    ``<PATH>`` is usually shorter, but several short raw fragments each
    becoming their own token can be longer). The aggregate cap is
    therefore enforced *after* redaction, in deterministic source order:
    once appending the next complete, per-line-bounded line would push
    the running UTF-8 byte total over ``MAX_VERSION_OUTPUT_BYTES``, that
    line and every line after it are omitted, and the result is marked
    truncated. A line is never partially included, so no replacement
    token or multi-byte sequence is ever split.
    """

    bounded = BoundedVersionCapture()
    bounded.feed(raw)
    truncated = bounded.overflowed
    selected: list[str] = []
    aggregate_bytes = 0
    omit_rest = False
    for line in _decode_lines(bounded.bytes()):
        redacted = redact_evidence_line(line)
        if len(line) > MAX_EVIDENCE_LINE_CHARS or len(redacted) > MAX_EVIDENCE_LINE_CHARS:
            truncated = True
        bounded_line = redacted[:MAX_EVIDENCE_LINE_CHARS]
        if omit_rest:
            truncated = True
            continue
        line_bytes = len(bounded_line.encode("utf-8"))
        if aggregate_bytes + line_bytes > MAX_VERSION_OUTPUT_BYTES:
            omit_rest = True
            truncated = True
            continue
        aggregate_bytes += line_bytes
        selected.append(bounded_line)
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
        deadline: float,
    ) -> None:
        """Bounded, best-effort drain of already-available output.

        A healthy server's pipes may never reach EOF, so this never waits
        for ``pump.all_finished``: it consumes whatever is already
        complete and performs only a small fixed number of short waits,
        each bounded by both the fixed slice and whatever startup
        deadline actually remains. Once the deadline has passed, no
        further wait is issued -- the loop simply stops so the governing
        startup deadline is never extended. The pump itself is left
        running so later lifecycle steps and final cleanup keep draining
        safely.
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
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            wait_ms = min(_STARTUP_DRAIN_WAIT_MS, int(remaining * 1000))
            if wait_ms <= 0:
                break
            pump.wait(wait_ms)
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
                deadline=deadline,
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

    # Independently enforce privacy safety for every evidence-bearing
    # string, never trusting that redact_evidence_line was previously
    # called on it. Scoped to the fields that actually carry captured
    # evidence: executable.basename/sha256 have their own dedicated
    # validators above (and are not redaction targets -- a basename like
    # "ollama.exe" is not sensitive), and failure categories are a closed
    # enum that needs no heuristic scan.
    if isinstance(observations, dict):
        api_version = observations.get("api_version")
        if isinstance(api_version, str) and _contains_unredacted_sensitive_data(api_version):
            problems.append("observations.api_version contains raw sensitive data")
        for key in ("version_command_lines", "startup_evidence_lines"):
            lines = observations.get(key)
            if isinstance(lines, list):
                for index, entry in enumerate(lines):
                    if isinstance(entry, str) and _contains_unredacted_sensitive_data(entry):
                        problems.append(
                            f"observations.{key}[{index}] contains raw sensitive data"
                        )
    return problems
