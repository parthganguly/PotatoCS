from __future__ import annotations

import hashlib
import json
import re
import time
from types import MappingProxyType
from pathlib import Path

import pytest

from odysseus_desktop_backend.runtime_bench.__main__ import main
import odysseus_desktop_backend.runtime_bench.isolated_server as isolated_server
from odysseus_desktop_backend.runtime_bench.isolated_server import (
    ATTESTATION_ARTIFACT_KIND,
    ATTESTATION_SCHEMA_VERSION,
    FIRST_LOG_BYTES,
    FIXED_INTERNAL_ENV_KEYS,
    LAST_LOG_BYTES,
    OWNERSHIP_AMBIGUOUS,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NOT_PRESENT,
    OWNERSHIP_OWNED,
    PHASE1_FAILURE_CATEGORIES,
    USER_OVERRIDE_ENV_KEYS,
    BoundedLogCapture,
    BoundedVersionCapture,
    CreatedProcess,
    IsolatedOllamaServer,
    IsolatedServerFailure,
    Phase1ContractError,
    ReviewedDialectEntry,
    RuntimeIdentity,
    StartupDialect,
    TcpListenerRow,
    VersionOutputDialect,
    WindowsLifecycleApi,
    build_child_environment,
    build_dry_run_plan,
    capture_foreign_owner_identity,
    cancel_pending_pipe_io,
    choose_loopback_port,
    classify_relevant_listener_rows,
    compare_requested_attestation,
    create_session_space,
    empty_attestation_artifact,
    normalize_ollama_version,
    parse_startup_attestation,
    parse_version_output,
    hash_executable,
    resolve_endpoint_ownership,
    reviewed_dialect_for_hash,
    run_owned_version_probe,
    teardown_session_space,
    validate_attestation_artifact,
    validate_dialect_registry,
    validate_user_overrides,
    verify_empty_model_residency,
)


def _empty_model_residency(port: int, timeout: float) -> bytes:
    return b'{"models":[]}'


# ---------------------------------------------------------------------------
# Environment construction (unchanged by revision 6)
# ---------------------------------------------------------------------------


def test_environment_key_sets_are_separate_and_closed() -> None:
    assert FIXED_INTERNAL_ENV_KEYS.isdisjoint(USER_OVERRIDE_ENV_KEYS)
    assert USER_OVERRIDE_ENV_KEYS == {
        "OLLAMA_FLASH_ATTENTION",
        "OLLAMA_KV_CACHE_TYPE",
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_CONTEXT_LENGTH",
    }
    assert "OLLAMA_HOST" in FIXED_INTERNAL_ENV_KEYS
    assert "OLLAMA_HOST" not in USER_OVERRIDE_ENV_KEYS


@pytest.mark.parametrize("key", sorted(FIXED_INTERNAL_ENV_KEYS))
def test_caller_cannot_supply_fixed_internal_key_case_insensitively(key: str) -> None:
    with pytest.raises(Phase1ContractError, match="fixed internal"):
        validate_user_overrides({key.swapcase(): "hostile"})


def test_fixed_key_rejected_before_any_lifecycle_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touched = False

    def forbidden_mkdtemp(*args, **kwargs):
        nonlocal touched
        touched = True
        raise AssertionError("temp allocation must not happen")

    monkeypatch.setattr("tempfile.mkdtemp", forbidden_mkdtemp)
    with pytest.raises(Phase1ContractError, match="fixed internal"):
        IsolatedOllamaServer(tmp_path / "missing.exe", user_overrides={"ollama_host": "hostile"})
    assert touched is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"OLLAMA_API_KEY": "secret"}, "not an allowed"),
        ({"ollama_keep_alive": "5m"}, "not an allowed"),
        ({"OLLAMA_FLASH_ATTENTION": "sometimes"}, "FLASH_ATTENTION"),
        ({"OLLAMA_KV_CACHE_TYPE": "q2"}, "KV_CACHE"),
        ({"OLLAMA_CONTEXT_LENGTH": "0"}, "CONTEXT_LENGTH"),
        ({"OLLAMA_KEEP_ALIVE": "5m;whoami"}, "KEEP_ALIVE"),
    ],
)
def test_user_override_validation_is_typed(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(Phase1ContractError, match=message):
        validate_user_overrides(overrides)


def test_child_environment_is_built_from_empty_and_excludes_secrets(tmp_path: Path) -> None:
    executable = tmp_path / "install" / "ollama.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"fixture")
    space = create_session_space(tmp_path)
    try:
        inherited = {
            "SystemRoot": r"C:\Windows",
            "SystemDrive": "C:",
            "HTTP_PROXY": "http://secret-proxy",
            "HTTPS_PROXY": "https://secret-proxy",
            "OLLAMA_HOST": "hostile",
            "OLLAMA_API_KEY": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
        }
        env = build_child_environment(
            executable=executable,
            space=space,
            endpoint_port=54321,
            user_overrides={"OLLAMA_KEEP_ALIVE": "0"},
            parent_environment=inherited,
        )
        assert set(env) == FIXED_INTERNAL_ENV_KEYS | {"OLLAMA_KEEP_ALIVE"}
        assert env["OLLAMA_HOST"] == "127.0.0.1:54321"
        assert env["OLLAMA_KEEP_ALIVE"] == "0"
        assert not ({"HTTP_PROXY", "HTTPS_PROXY", "OLLAMA_API_KEY", "AWS_SECRET_ACCESS_KEY"} & set(env))
        assert all("secret-proxy" not in value and value != "secret" for value in env.values())
    finally:
        teardown_session_space(space)


# ---------------------------------------------------------------------------
# Version normalization (unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.32.1", "0.32.1"),
        ("v0.32.1", "0.32.1"),
        ("ollama version is 0.32.1", "0.32.1"),
        (b"ollama version 1.2.3-RC.1+WIN", "1.2.3-rc.1+win"),
    ],
)
def test_version_normalization(raw: str | bytes, expected: str) -> None:
    assert normalize_ollama_version(raw) == expected


@pytest.mark.parametrize("raw", ["", "0.32", "01.2.3", "version=0.32.1", "0.32.1\nnoise", b"\xff"])
def test_version_normalization_is_strict(raw: str | bytes) -> None:
    with pytest.raises(ValueError):
        normalize_ollama_version(raw)


def test_hash_executable_persists_only_basename_and_sha256(tmp_path: Path) -> None:
    executable = tmp_path / "private" / "ollama.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    basename, digest = hash_executable(executable)
    assert basename == "ollama.exe"
    assert digest == hashlib.sha256(b"synthetic executable").hexdigest()
    assert str(tmp_path) not in basename


# ---------------------------------------------------------------------------
# Dialects: startup, version-output, and the combined registry entry
# ---------------------------------------------------------------------------


def _identity(api_version: str = "0.32.1") -> RuntimeIdentity:
    return RuntimeIdentity("ollama.exe", "a" * 64, api_version, api_version, api_version)


def _dialect(identity: RuntimeIdentity, dialect: int = 1) -> StartupDialect:
    if dialect == 1:
        return StartupDialect(
            identity.executable_sha256,
            re.compile(r"server_version=(\S+)"),
            {
                "noprune": (re.compile(r"noprune=(\S+)"), "startup_log"),
                "no_cloud": (re.compile(r"no_cloud=(\S+)"), "startup_log"),
            },
        )
    return StartupDialect(
        identity.executable_sha256,
        re.compile(r'"version":"([^"]+)"'),
        {
            "noprune": (re.compile(r'"OLLAMA_NOPRUNE":"([^"]+)"'), "startup_log"),
            "no_cloud": (re.compile(r'"OLLAMA_NO_CLOUD":"([^"]+)"'), "startup_log"),
            "flash_attention": (re.compile(r"--flash-attn\s+(\S+)"), "runner_log"),
            "kv_cache_type": (re.compile(r"--cache-type-k\s+(\S+)"), "runner_log"),
        },
    )


def _version_output_dialect(digest: str, *, lone_server: bool = True) -> VersionOutputDialect:
    return VersionOutputDialect(
        digest,
        re.compile(r"ollama version is (\S+)"),
        re.compile(r"Warning: client version is (\S+)"),
        "could not connect to a running Ollama instance",
        lone_server,
    )


@pytest.mark.parametrize(
    ("startup", "settings", "message"),
    [
        (
            "not-compiled",
            {
                "noprune": (re.compile(r"noprune=(\S+)"), "startup_log"),
                "no_cloud": (re.compile(r"no_cloud=(\S+)"), "startup_log"),
            },
            "compiled",
        ),
        (
            re.compile(r"version=(\S+)"),
            {"noprune": (re.compile(r"noprune=(\S+)"), "startup_log")},
            "missing",
        ),
        (
            re.compile(r"version=(\S+)"),
            {
                "noprune": (re.compile(r"noprune=(\S+)"), "startup_log"),
                "no_cloud": (re.compile(r"no_cloud=(\S+)"), "startup_log"),
                "secret_path": (re.compile(r"secret=(\S+)"), "startup_log"),
            },
            "unknown",
        ),
        (
            re.compile(r"version=(\S+)"),
            {
                "noprune": (re.compile(r"noprune=(\S+)"), "arbitrary"),
                "no_cloud": (re.compile(r"no_cloud=(\S+)"), "startup_log"),
            },
            "source",
        ),
        (
            re.compile(r"version=(\S+)"),
            {
                "noprune": (re.compile("(" + "x" * 600 + ")"), "startup_log"),
                "no_cloud": (re.compile(r"no_cloud=(\S+)"), "startup_log"),
            },
            "length",
        ),
    ],
)
def test_startup_dialect_is_closed_and_validated(
    startup: object, settings: dict[str, tuple[object, str]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        StartupDialect("a" * 64, startup, settings)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("server", "client", "marker", "message"),
    [
        ("not-compiled", re.compile(r"(\S+)"), "warn", "compiled"),
        (re.compile(r"(\S+)(\S+)"), re.compile(r"(\S+)"), "warn", "capture group"),
        (re.compile(r"(\S+)"), re.compile(r"(\S+)"), "", "marker"),
    ],
)
def test_version_output_dialect_is_validated(server: object, client: object, marker: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        VersionOutputDialect("a" * 64, server, client, marker, True)  # type: ignore[arg-type]


def test_reviewed_dialect_entry_requires_matching_identity_across_both_dialects() -> None:
    identity = _identity()
    startup = _dialect(identity, 1)
    mismatched_version_output = _version_output_dialect("b" * 64)
    with pytest.raises(ValueError, match="does not match"):
        ReviewedDialectEntry(identity.executable_sha256, startup, mismatched_version_output)


# ---------------------------------------------------------------------------
# parse_startup_attestation / parse_version_output
# ---------------------------------------------------------------------------


def test_attestation_parser_supports_fixture_dialect_one() -> None:
    identity = _identity()
    startup_record, settings = parse_startup_attestation(
        b"server_version=0.32.1 noprune=true no_cloud=true",
        identity=identity,
        dialect=_dialect(identity, 1),
    )
    assert startup_record == {"state": "attested", "value": "0.32.1", "source": "startup_log"}
    assert settings["noprune"] == {"state": "attested", "value": "true", "source": "startup_log"}


def test_attestation_parser_supports_fixture_dialect_two() -> None:
    identity = _identity()
    _, settings = parse_startup_attestation(
        b'{"version":"0.32.1","OLLAMA_NOPRUNE":"1"}\nrunner --flash-attn on --cache-type-k q8_0',
        identity=identity,
        dialect=_dialect(identity, 2),
    )
    assert settings["flash_attention"]["value"] == "on"
    assert settings["kv_cache_type"]["value"] == "q8_0"


def test_unparseable_startup_version_is_typed_unattested_not_failure() -> None:
    identity = _identity()
    startup_record, settings = parse_startup_attestation(
        b"a future log dialect with no stable version field",
        identity=identity,
        dialect=_dialect(identity, 1),
    )
    assert startup_record == {"state": "unattested", "value": None, "source": "unattested"}
    assert all(record["state"] == "unattested" for record in settings.values())


def test_startup_version_disagreement_with_bound_identity_fails_closed() -> None:
    identity = _identity(api_version="0.32.1")
    with pytest.raises(IsolatedServerFailure, match="runtime_identity_mismatch"):
        parse_startup_attestation(
            b"server_version=0.32.2 noprune=true no_cloud=true",
            identity=identity,
            dialect=_dialect(identity, 1),
        )


def test_parse_version_output_lone_server_line_attests_client() -> None:
    dialect = _version_output_dialect("a" * 64)
    client, server = parse_version_output(b"ollama version is 0.32.1\n", dialect)
    assert client == server == "0.32.1"


def test_parse_version_output_explicit_client_warning_line() -> None:
    dialect = _version_output_dialect("a" * 64)
    client, server = parse_version_output(
        b"ollama version is 0.32.1\nWarning: client version is 0.32.0\n", dialect
    )
    assert server == "0.32.1"
    assert client == "0.32.0"


def test_parse_version_output_connection_warning_marker_is_ownership_failure() -> None:
    dialect = _version_output_dialect("a" * 64)
    with pytest.raises(IsolatedServerFailure, match="version_endpoint_ownership_failed"):
        parse_version_output(
            b"Warning: could not connect to a running Ollama instance\n"
            b"Warning: client version is 0.32.1\n",
            dialect,
        )


def test_parse_version_output_missing_server_line_is_malformed() -> None:
    dialect = _version_output_dialect("a" * 64)
    with pytest.raises(IsolatedServerFailure, match="version_output_malformed"):
        parse_version_output(b"Warning: client version is 0.32.1\n", dialect)


def test_parse_version_output_missing_client_line_without_lone_rule_is_malformed() -> None:
    dialect = _version_output_dialect("a" * 64, lone_server=False)
    with pytest.raises(IsolatedServerFailure, match="version_output_malformed"):
        parse_version_output(b"ollama version is 0.32.1\n", dialect)


def test_parse_version_output_unexpected_line_is_malformed() -> None:
    dialect = _version_output_dialect("a" * 64)
    with pytest.raises(IsolatedServerFailure, match="version_output_malformed"):
        parse_version_output(
            b"ollama version is 0.32.1\nsome unexpected diagnostic line\n", dialect
        )


def test_parse_version_output_oversized_is_malformed() -> None:
    dialect = _version_output_dialect("a" * 64)
    with pytest.raises(IsolatedServerFailure, match="version_output_malformed"):
        parse_version_output(b"x" * (isolated_server.MAX_VERSION_OUTPUT_BYTES + 1), dialect)


def test_requested_settings_require_attestation_and_must_match() -> None:
    unattested = {
        key: {"state": "unattested", "value": None, "source": "unattested"}
        for key in {"noprune", "no_cloud", "flash_attention", "kv_cache_type", "keep_alive", "context_length"}
    }
    assert [failure.category for failure in compare_requested_attestation({}, unattested)] == ["attestation_missing"]
    attested = {key: dict(value) for key, value in unattested.items()}
    attested["noprune"] = {"state": "attested", "value": "1", "source": "startup_log"}
    attested["no_cloud"] = {"state": "attested", "value": "true", "source": "startup_log"}
    attested["flash_attention"] = {"state": "attested", "value": "off", "source": "runner_log"}
    failures = compare_requested_attestation({"OLLAMA_FLASH_ATTENTION": "1"}, attested)
    assert [failure.category for failure in failures] == ["attestation_mismatch"]


# ---------------------------------------------------------------------------
# Bounded capture (single-threaded; no reader-failure surface any more)
# ---------------------------------------------------------------------------


def test_bounded_log_capture_keeps_first_and_last_windows() -> None:
    capture = BoundedLogCapture()
    data = b"a" * FIRST_LOG_BYTES + b"middle" * 100_000 + b"z" * LAST_LOG_BYTES
    capture.feed(data)
    stored = capture.bytes()
    assert len(stored) == FIRST_LOG_BYTES + LAST_LOG_BYTES
    assert stored[:FIRST_LOG_BYTES] == b"a" * FIRST_LOG_BYTES
    assert stored[-LAST_LOG_BYTES:] == b"z" * LAST_LOG_BYTES
    assert capture.truncated is True
    assert capture.evidence()["bytes_observed"] == len(data)


def test_bounded_version_capture_retains_at_most_four_kibibytes() -> None:
    capture = BoundedVersionCapture()
    capture.feed(b"x" * 100_000)
    assert len(capture.bytes()) == 4096
    assert capture.observed == 100_000
    assert capture.overflowed is True


# ---------------------------------------------------------------------------
# Session space + port selection (unchanged)
# ---------------------------------------------------------------------------


def test_temp_space_lifecycle_uses_only_supplied_parent(tmp_path: Path) -> None:
    space = create_session_space(tmp_path)
    assert space.root.parent == tmp_path
    assert space.profile.is_dir()
    assert space.model_store.is_dir()
    assert list(space.model_store.iterdir()) == []
    teardown_session_space(space)
    assert not space.root.exists()


def test_temp_space_teardown_failure_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = create_session_space(tmp_path)
    monkeypatch.setattr("shutil.rmtree", lambda _: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(IsolatedServerFailure, match="teardown_incomplete"):
        teardown_session_space(space)


def test_loopback_port_probe_honors_exclusion() -> None:
    first = choose_loopback_port(set())
    second = choose_loopback_port({first})
    assert 1 <= first <= 65535
    assert second != first


# ---------------------------------------------------------------------------
# Address-aware TCP ownership (correction 2, §3.3) — pure-function level
# ---------------------------------------------------------------------------


def test_classify_relevant_listener_rows_excludes_unrelated_interfaces() -> None:
    rows = (
        TcpListenerRow("127.0.0.1", 5000, 100),
        TcpListenerRow("10.0.0.5", 5000, 200),
        TcpListenerRow("127.0.0.1", 5001, 300),
    )
    relevant = classify_relevant_listener_rows(rows, 5000)
    assert relevant == (TcpListenerRow("127.0.0.1", 5000, 100),)


def test_classify_relevant_listener_rows_treats_wildcard_as_relevant() -> None:
    rows = (TcpListenerRow("0.0.0.0", 5000, 100),)
    assert classify_relevant_listener_rows(rows, 5000) == rows


class _StubOwnershipApi:
    def __init__(self, rows: tuple[TcpListenerRow, ...], *, in_job: bool = False) -> None:
        self.rows = rows
        self.in_job = in_job

    def tcp_listener_rows(self) -> tuple[TcpListenerRow, ...]:
        return self.rows

    def process_id_in_job(self, job: object, process_id: int) -> bool:
        return self.in_job


def test_resolve_endpoint_ownership_wildcard_plus_loopback_is_ambiguous() -> None:
    rows = (TcpListenerRow("127.0.0.1", 5000, 1), TcpListenerRow("0.0.0.0", 5000, 2))
    result = resolve_endpoint_ownership(_StubOwnershipApi(rows), 5000, process=None, job=None)
    assert result.state == OWNERSHIP_AMBIGUOUS


def test_resolve_endpoint_ownership_multiple_relevant_owners_is_ambiguous() -> None:
    rows = (TcpListenerRow("127.0.0.1", 5000, 1), TcpListenerRow("127.0.0.1", 5000, 2))
    result = resolve_endpoint_ownership(_StubOwnershipApi(rows), 5000, process=None, job=None)
    assert result.state == OWNERSHIP_AMBIGUOUS


def test_resolve_endpoint_ownership_not_present_when_no_relevant_rows() -> None:
    result = resolve_endpoint_ownership(_StubOwnershipApi(()), 5000, process=None, job=None)
    assert result.state == OWNERSHIP_NOT_PRESENT


def test_resolve_endpoint_ownership_foreign_captures_owner_pid() -> None:
    rows = (TcpListenerRow("127.0.0.1", 5000, 42),)
    result = resolve_endpoint_ownership(_StubOwnershipApi(rows, in_job=False), 5000, process=None, job="job")
    assert result.state == OWNERSHIP_FOREIGN
    assert result.foreign_owner_process_id == 42


def test_resolve_endpoint_ownership_job_member_counts_as_owned() -> None:
    rows = (TcpListenerRow("127.0.0.1", 5000, 42),)
    result = resolve_endpoint_ownership(_StubOwnershipApi(rows, in_job=True), 5000, process=None, job="job")
    assert result.state == OWNERSHIP_OWNED


# ---------------------------------------------------------------------------
# Stable foreign-owner identity (correction 2, §3.3)
# ---------------------------------------------------------------------------


class _IdentityApi:
    def __init__(self) -> None:
        self.opened: list[int] = []
        self.closed: list[str] = []
        self.creation_time_fails = False

    def open_limited_process_handle(self, process_id: int) -> str:
        self.opened.append(process_id)
        return f"handle-{process_id}"

    def process_creation_time(self, handle: str) -> int:
        if self.creation_time_fails:
            raise IsolatedServerFailure("owner_identity_unavailable")
        return 12345

    def close_handle(self, handle: str) -> None:
        self.closed.append(handle)


def test_capture_foreign_owner_identity_holds_handle_and_creation_time() -> None:
    api = _IdentityApi()
    identity = capture_foreign_owner_identity(api, 99)
    assert identity.process_id == 99
    assert identity.creation_time == 12345
    assert identity.handle == "handle-99"
    assert api.opened == [99]
    assert api.closed == []


def test_capture_foreign_owner_identity_closes_handle_on_creation_time_failure() -> None:
    api = _IdentityApi()
    api.creation_time_fails = True
    with pytest.raises(IsolatedServerFailure, match="owner_identity_unavailable"):
        capture_foreign_owner_identity(api, 99)
    assert api.closed == ["handle-99"]


# ---------------------------------------------------------------------------
# WindowsLifecycleApi ctypes fixtures: attribute list + handle inheritance
# (unaffected by revision 6 beyond the pipe mechanism, kept as regression
# coverage for the exact three-handle inheritance list).
# ---------------------------------------------------------------------------


class FakeAttributeKernel:
    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.fail_stage = fail_stage
        self.initialize_calls = 0
        self.updated_handles: tuple[int, ...] = ()
        self.deleted = 0
        self.null_opened = False

    def CreateFileW(self, name, access, share, attrs, disposition, flags, template) -> int:
        self.null_opened = True
        if self.fail_stage == "null_open":
            isolated_server.ctypes.set_last_error(5)
            return isolated_server.ctypes.c_void_p(-1).value
        security = isolated_server.ctypes.cast(
            attrs, isolated_server.ctypes.POINTER(isolated_server._WinSecurityAttributes)
        ).contents
        assert name == "NUL"
        assert access == WindowsLifecycleApi.GENERIC_READ
        assert security.bInheritHandle == 1
        return 10

    def InitializeProcThreadAttributeList(self, target, count, flags, size_pointer) -> int:
        self.initialize_calls += 1
        if self.initialize_calls == 1:
            ctypes_size = isolated_server.ctypes.cast(
                size_pointer, isolated_server.ctypes.POINTER(isolated_server.ctypes.c_size_t)
            )
            ctypes_size[0] = 128
            isolated_server.ctypes.set_last_error(122)
            return 0
        return 0 if self.fail_stage == "initialize" else 1

    def UpdateProcThreadAttribute(self, target, flags, attribute, value, size, previous, return_size) -> int:
        handle_array = isolated_server.ctypes.cast(
            value, isolated_server.ctypes.POINTER(isolated_server.ctypes.c_void_p * 3)
        ).contents
        self.updated_handles = tuple(int(handle or 0) for handle in handle_array)
        return 0 if self.fail_stage == "update" else 1

    def DeleteProcThreadAttributeList(self, target) -> None:
        self.deleted += 1
        if self.fail_stage == "delete":
            raise OSError("synthetic attribute cleanup failure")


def _attribute_api(kernel: FakeAttributeKernel) -> WindowsLifecycleApi:
    api = object.__new__(WindowsLifecycleApi)
    api.kernel32 = kernel
    return api


def test_startupinfoex_inherits_only_null_stdin_stdout_and_stderr_handles() -> None:
    kernel = FakeAttributeKernel()
    api = _attribute_api(kernel)
    stdin_read = api._open_null_stdin()
    attributes = api._create_attribute_list(stdin_read, 11, 12)
    api._delete_attribute_list(attributes)
    assert kernel.updated_handles == (10, 11, 12)
    assert 0 not in kernel.updated_handles
    assert 999 not in kernel.updated_handles
    assert kernel.null_opened is True
    assert kernel.deleted == 1


@pytest.mark.parametrize("stage", ["initialize", "update"])
def test_attribute_list_api_failures_are_closed(stage: str) -> None:
    api = _attribute_api(FakeAttributeKernel(fail_stage=stage))
    with pytest.raises(IsolatedServerFailure, match="process_attribute_list_failed"):
        api._create_attribute_list(10, 11, 12)


def test_null_stdin_open_failure_is_closed() -> None:
    api = _attribute_api(FakeAttributeKernel(fail_stage="null_open"))
    with pytest.raises(IsolatedServerFailure, match="process_create_failed"):
        api._open_null_stdin()


def test_attribute_list_cleanup_failure_is_closed() -> None:
    api = _attribute_api(FakeAttributeKernel(fail_stage="delete"))
    attributes = api._create_attribute_list(10, 11, 12)
    with pytest.raises(IsolatedServerFailure, match="process_attribute_list_cleanup_failed"):
        api._delete_attribute_list(attributes)


# ---------------------------------------------------------------------------
# Overlapped pipe creation and cancellation (correction 3, §5) — ctypes level
# ---------------------------------------------------------------------------


class OverlappedPipeKernel:
    def __init__(self) -> None:
        self.pipe_flags = None
        self.pipe_mode = None
        self.connect_called = False
        self.client_access = None
        self.security_inherit = None

    def CreateNamedPipeW(self, name, open_mode, pipe_mode, max_instances, out_buf, in_buf, timeout, attrs) -> int:
        self.pipe_flags = open_mode
        self.pipe_mode = pipe_mode
        assert name.startswith("\\\\.\\pipe\\odysseus-bench-")
        assert attrs is None
        return 50

    def CreateEventW(self, attrs, manual_reset, initial_state, name) -> int:
        assert manual_reset is True
        assert initial_state is False
        return 51

    def CreateFileW(self, name, access, share, attrs, disposition, flags, template) -> int:
        self.client_access = access
        security = isolated_server.ctypes.cast(
            attrs, isolated_server.ctypes.POINTER(isolated_server._WinSecurityAttributes)
        ).contents
        self.security_inherit = security.bInheritHandle
        return 52

    def ConnectNamedPipe(self, handle, overlapped) -> int:
        self.connect_called = True
        isolated_server.ctypes.set_last_error(WindowsLifecycleApi.ERROR_PIPE_CONNECTED)
        return 0

    def CloseHandle(self, handle) -> int:
        return 1


def test_overlapped_pipe_uses_single_instance_inbound_flags_and_inheritable_child_handle() -> None:
    kernel = OverlappedPipeKernel()
    api = object.__new__(WindowsLifecycleApi)
    api.kernel32 = kernel
    pipe, client_handle = api._create_overlapped_pipe()
    expected_open = (
        WindowsLifecycleApi.PIPE_ACCESS_INBOUND
        | WindowsLifecycleApi.FILE_FLAG_OVERLAPPED
        | WindowsLifecycleApi.FILE_FLAG_FIRST_PIPE_INSTANCE
    )
    expected_mode = (
        WindowsLifecycleApi.PIPE_TYPE_BYTE
        | WindowsLifecycleApi.PIPE_READMODE_BYTE
        | WindowsLifecycleApi.PIPE_WAIT
        | WindowsLifecycleApi.PIPE_REJECT_REMOTE_CLIENTS
    )
    assert kernel.pipe_flags == expected_open
    assert kernel.pipe_mode == expected_mode
    assert kernel.connect_called is True
    assert kernel.client_access == WindowsLifecycleApi.GENERIC_WRITE
    assert kernel.security_inherit == 1
    assert client_handle == 52
    assert pipe.handle == 50
    assert pipe.event == 51


def test_cancel_overlapped_read_targets_exact_pipe_and_operation() -> None:
    class CancelKernel:
        def __init__(self) -> None:
            self.cancelled_handle = None

        def CancelIoEx(self, handle, overlapped) -> int:
            self.cancelled_handle = handle
            return 1

    kernel = CancelKernel()
    api = object.__new__(WindowsLifecycleApi)
    api.kernel32 = kernel
    pipe = isolated_server._OverlappedPipe(
        handle=77, event=78, overlapped=isolated_server._WinOverlapped(), buffer=None, pending=True
    )
    api.cancel_overlapped_read(pipe)
    assert kernel.cancelled_handle == 77


def test_cancel_overlapped_read_treats_already_complete_as_success() -> None:
    class CancelKernel:
        def CancelIoEx(self, handle, overlapped) -> int:
            isolated_server.ctypes.set_last_error(WindowsLifecycleApi.ERROR_NOT_FOUND)
            return 0

    api = object.__new__(WindowsLifecycleApi)
    api.kernel32 = CancelKernel()
    pipe = isolated_server._OverlappedPipe(
        handle=1, event=2, overlapped=isolated_server._WinOverlapped(), buffer=None, pending=True
    )
    api.cancel_overlapped_read(pipe)  # must not raise


def test_cancel_overlapped_read_failure_is_io_cancellation_failed() -> None:
    class CancelKernel:
        def CancelIoEx(self, handle, overlapped) -> int:
            isolated_server.ctypes.set_last_error(5)
            return 0

    api = object.__new__(WindowsLifecycleApi)
    api.kernel32 = CancelKernel()
    pipe = isolated_server._OverlappedPipe(
        handle=1, event=2, overlapped=isolated_server._WinOverlapped(), buffer=None, pending=True
    )
    with pytest.raises(IsolatedServerFailure, match="io_cancellation_failed"):
        api.cancel_overlapped_read(pipe)


class FakeCreateProcessKernel:
    def __init__(self) -> None:
        self.startup_handles: tuple[int, int, int] | None = None

    def CreateProcessW(
        self, executable, command, process_attrs, thread_attrs, inherit, flags,
        environment, cwd, startup_pointer, process_pointer
    ) -> int:
        startup = isolated_server.ctypes.cast(
            startup_pointer, isolated_server.ctypes.POINTER(isolated_server._WinStartupInfoEx)
        ).contents.StartupInfo
        self.startup_handles = (
            int(startup.hStdInput or 0),
            int(startup.hStdOutput or 0),
            int(startup.hStdError or 0),
        )
        process = isolated_server.ctypes.cast(
            process_pointer, isolated_server.ctypes.POINTER(isolated_server._WinProcessInformation)
        ).contents
        process.hProcess = 30
        process.hThread = 31
        process.dwProcessId = 32
        return 1


def _create_process_api(*, fail_null_close: bool = False):
    api = object.__new__(WindowsLifecycleApi)
    api.kernel32 = FakeCreateProcessKernel()
    pipe_objs = iter((object(), object()))
    writes = iter((11, 12))
    closed: list[int] = []
    closed_pipes: list[object] = []
    aborted: list[bool] = []
    api._open_null_stdin = lambda: 10
    api._create_overlapped_pipe = lambda: (next(pipe_objs), next(writes))
    api._create_attribute_list = lambda stdin, stdout, stderr: (
        isolated_server._ProcessAttributeList(
            None, isolated_server.ctypes.c_void_p(99), (stdin, stdout, stderr),
        )
    )
    api._delete_attribute_list = lambda attributes: None

    def close_handle(handle) -> None:
        value = int(handle.value) if hasattr(handle, "value") else int(handle)
        closed.append(value)
        if fail_null_close and value == 10:
            raise IsolatedServerFailure("teardown_incomplete")

    api.close_handle = close_handle
    api.close_pipe = lambda pipe: closed_pipes.append(pipe)
    api._abort_created_suspended = lambda process: aborted.append(True)
    return api, closed, closed_pipes, aborted


def test_create_process_uses_matching_three_handle_stdio_and_closes_child_ends() -> None:
    api, closed, closed_pipes, aborted = _create_process_api()
    process = api.create_suspended(Path("C:/fixture/ollama.exe"), ("--version",), {})
    assert api.kernel32.startup_handles == (10, 11, 12)
    assert closed == [10, 11, 12]
    assert aborted == []
    assert process.process_id == 32
    assert closed_pipes == []


def test_null_parent_handle_close_failure_aborts_suspended_child_and_is_closed() -> None:
    api, closed, closed_pipes, aborted = _create_process_api(fail_null_close=True)
    with pytest.raises(IsolatedServerFailure, match="process_attribute_list_cleanup_failed"):
        api.create_suspended(Path("C:/fixture/ollama.exe"), ("serve",), {})
    assert closed == [10, 11, 12]
    assert aborted == [True]
    assert len(closed_pipes) == 2


# ---------------------------------------------------------------------------
# Synthetic fixture lifecycle API: FakePipe delivers scripted overlapped-read
# events, FakeLifecycleApi wires up address-aware TCP ownership and a stable
# foreign-owner identity.  Everything is single-threaded and deterministic —
# consistent with the branch's synthetic-only validation rule.
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


class FakePipe:
    """Scripted overlapped pipe.  ``events`` is consumed one entry per
    completed read; an empty ``events`` list while ``pending`` is True means
    the read is genuinely outstanding until the test supplies more."""

    def __init__(self, events=None, *, hang: bool = False) -> None:
        self.events: list[tuple[str, bytes]] = list(events) if events else []
        self.hang = hang
        self.pending = False
        self.eof = False
        self.closed = False
        self.close_raises = False
        self.cancel_calls = 0


class FakeLifecycleApi:
    def __init__(
        self,
        *,
        server_log: bytes = b"server_version=0.32.1 noprune=true no_cloud=true",
        version_output: bytes = b"ollama version is 0.32.1\n",
        server_descendants: set[int] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.server_log = server_log
        self.version_output = version_output
        self.server_descendants = set() if server_descendants is None else server_descendants
        self.environments: list[dict[str, str]] = []
        self.arguments: list[tuple[str, ...]] = []
        self.process_kind: dict[int, str] = {}
        self.processes: dict[int, CreatedProcess] = {}
        self.resumed: set[int] = set()
        self.terminated: set[int] = set()
        self.job_process: dict[str, int] = {}
        self.next_id = 4200
        self.active_server_id: int | None = None
        self.active_server_port: int | None = None
        self.creation_times: dict[int, int] = {}
        self._creation_counter = 1000
        self.signaled_pids: set[int] = set()
        self.identity_open_failures: set[int] = set()
        self.creation_time_failures: set[int] = set()
        self.cancel_failures: set[FakePipe] = set()
        self.clock: FakeClock | None = None

    def _kind(self, process: CreatedProcess) -> str:
        return self.process_kind[process.process_id]

    def create_suspended(self, executable: Path, arguments: tuple[str, ...], environment: dict[str, str]) -> CreatedProcess:
        kind = "version" if arguments == ("--version",) else "server"
        self.calls.append(f"create_suspended_{kind}")
        assert set(environment) == FIXED_INTERNAL_ENV_KEYS
        self.environments.append(dict(environment))
        self.arguments.append(arguments)
        self.next_id += 1
        process_id = self.next_id
        output = self.version_output if kind == "version" else self.server_log
        stdout = FakePipe(events=[("data", output), ("eof", b"")] if output else [("eof", b"")])
        stderr = FakePipe(events=[("eof", b"")])
        process = CreatedProcess(process_id, f"process-{process_id}", f"thread-{process_id}", stdout, stderr)
        self.process_kind[process_id] = kind
        self.processes[process_id] = process
        if kind == "server":
            self.active_server_id = process_id
            self.active_server_port = int(environment["OLLAMA_HOST"].rsplit(":", 1)[1])
        return process

    def process_image_matches(self, process: CreatedProcess, executable: Path) -> bool:
        self.calls.append(f"process_image_{self._kind(process)}")
        return True

    def create_job(self) -> str:
        job = f"job-{len(self.job_process) + 1}"
        self.calls.append("create_job")
        return job

    def configure_kill_on_close(self, job: str) -> None:
        self.calls.append("configure_job")

    def assign_process(self, job: str, process: CreatedProcess) -> None:
        self.calls.append("assign_process")
        self.job_process[job] = process.process_id

    def verify_job_assignment(self, job: str, process: CreatedProcess) -> bool:
        self.calls.append("verify_job")
        return self.job_process.get(job) == process.process_id

    def resume_process(self, process: CreatedProcess) -> None:
        self.calls.append(f"resume_{self._kind(process)}")
        self.resumed.add(process.process_id)

    def process_exit_code(self, process: CreatedProcess) -> int | None:
        self.calls.append(f"process_exit_{self._kind(process)}")
        if process.process_id in self.terminated:
            return 0
        if self._kind(process) == "version" and process.process_id in self.resumed:
            return 0
        return None

    def tcp_listener_rows(self) -> tuple[TcpListenerRow, ...]:
        self.calls.append("tcp_listener_rows")
        if self.active_server_id is None or self.active_server_id in self.terminated:
            return ()
        return (TcpListenerRow("127.0.0.1", self.active_server_port, self.active_server_id),)

    def process_id_in_job(self, job: str, process_id: int) -> bool:
        self.calls.append("process_id_in_job")
        return self.job_process.get(job) == process_id

    def _next_creation_time(self, process_id: int) -> int:
        self._creation_counter += 1
        return self._creation_counter

    def open_limited_process_handle(self, process_id: int) -> str:
        self.calls.append("open_limited_process_handle")
        if process_id in self.identity_open_failures:
            raise IsolatedServerFailure("owner_identity_unavailable")
        return f"identity-handle-{process_id}"

    def process_creation_time(self, handle: str) -> int:
        self.calls.append("process_creation_time")
        pid = int(str(handle).rsplit("-", 1)[-1])
        if pid in self.creation_time_failures:
            raise IsolatedServerFailure("owner_identity_unavailable")
        return self.creation_times.setdefault(pid, self._next_creation_time(pid))

    def query_pid_creation_time(self, process_id: int) -> int:
        self.calls.append("query_pid_creation_time")
        if process_id in self.creation_time_failures:
            raise IsolatedServerFailure("owner_identity_unavailable")
        return self.creation_times.setdefault(process_id, self._next_creation_time(process_id))

    def handle_signaled(self, handle: str) -> bool:
        self.calls.append("handle_signaled")
        pid = int(str(handle).rsplit("-", 1)[-1])
        return pid in self.signaled_pids

    def terminate_job(self, job: str) -> None:
        self.calls.append("terminate_job")
        process_id = self.job_process[job]
        self.terminated.add(process_id)

    def terminate_process(self, process: CreatedProcess) -> None:
        self.calls.append("terminate_process")
        self.terminated.add(process.process_id)

    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool:
        self.calls.append("wait_process")
        return process.process_id in self.terminated or self._kind(process) == "version"

    def descendant_process_ids(self, process_id: int) -> set[int]:
        self.calls.append("descendant_process_ids")
        if self.process_kind[process_id] == "server":
            return set(self.server_descendants)
        return set()

    def post_overlapped_read(self, pipe: FakePipe) -> None:
        self.calls.append("post_overlapped_read")
        if pipe.eof or pipe.pending:
            return
        pipe.pending = True

    def finish_overlapped_read(self, pipe: FakePipe) -> tuple[str, bytes]:
        self.calls.append("finish_overlapped_read")
        if pipe.eof:
            return ("eof", b"")
        if not pipe.pending:
            return ("idle", b"")
        if not pipe.events:
            return ("pending", b"")
        status, data = pipe.events.pop(0)
        pipe.pending = False
        if status == "eof":
            pipe.eof = True
        return (status, data)

    def cancel_overlapped_read(self, pipe: FakePipe) -> None:
        self.calls.append("cancel_overlapped_read")
        pipe.cancel_calls += 1
        if pipe in self.cancel_failures:
            raise IsolatedServerFailure("io_cancellation_failed", win32_code=6)
        if pipe.pending and not pipe.hang:
            pipe.events.insert(0, ("aborted", b""))

    def wait_for_completion(self, pipes: tuple, process: CreatedProcess | None, timeout_ms: int) -> None:
        self.calls.append("wait_for_completion")
        if self.clock is not None:
            self.clock.sleep(timeout_ms / 1000.0)

    def close_pipe(self, pipe: FakePipe) -> None:
        self.calls.append("close_pipe")
        pipe.closed = True
        if pipe.close_raises:
            raise IsolatedServerFailure("teardown_incomplete")

    def close_handle(self, handle: str) -> None:
        self.calls.append(f"close_{handle}")


# ---------------------------------------------------------------------------
# run_owned_version_probe (§2.6 command-version child) unit tests
# ---------------------------------------------------------------------------


def _owned_version_fixture(tmp_path: Path, api: FakeLifecycleApi, *, clock=None) -> bytes:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"owned version fixture")
    basename, digest = hash_executable(executable)
    space = create_session_space(tmp_path)
    try:
        environment = build_child_environment(
            executable=executable,
            space=space,
            endpoint_port=51234,
            user_overrides={},
            parent_environment={
                "SystemRoot": r"C:\Windows",
                "SystemDrive": "C:",
                "HTTP_PROXY": "http://secret-proxy",
                "OLLAMA_API_KEY": "secret",
            },
        )
        return run_owned_version_probe(
            executable=executable,
            environment=environment,
            api=api,
            expected_basename=basename,
            expected_sha256=digest,
            timeout_seconds=0.2,
            cleanup_timeout_seconds=0.2,
            clock=time.monotonic if clock is None else clock,
        )
    finally:
        teardown_session_space(space)


def test_version_probe_is_job_owned_environment_isolated_and_bounded(tmp_path: Path) -> None:
    api = FakeLifecycleApi()
    output = _owned_version_fixture(tmp_path, api)
    assert output == b"ollama version is 0.32.1\n"
    assert api.arguments == [("--version",)]
    assert not ({"HTTP_PROXY", "OLLAMA_API_KEY"} & set(api.environments[0]))
    assert api.calls.index("create_suspended_version") < api.calls.index("create_job")
    assert api.calls.index("verify_job") < api.calls.index("resume_version")
    assert api.calls.index("terminate_job") < api.calls.index("descendant_process_ids")


def test_version_probe_rejects_finite_infinite_output_fixture(tmp_path: Path) -> None:
    api = FakeLifecycleApi(version_output=b"x" * 100_000)
    with pytest.raises(IsolatedServerFailure, match="version_probe_output_overflow"):
        _owned_version_fixture(tmp_path, api)


class VersionTimeoutApi(FakeLifecycleApi):
    def process_exit_code(self, process: CreatedProcess) -> int | None:
        if self._kind(process) == "version" and process.process_id not in self.terminated:
            return None
        return super().process_exit_code(process)


def test_version_probe_enforces_true_wall_clock_timeout(tmp_path: Path) -> None:
    clock = FakeClock()
    api = VersionTimeoutApi(version_output=b"")
    api.clock = clock
    with pytest.raises(IsolatedServerFailure, match="version_probe_timeout"):
        _owned_version_fixture(tmp_path, api, clock=clock)
    assert clock.now >= 0.2
    assert "terminate_job" in api.calls


class VersionDescendantApi(FakeLifecycleApi):
    def descendant_process_ids(self, process_id: int) -> set[int]:
        if self.process_kind[process_id] == "version":
            return {9999}
        return super().descendant_process_ids(process_id)


def test_version_probe_rejects_surviving_descendant(tmp_path: Path) -> None:
    with pytest.raises(IsolatedServerFailure, match="version_probe_cleanup_failed"):
        _owned_version_fixture(tmp_path, VersionDescendantApi())


class VersionCleanupFailureApi(FakeLifecycleApi):
    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool:
        if self._kind(process) == "version":
            return False
        return super().wait_process(process, timeout_ms)


def test_version_probe_failed_cleanup_is_closed(tmp_path: Path) -> None:
    with pytest.raises(IsolatedServerFailure, match="version_probe_cleanup_failed"):
        _owned_version_fixture(tmp_path, VersionCleanupFailureApi())


# ---------------------------------------------------------------------------
# cancel_pending_pipe_io (§5 cancellation sequence) unit tests
# ---------------------------------------------------------------------------


def test_pending_read_ignoring_cancellation_times_out_without_retry() -> None:
    api = FakeLifecycleApi()
    clock = FakeClock()
    api.clock = clock
    pipe = FakePipe(events=[], hang=True)
    pipe.pending = True
    failures = cancel_pending_pipe_io(
        api, (pipe,), deadline=clock() + 0.2, clock=clock, capture=None
    )
    assert [failure.category for failure in failures] == ["pending_io_cleanup_timeout"]
    assert pipe.cancel_calls == 1
    assert pipe in isolated_server._QUARANTINED_PENDING_IO


def test_cancel_pending_pipe_io_reports_cancellation_failure_and_quarantines_pipe() -> None:
    api = FakeLifecycleApi()
    pipe = FakePipe(events=[], hang=True)
    pipe.pending = True
    api.cancel_failures.add(pipe)
    clock = FakeClock()
    failures = cancel_pending_pipe_io(
        api, (pipe,), deadline=clock() + 1.0, clock=clock, capture=None
    )
    assert [failure.category for failure in failures] == ["io_cancellation_failed"]
    assert pipe in isolated_server._QUARANTINED_PENDING_IO


def test_cancel_pending_pipe_io_drains_and_closes_a_normally_completing_pipe() -> None:
    api = FakeLifecycleApi()
    pipe = FakePipe(events=[("data", b"tail"), ("eof", b"")])
    pipe.pending = True
    clock = FakeClock()
    captured = BoundedLogCapture()
    failures = cancel_pending_pipe_io(
        api, (pipe,), deadline=clock() + 1.0, clock=clock, capture=captured
    )
    assert failures == []
    assert pipe.closed is True
    assert captured.bytes() == b"tail"


# ---------------------------------------------------------------------------
# Full IsolatedOllamaServer lifecycle (§12 state machine)
# ---------------------------------------------------------------------------


def _register_test_dialect(
    monkeypatch: pytest.MonkeyPatch, executable: Path, dialect_number: int = 1
) -> RuntimeIdentity:
    basename, digest = hash_executable(executable)
    identity = RuntimeIdentity(basename, digest, "0.32.1", "0.32.1", "0.32.1")
    entry = ReviewedDialectEntry(
        digest, _dialect(identity, dialect_number), _version_output_dialect(digest)
    )
    monkeypatch.setattr(
        isolated_server, "REVIEWED_DIALECT_REGISTRY", MappingProxyType({digest: entry})
    )
    return identity


def test_synthetic_end_to_end_lifecycle_produces_private_closed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"synthetic ollama fixture")
    _register_test_dialect(monkeypatch, executable)
    api = FakeLifecycleApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert validate_attestation_artifact(artifact) == []
    assert artifact["schema_version"] == ATTESTATION_SCHEMA_VERSION
    assert artifact["artifact_kind"] == ATTESTATION_ARTIFACT_KIND
    assert artifact["runtime_identity"]["client_version"] == "0.32.1"
    assert artifact["runtime_identity"]["command_server_version"] == "0.32.1"
    assert artifact["runtime_identity"]["api_version"] == "0.32.1"
    assert artifact["runtime_identity"]["startup_version"]["state"] == "attested"
    assert artifact["overall_diagnostic_evidence_state"] == "complete"
    assert artifact["failures"] == []
    serialized = json.dumps(artifact, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert all(str(process_id) not in serialized for process_id in api.processes)
    assert "127.0.0.1" not in serialized
    assert "0.0.0.0" not in serialized
    assert "\\\\.\\pipe\\" not in serialized
    # Revision 6: the server launches first; the version child only runs
    # post-readiness against the already-owned endpoint.
    assert api.arguments == [("serve",), ("--version",)]
    assert all(set(environment) == FIXED_INTERNAL_ENV_KEYS for environment in api.environments)
    assert api.calls.index("resume_server") < api.calls.index("create_suspended_version")


def test_unknown_dialect_refuses_before_any_process_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"unknown dialect fixture")
    monkeypatch.setattr(isolated_server, "REVIEWED_DIALECT_REGISTRY", MappingProxyType({}))
    api = FakeLifecycleApi()
    artifact = IsolatedOllamaServer(executable, api=api, temp_parent=tmp_path).run()
    assert [failure["category"] for failure in artifact["failures"]] == ["attestation_dialect_unavailable"]
    assert api.arguments == []
    assert artifact["temporary_space_torn_down"] is False


def test_synthetic_orphan_is_reported_without_persisting_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    _register_test_dialect(monkeypatch, executable)
    api = FakeLifecycleApi(server_descendants={9876})
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert artifact["orphan_verification"] == "survivor_detected"
    assert {failure["category"] for failure in artifact["failures"]} == {"orphaned_runner"}
    assert "9876" not in json.dumps(artifact)


def test_executable_replacement_is_detected_before_server_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"original executable fixture")
    _register_test_dialect(monkeypatch, executable)

    class ReplacingExecutableApi(FakeLifecycleApi):
        def create_suspended(self, executable_arg, arguments, environment):
            process = super().create_suspended(executable_arg, arguments, environment)
            if arguments == ("serve",):
                executable.write_bytes(b"replaced after suspended creation")
            return process

    api = ReplacingExecutableApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert [failure["category"] for failure in artifact["failures"]] == ["runtime_identity_mismatch"]
    assert "resume_server" not in api.calls
    assert api.arguments == [("serve",)]
    assert str(tmp_path) not in json.dumps(artifact)


# ---------------------------------------------------------------------------
# Address-aware ownership through the full lifecycle
# ---------------------------------------------------------------------------


class AlternateOwnerApi(FakeLifecycleApi):
    def __init__(self, *, in_job: bool) -> None:
        super().__init__()
        self.in_job = in_job

    def tcp_listener_rows(self):
        self.calls.append("tcp_listener_rows")
        if self.active_server_id is None or self.active_server_id in self.terminated:
            return ()
        return (TcpListenerRow("127.0.0.1", self.active_server_port, 7777),)

    def process_id_in_job(self, job, process_id):
        self.calls.append("process_id_in_job")
        assert process_id == 7777
        return self.in_job


def test_job_member_listener_is_owned_but_foreign_listener_is_never_contacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    _register_test_dialect(monkeypatch, executable)
    owned = AlternateOwnerApi(in_job=True)
    artifact = IsolatedOllamaServer(
        executable,
        api=owned,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert artifact["endpoint_owner_verified"] is True
    assert "process_id_in_job" in owned.calls

    contacted = False

    def forbidden_probe(port: int, timeout: float) -> str:
        nonlocal contacted
        contacted = True
        return "0.32.1"

    foreign = AlternateOwnerApi(in_job=False)
    artifact = IsolatedOllamaServer(
        executable,
        api=foreign,
        api_version_probe=forbidden_probe,
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert "port_hijacked" in {failure["category"] for failure in artifact["failures"]}
    assert contacted is False


class ConfigureFailureApi(FakeLifecycleApi):
    def __init__(self) -> None:
        super().__init__()
        self.configure_count = 0

    def configure_kill_on_close(self, job: str) -> None:
        self.configure_count += 1
        self.calls.append("configure_job")
        if self.configure_count == 1:
            raise IsolatedServerFailure("job_limit_configuration_failed", win32_code=5)


def test_pre_assignment_failure_directly_terminates_suspended_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    _register_test_dialect(monkeypatch, executable)
    api = ConfigureFailureApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert [failure["category"] for failure in artifact["failures"]] == ["job_limit_configuration_failed"]
    assert "terminate_process" in api.calls


# ---------------------------------------------------------------------------
# §2.6 command-version binding hostile tests
# ---------------------------------------------------------------------------


class OwnershipChangeBeforeVersionApi(FakeLifecycleApi):
    def tcp_listener_rows(self):
        self.calls.append("tcp_listener_rows")
        count = getattr(self, "_tcp_calls", 0) + 1
        self._tcp_calls = count
        if self.active_server_id is None or self.active_server_id in self.terminated:
            return ()
        if count == 1:
            return (TcpListenerRow("127.0.0.1", self.active_server_port, self.active_server_id),)
        return (TcpListenerRow("127.0.0.1", self.active_server_port, 9999),)


def test_ownership_change_before_version_child_prevents_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"ownership change fixture")
    _register_test_dialect(monkeypatch, executable)
    api = OwnershipChangeBeforeVersionApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert "version_endpoint_ownership_failed" in {f["category"] for f in artifact["failures"]}
    assert api.arguments.count(("--version",)) == 0


def test_client_server_api_disagreement_fails_runtime_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"identity mismatch fixture")
    _register_test_dialect(monkeypatch, executable)
    # The command-reported server version disagrees with /api/version.
    api = FakeLifecycleApi(version_output=b"ollama version is 0.32.2\n")
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert "runtime_identity_mismatch" in {f["category"] for f in artifact["failures"]}


def test_version_child_connection_warning_marker_is_ownership_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"connection warning fixture")
    _register_test_dialect(monkeypatch, executable)
    api = FakeLifecycleApi(
        version_output=b"Warning: could not connect to a running Ollama instance\n"
        b"Warning: client version is 0.32.1\n"
    )
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert "version_endpoint_ownership_failed" in {f["category"] for f in artifact["failures"]}
    assert api.arguments.count(("--version",)) == 1


# ---------------------------------------------------------------------------
# Candidate-port race — the single S10 → S4 edge, gated on a stable foreign
# identity (held handle + creation time), never PID equality alone.
# ---------------------------------------------------------------------------


class BindRaceApi(FakeLifecycleApi):
    FOREIGN_PID = 7777

    def __init__(self, *, races: int) -> None:
        super().__init__()
        self.races_remaining = races
        self.racing_processes: set[int] = set()
        self.racing_ports: dict[int, int] = {}

    def create_suspended(self, executable, arguments, environment):
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",) and self.races_remaining > 0:
            self.races_remaining -= 1
            self.racing_processes.add(process.process_id)
            port = int(environment["OLLAMA_HOST"].rsplit(":", 1)[1])
            self.racing_ports[port] = self.FOREIGN_PID
            self.active_server_id = None
        return process

    def tcp_listener_rows(self):
        self.calls.append("tcp_listener_rows")
        rows = [TcpListenerRow("127.0.0.1", port, pid) for port, pid in self.racing_ports.items()]
        if self.active_server_id is not None and self.active_server_id not in self.terminated:
            rows.append(TcpListenerRow("127.0.0.1", self.active_server_port, self.active_server_id))
        return tuple(rows)

    def process_exit_code(self, process):
        self.calls.append(f"process_exit_{self._kind(process)}")
        if process.process_id in self.racing_processes and process.process_id not in self.terminated:
            self.calls.append("process_exit_bind_race")
            return 10048
        return super().process_exit_code(process)


def test_genuine_bind_race_retries_full_clean_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"bind race fixture")
    _register_test_dialect(monkeypatch, executable)
    ports = iter((50001, 50002, 50003))
    monkeypatch.setattr(isolated_server, "choose_loopback_port", lambda exclusions: next(ports))
    api = BindRaceApi(races=1)
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        launch_attempts=3,
        temp_parent=tmp_path,
    ).run()
    assert artifact["failures"] == []
    assert api.arguments.count(("serve",)) == 2
    server_ports = [
        environment["OLLAMA_HOST"] for environment, args in zip(api.environments, api.arguments)
        if args == ("serve",)
    ]
    assert len(set(server_ports)) == 2
    assert list(tmp_path.glob("odysseus-ollama-attest-*")) == []


def test_exhausted_bind_races_report_bounded_attempt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"bind exhaustion fixture")
    _register_test_dialect(monkeypatch, executable)
    ports = iter((50101, 50102, 50103))
    monkeypatch.setattr(isolated_server, "choose_loopback_port", lambda exclusions: next(ports))
    api = BindRaceApi(races=5)
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        launch_attempts=3,
        temp_parent=tmp_path,
    ).run()
    failure = next(f for f in artifact["failures"] if f["category"] == "port_bind_failed")
    assert failure["numeric_metadata"] == {"attempts": 3}
    assert api.arguments.count(("serve",)) == 3


class MutatingRaceOwnerApi(BindRaceApi):
    def __init__(self, cleanup_state: str) -> None:
        super().__init__(races=1)
        self.cleanup_state = cleanup_state
        self._foreign_seen: dict[int, int] = {}

    def tcp_listener_rows(self):
        self.calls.append("tcp_listener_rows")
        rows = []
        for port, pid in self.racing_ports.items():
            seen = self._foreign_seen.get(port, 0)
            self._foreign_seen[port] = seen + 1
            if seen == 0:
                rows.append(TcpListenerRow("127.0.0.1", port, pid))
                continue
            if self.cleanup_state == "vanished":
                continue
            if self.cleanup_state == "uncertain":
                raise IsolatedServerFailure("ownership_probe_unavailable", win32_code=5)
            if self.cleanup_state == "ambiguous":
                rows.append(TcpListenerRow("127.0.0.1", port, pid))
                rows.append(TcpListenerRow("0.0.0.0", port, pid))
                continue
            rows.append(TcpListenerRow("127.0.0.1", port, pid))
        if self.active_server_id is not None and self.active_server_id not in self.terminated:
            rows.append(TcpListenerRow("127.0.0.1", self.active_server_port, self.active_server_id))
        return tuple(rows)

    def query_pid_creation_time(self, process_id: int) -> int:
        self.calls.append("query_pid_creation_time")
        if self.cleanup_state == "changed" and process_id == self.FOREIGN_PID:
            return self.creation_times.get(process_id, 1000) + 999
        return super().query_pid_creation_time(process_id)

    def handle_signaled(self, handle: str) -> bool:
        self.calls.append("handle_signaled")
        if self.cleanup_state == "signaled":
            return True
        return super().handle_signaled(handle)


@pytest.mark.parametrize(
    ("cleanup_state", "expected_category"),
    [
        ("changed", "owner_identity_changed"),
        ("signaled", "owner_identity_changed"),
        ("vanished", "owner_identity_changed"),
        ("ambiguous", "port_ownership_ambiguous"),
        ("uncertain", "ownership_probe_unavailable"),
    ],
)
def test_bind_race_cleanup_ambiguity_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_state: str,
    expected_category: str,
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"bind cleanup uncertainty fixture")
    _register_test_dialect(monkeypatch, executable)
    monkeypatch.setattr(isolated_server, "choose_loopback_port", lambda exclusions: 50201)
    api = MutatingRaceOwnerApi(cleanup_state)
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        launch_attempts=3,
        temp_parent=tmp_path,
    ).run()
    assert expected_category in {f["category"] for f in artifact["failures"]}
    assert api.arguments.count(("serve",)) == 1


class ArbitraryCrashApi(FakeLifecycleApi):
    def tcp_listener_rows(self):
        self.calls.append("tcp_listener_rows_empty")
        return ()

    def process_exit_code(self, process):
        if self._kind(process) == "server" and process.process_id not in self.terminated:
            return 7
        return super().process_exit_code(process)


def test_non_bind_process_failure_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"arbitrary crash fixture")
    _register_test_dialect(monkeypatch, executable)
    api = ArbitraryCrashApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        launch_attempts=3,
        temp_parent=tmp_path,
    ).run()
    assert "startup_process_exit" in {f["category"] for f in artifact["failures"]}
    assert api.arguments.count(("serve",)) == 1


# ---------------------------------------------------------------------------
# Bounded owned /api/ps (model residency)
# ---------------------------------------------------------------------------


def test_empty_model_residency_response_is_closed_and_valid() -> None:
    verify_empty_model_residency(b'{"models":[]}')


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b"{}",
        b'{"models":null}',
        b'{"models":[],"extra":true}',
        b'{"models":[],"models":[]}',
    ],
)
def test_malformed_model_residency_response_fails_closed(payload: bytes) -> None:
    with pytest.raises(IsolatedServerFailure, match="model_residency_probe_failed"):
        verify_empty_model_residency(payload)


def test_nonempty_model_residency_response_is_typed_without_leaking_model() -> None:
    with pytest.raises(IsolatedServerFailure, match="unexpected_model_residency") as exc:
        verify_empty_model_residency(b'{"models":[{"name":"private-model"}]}')
    assert "private-model" not in str(exc.value)


def test_oversized_model_residency_response_is_bounded() -> None:
    payload = b"x" * (isolated_server.MAX_MODEL_RESIDENCY_RESPONSE_BYTES + 1)
    with pytest.raises(IsolatedServerFailure, match="model_residency_probe_failed") as exc:
        verify_empty_model_residency(payload)
    assert exc.value.numeric_metadata == {"bytes_observed": len(payload)}


def test_default_model_residency_probe_makes_only_bounded_get_api_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, str, int]] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def read(self, size: int) -> bytes:
            observed.append((request.get_method(), request.full_url, size))
            return b'{"models":[]}'

    def fake_urlopen(probe_request, timeout: float):
        nonlocal request
        request = probe_request
        assert timeout == isolated_server.MODEL_RESIDENCY_TIMEOUT_SECONDS
        return Response()

    request = None
    monkeypatch.setattr(isolated_server.urllib.request, "urlopen", fake_urlopen)
    raw = isolated_server._default_model_residency(54321, isolated_server.MODEL_RESIDENCY_TIMEOUT_SECONDS)
    verify_empty_model_residency(raw)
    assert observed == [
        ("GET", "http://127.0.0.1:54321/api/ps", isolated_server.MAX_MODEL_RESIDENCY_RESPONSE_BYTES + 1)
    ]
    assert not any(token in observed[0][1] for token in ("generate", "load", "unload"))


class ResidencyOwnerChangeApi(FakeLifecycleApi):
    def __init__(self) -> None:
        super().__init__()
        self._probe_count = 0

    def tcp_listener_rows(self):
        self.calls.append("tcp_listener_rows")
        if self.active_server_id is None or self.active_server_id in self.terminated:
            return ()
        self._probe_count += 1
        if self._probe_count >= 3:
            return (TcpListenerRow("127.0.0.1", self.active_server_port, 7777),)
        return (TcpListenerRow("127.0.0.1", self.active_server_port, self.active_server_id),)


def test_ownership_change_before_api_ps_prevents_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"residency ownership fixture")
    _register_test_dialect(monkeypatch, executable)
    contacted = False

    def forbidden_residency(port: int, timeout: float) -> bytes:
        nonlocal contacted
        contacted = True
        return b'{"models":[]}'

    api = ResidencyOwnerChangeApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=forbidden_residency,
        temp_parent=tmp_path,
    ).run()
    assert "port_hijacked" in {f["category"] for f in artifact["failures"]}
    assert artifact["model_residency_verified_empty"] is False
    assert contacted is False


@pytest.mark.parametrize(
    ("payload", "category"),
    [
        (b"malformed", "model_residency_probe_failed"),
        (b'{"models":[{"name":"private-model"}]}', "unexpected_model_residency"),
        (b"x" * (isolated_server.MAX_MODEL_RESIDENCY_RESPONSE_BYTES + 1), "model_residency_probe_failed"),
    ],
)
def test_model_residency_failures_are_privacy_safe_artifact_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, category: str
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"residency failure fixture")
    _register_test_dialect(monkeypatch, executable)
    artifact = IsolatedOllamaServer(
        executable,
        api=FakeLifecycleApi(),
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=lambda port, timeout: payload,
        temp_parent=tmp_path,
    ).run()
    assert category in {f["category"] for f in artifact["failures"]}
    assert artifact["model_residency_verified_empty"] is False
    assert "private-model" not in json.dumps(artifact)


# ---------------------------------------------------------------------------
# Attestation timing: separate clocks, deadline exclusions
# ---------------------------------------------------------------------------


class DelayedLogApi(FakeLifecycleApi):
    """Server stdout hangs until the harness has observed API readiness,
    then delivers the attestation payload after two subsequent polls."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(server_log=b"")
        self.payload = payload
        self.api_ready = False
        self._poll_count = 0
        self._server_stdout: FakePipe | None = None

    def create_suspended(self, executable, arguments, environment):
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",):
            process.stdout.events = []
            process.stdout.hang = True
            self._server_stdout = process.stdout
        return process

    def finish_overlapped_read(self, pipe):
        if (
            self.api_ready
            and pipe is self._server_stdout
            and pipe.pending
            and not pipe.events
            and not pipe.eof
        ):
            self._poll_count += 1
            if self._poll_count >= 2:
                pipe.hang = False
                pipe.events = [("data", self.payload), ("eof", b"")]
        return super().finish_overlapped_read(pipe)


def test_attestation_waits_for_configuration_logs_after_api_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"delayed logs fixture")
    _register_test_dialect(monkeypatch, executable)
    api = DelayedLogApi(b"server_version=0.32.1 noprune=true no_cloud=true")
    clock = FakeClock()
    api.clock = clock

    def version_api(port: int, timeout: float) -> str:
        api.api_ready = True
        return "0.32.1"

    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=version_api,
        model_residency_probe=_empty_model_residency,
        clock=clock,
        temp_parent=tmp_path,
    ).run()
    assert artifact["failures"] == []
    assert artifact["attested_settings"]["noprune"]["value"] == "true"
    assert api.calls.count("wait_for_completion") >= 1


def test_attestation_deadline_is_separate_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"missing markers fixture")
    _register_test_dialect(monkeypatch, executable)
    clock = FakeClock()
    api = FakeLifecycleApi(server_log=b"server_version=0.32.1")
    api.clock = clock
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        attestation_timeout_seconds=0.2,
        clock=clock,
        temp_parent=tmp_path,
    ).run()
    missing = next(f for f in artifact["failures"] if f["category"] == "attestation_missing")
    assert missing["numeric_metadata"] == {"timeout_ms": 200}
    assert artifact["readiness_duration_ms"] == 0


class SlowPrelaunchApi(FakeLifecycleApi):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._fake_clock = clock

    def create_suspended(self, executable, arguments, environment):
        if arguments == ("serve",):
            self._fake_clock.now += 50.0
        return super().create_suspended(executable, arguments, environment)

    def resume_process(self, process):
        if self._kind(process) == "server":
            self._fake_clock.now += 0.1
        super().resume_process(process)


def test_readiness_duration_excludes_all_prelaunch_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"readiness clock fixture")
    _register_test_dialect(monkeypatch, executable)
    clock = FakeClock()
    api = SlowPrelaunchApi(clock)
    api.clock = clock

    def api_probe(port: int, timeout: float) -> str:
        clock.now += 0.2
        return "0.32.1"

    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=api_probe,
        model_residency_probe=_empty_model_residency,
        clock=clock,
        temp_parent=tmp_path,
    ).run()
    assert artifact["readiness_duration_ms"] == 300
    assert artifact["readiness_duration_ms"] < 50_000


# ---------------------------------------------------------------------------
# Cleanup-deadline exhaustion (§5 fixed no-retry cleanup failure)
# ---------------------------------------------------------------------------


class HangingCleanupApi(FakeLifecycleApi):
    def create_suspended(self, executable, arguments, environment):
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",):
            process.stdout.hang = True
            process.stdout.events = []
        return process


def test_cleanup_deadline_exhaustion_yields_fixed_failure_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"cleanup deadline fixture")
    _register_test_dialect(monkeypatch, executable)
    clock = FakeClock()
    api = HangingCleanupApi()
    api.clock = clock
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        attestation_timeout_seconds=0.2,
        cleanup_timeout_seconds=0.2,
        launch_attempts=3,
        clock=clock,
        temp_parent=tmp_path,
    ).run()
    categories = {f["category"] for f in artifact["failures"]}
    assert "pending_io_cleanup_timeout" in categories
    assert artifact["overall_diagnostic_evidence_state"] != "complete"
    assert api.arguments.count(("serve",)) == 1


# ---------------------------------------------------------------------------
# Artifact schema, failure-category reachability, dry-run/CLI, privacy
# ---------------------------------------------------------------------------


def test_attestation_artifact_schema_rejects_unknown_and_private_fields() -> None:
    artifact = empty_attestation_artifact()
    assert validate_attestation_artifact(artifact) == []
    artifact["pid"] = 123
    assert validate_attestation_artifact(artifact)
    artifact = empty_attestation_artifact()
    artifact["runtime_identity"]["executable_basename"] = "some/path/ollama.exe"
    assert validate_attestation_artifact(artifact)


@pytest.mark.parametrize("category", sorted(PHASE1_FAILURE_CATEGORIES))
def test_every_phase1_failure_category_is_reachable_and_closed(category: str) -> None:
    failure = IsolatedServerFailure(category, attempts=1)
    assert failure.as_record() == {"category": category, "numeric_metadata": {"attempts": 1}}
    artifact = empty_attestation_artifact()
    artifact["failures"] = [failure.as_record()]
    assert validate_attestation_artifact(artifact) == []


def test_unknown_failure_category_and_metadata_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown Phase-1"):
        IsolatedServerFailure("surprise")
    with pytest.raises(ValueError, match="metadata"):
        IsolatedServerFailure("startup_timeout", private_port=11434)


def test_dry_run_plan_is_process_free_and_does_not_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: (_ for _ in ()).throw(AssertionError("must not probe")))
    plan = build_dry_run_plan()
    assert plan["mode"] == "dry_run"
    assert plan["would_spawn"] is False
    assert plan["environment"]["construction"] == "from_empty"
    assert plan["requested_settings"]["user_overrides"] == {}
    assert plan["deadlines_ms"]["cleanup"] == 10_000
    assert plan["version_binding"] == "post_readiness_owned_endpoint_only"
    assert plan["tcp_ownership"] == "address_aware_closed_result_model"
    assert plan["log_io"] == "overlapped_no_helper_threads"


def test_cli_attest_defaults_to_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["attest"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run"
    assert output["would_spawn"] is False


def test_complete_artifact_contains_no_pid_port_address_or_pipe_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"privacy fixture")
    _register_test_dialect(monkeypatch, executable)
    api = FakeLifecycleApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_model_residency,
        temp_parent=tmp_path,
    ).run()
    assert artifact["overall_diagnostic_evidence_state"] == "complete"
    serialized = json.dumps(artifact, sort_keys=True)
    for pid in api.processes:
        assert str(pid) not in serialized
    if api.active_server_port is not None:
        assert str(api.active_server_port) not in serialized
    assert "127.0.0.1" not in serialized
    assert "0.0.0.0" not in serialized
    assert "\\\\.\\pipe\\" not in serialized
    assert str(tmp_path) not in serialized


# ---------------------------------------------------------------------------
# Reviewed Ollama 0.32.1 dialect (SHA-256-bound, G-ISO-D0 real evidence)
# ---------------------------------------------------------------------------

_REVIEWED_OLLAMA_0321_SHA256 = "7a777be95617a38798a9942a7fce7ec65f972ccc10ec061007b5a4dd5329741b"

_REVIEWED_STARTUP_FIXTURE = (
    'time=2026-07-20T09:14:02.001-07:00 level=INFO source=routes.go:1234 msg="server config" '
    'env="map[OLLAMA_DEBUG:false OLLAMA_HOST:127.0.0.1:54321 OLLAMA_NOPRUNE:true OLLAMA_NUM_PARALLEL:0]"\n'
    'time=2026-07-20T09:14:02.050-07:00 level=INFO source=routes.go:1240 msg="Ollama cloud disabled: true"\n'
    'time=2026-07-20T09:14:02.301-07:00 level=INFO source=routes.go:1300 '
    'msg="Listening on 127.0.0.1:54321 (version 0.32.1)"\n'
).encode("utf-8")


def _reviewed_identity() -> RuntimeIdentity:
    return RuntimeIdentity("ollama.exe", _REVIEWED_OLLAMA_0321_SHA256, "0.32.1", "0.32.1", "0.32.1")


def _reviewed_entry() -> ReviewedDialectEntry:
    return isolated_server.REVIEWED_DIALECT_REGISTRY[_REVIEWED_OLLAMA_0321_SHA256]


def test_reviewed_registry_contains_exactly_the_one_reviewed_sha() -> None:
    assert set(isolated_server.REVIEWED_DIALECT_REGISTRY) == {_REVIEWED_OLLAMA_0321_SHA256}


def test_reviewed_dialect_for_hash_returns_the_exact_entry() -> None:
    entry = reviewed_dialect_for_hash(_REVIEWED_OLLAMA_0321_SHA256)
    assert entry.identity_sha256 == _REVIEWED_OLLAMA_0321_SHA256
    assert entry is _reviewed_entry()


def test_reviewed_registry_validates_cleanly() -> None:
    validate_dialect_registry(isolated_server.REVIEWED_DIALECT_REGISTRY)


@pytest.mark.parametrize(
    "digest",
    [
        "0" * 64,
        "b" * 64,
        _REVIEWED_OLLAMA_0321_SHA256[:-1] + ("0" if _REVIEWED_OLLAMA_0321_SHA256[-1] != "0" else "1"),
    ],
)
def test_unknown_or_unrelated_hash_fails_closed(digest: str) -> None:
    with pytest.raises(IsolatedServerFailure, match="attestation_dialect_unavailable"):
        reviewed_dialect_for_hash(digest)


def test_reviewed_dialect_does_not_admit_unrelated_hash() -> None:
    unrelated = "f" * 64
    assert unrelated not in isolated_server.REVIEWED_DIALECT_REGISTRY
    with pytest.raises(IsolatedServerFailure, match="attestation_dialect_unavailable"):
        reviewed_dialect_for_hash(unrelated)


def test_every_reviewed_dialect_pattern_has_exactly_one_capture_group() -> None:
    entry = _reviewed_entry()
    assert entry.startup.startup_version.groups == 1
    for pattern, _source in entry.startup.setting_patterns.values():
        assert pattern.groups == 1
    assert entry.version_output.server_version_pattern.groups == 1
    assert entry.version_output.client_version_pattern.groups == 1


def test_reviewed_version_output_parses_the_owned_command_line() -> None:
    client, server = parse_version_output(b"ollama version is 0.32.1\n", _reviewed_entry().version_output)
    assert client == "0.32.1"
    assert server == "0.32.1"


def test_reviewed_version_grammar_accepts_another_valid_semver_directly() -> None:
    # Same grammar, invoked directly against the reviewed dialect's own
    # parser -- not registered under a different SHA-256.
    client, server = parse_version_output(
        b"ollama version is 1.2.3-rc.1+build.9\n", _reviewed_entry().version_output
    )
    assert client == "1.2.3-rc.1+build.9"
    assert server == "1.2.3-rc.1+build.9"


def test_reviewed_client_warning_line_is_recognized_and_captured() -> None:
    raw = b"Warning: client version is 0.32.0\nollama version is 0.32.1\n"
    client, server = parse_version_output(raw, _reviewed_entry().version_output)
    assert client == "0.32.0"
    assert server == "0.32.1"


def test_reviewed_connection_warning_fails_endpoint_ownership() -> None:
    raw = b"Warning: could not connect to a running Ollama instance\n"
    with pytest.raises(IsolatedServerFailure, match="version_endpoint_ownership_failed"):
        parse_version_output(raw, _reviewed_entry().version_output)


@pytest.mark.parametrize(
    "raw",
    [
        b"unexpected line that matches nothing\n",
        b"ollama version is 0.32.1\nollama version is 0.32.1\n",
        b"ollama version is 0.32.1\nollama version is 0.32.2\n",
    ],
)
def test_reviewed_unexpected_or_duplicate_version_lines_fail_malformed(raw: bytes) -> None:
    with pytest.raises(IsolatedServerFailure, match="version_output_malformed"):
        parse_version_output(raw, _reviewed_entry().version_output)


def test_reviewed_startup_fixture_attests_version_noprune_and_no_cloud() -> None:
    startup_record, settings = parse_startup_attestation(
        _REVIEWED_STARTUP_FIXTURE, identity=_reviewed_identity(), dialect=_reviewed_entry().startup
    )
    assert startup_record == {"state": "attested", "value": "0.32.1", "source": "startup_log"}
    assert settings["noprune"] == {"state": "attested", "value": "true", "source": "startup_log"}
    assert settings["no_cloud"] == {"state": "attested", "value": "true", "source": "startup_log"}


def test_reviewed_startup_version_mismatch_fails_identity() -> None:
    mismatched_identity = RuntimeIdentity(
        "ollama.exe", _REVIEWED_OLLAMA_0321_SHA256, "0.32.9", "0.32.9", "0.32.9"
    )
    with pytest.raises(IsolatedServerFailure, match="runtime_identity_mismatch"):
        parse_startup_attestation(
            _REVIEWED_STARTUP_FIXTURE, identity=mismatched_identity, dialect=_reviewed_entry().startup
        )


def test_reviewed_absent_mandatory_settings_stay_unattested_and_report_missing() -> None:
    raw = (
        'time=2026-07-20T09:14:02.301-07:00 level=INFO msg="Listening on 127.0.0.1:54321 (version 0.32.1)"\n'
    ).encode("utf-8")
    startup_record, settings = parse_startup_attestation(
        raw, identity=_reviewed_identity(), dialect=_reviewed_entry().startup
    )
    assert startup_record["state"] == "attested"
    assert settings["noprune"] == {"state": "unattested", "value": None, "source": "unattested"}
    assert settings["no_cloud"] == {"state": "unattested", "value": None, "source": "unattested"}
    failures = compare_requested_attestation({}, settings)
    assert [failure.category for failure in failures] == ["attestation_missing"]


def test_reviewed_dialect_synthetic_tests_never_launch_real_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_create_suspended(self, executable_arg, arguments, environment):
        raise AssertionError("real process creation must not happen in synthetic dialect tests")

    monkeypatch.setattr(WindowsLifecycleApi, "create_suspended", forbidden_create_suspended)
    entry = reviewed_dialect_for_hash(_REVIEWED_OLLAMA_0321_SHA256)
    parse_startup_attestation(_REVIEWED_STARTUP_FIXTURE, identity=_reviewed_identity(), dialect=entry.startup)
    parse_version_output(b"ollama version is 0.32.1\n", entry.version_output)
    validate_dialect_registry(isolated_server.REVIEWED_DIALECT_REGISTRY)
