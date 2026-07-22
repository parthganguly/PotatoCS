from __future__ import annotations

import json
import shutil
import socket
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from odysseus_desktop_backend.runtime_bench import __main__ as runtime_main
from odysseus_desktop_backend.runtime_bench import dialect_capture, isolated_server
from odysseus_desktop_backend.runtime_bench.dialect_capture import (
    EVIDENCE_STATE,
    MAX_EVIDENCE_LINE_CHARS,
    MAX_STARTUP_EVIDENCE_LINES,
    DialectCaptureSession,
    build_capture_dry_run_plan,
    redact_evidence_line,
    select_startup_evidence_lines,
    select_version_command_lines,
    validate_capture_result,
)
from odysseus_desktop_backend.runtime_bench.isolated_server import (
    MAX_VERSION_OUTPUT_BYTES,
    REVIEWED_DIALECT_REGISTRY,
    BoundedVersionCapture,
    IsolatedOllamaServer,
    IsolatedServerFailure,
    TcpListenerRow,
    validate_attestation_artifact,
    verify_empty_model_residency,
)
from test_isolated_ollama_server import FakeClock, FakeLifecycleApi, FakePipe


def _empty_ps(port: int, timeout: float) -> bytes:
    return b'{"models": []}'


def _capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    api: FakeLifecycleApi | None = None,
    api_version_probe=None,
    model_residency_probe=None,
    clock=None,
    startup_timeout_seconds: float = 0.2,
) -> tuple[dict, FakeLifecycleApi, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"synthetic unreviewed executable")
    monkeypatch.setattr(dialect_capture, "choose_loopback_port", lambda exclusions: 54321)
    selected_api = FakeLifecycleApi() if api is None else api
    session = DialectCaptureSession(
        executable,
        startup_timeout_seconds=startup_timeout_seconds,
        version_timeout_seconds=0.2,
        cleanup_timeout_seconds=0.2,
        api=selected_api,
        api_version_probe=(lambda port, timeout: "0.32.1")
        if api_version_probe is None
        else api_version_probe,
        model_residency_probe=_empty_ps
        if model_residency_probe is None
        else model_residency_probe,
        clock=selected_api.clock if clock is None and selected_api.clock is not None else (
            clock if clock is not None else dialect_capture.time.monotonic
        ),
        temp_parent=tmp_path,
    )
    return session.run(), selected_api, executable


def test_capture_dialect_no_approval_is_process_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        dialect_capture.tempfile if hasattr(dialect_capture, "tempfile") else isolated_server.tempfile,
        "mkdtemp",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not allocate")),
    )
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not open socket")),
    )
    assert runtime_main.main(["capture-dialect"]) == 0
    assert json.loads(capsys.readouterr().out) == build_capture_dry_run_plan()


def test_capture_dialect_approval_requires_tty_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        runtime_main,
        "DialectCaptureSession",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    with pytest.raises(SystemExit) as exc_info:
        runtime_main.main(["capture-dialect", "--approved-g-iso-d0"])
    assert exc_info.value.code == 2


def test_capture_dialect_approval_requires_tty_stdout_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdin alone being interactive must not be sufficient (Blocker 1)."""

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(
        runtime_main,
        "DialectCaptureSession",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not construct a session")),
    )
    monkeypatch.setattr(
        dialect_capture.shutil,
        "which",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not resolve executable")),
    )
    monkeypatch.setattr(
        dialect_capture,
        "create_session_space",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not allocate temp space")),
    )
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not open socket")),
    )
    with pytest.raises(SystemExit) as exc_info:
        runtime_main.main(["capture-dialect", "--approved-g-iso-d0"])
    assert exc_info.value.code == 2


def test_capture_dialect_dry_run_permits_redirected_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry-run path stays usable and process-free with redirected stdio."""

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not open socket")),
    )
    assert runtime_main.main(["capture-dialect"]) == 0
    assert json.loads(capsys.readouterr().out) == build_capture_dry_run_plan()


def test_normal_attest_unknown_hash_gate_remains_empty_and_prelaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fresh unknown attestation executable")
    monkeypatch.setattr(isolated_server, "REVIEWED_DIALECT_REGISTRY", MappingProxyType({}))
    api = FakeLifecycleApi()
    artifact = IsolatedOllamaServer(executable, api=api, temp_parent=tmp_path).run()
    assert dict(isolated_server.REVIEWED_DIALECT_REGISTRY) == {}
    assert [failure["category"] for failure in artifact["failures"]] == [
        "attestation_dialect_unavailable"
    ]
    assert api.arguments == []


def test_capture_succeeds_for_hash_absent_from_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, api, _ = _capture(tmp_path, monkeypatch)
    assert result["failures"] == []
    assert result["evidence_state"] == EVIDENCE_STATE
    assert result["executable"]["sha256"] not in REVIEWED_DIALECT_REGISTRY
    assert all(result["proofs"].values())
    assert api.arguments == [("serve",), ("--version",)]


def test_capture_import_and_use_leave_registry_mapping_proxy_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = isolated_server.REVIEWED_DIALECT_REGISTRY
    assert isinstance(registry, MappingProxyType)
    assert dict(registry) == {}
    _capture(tmp_path, monkeypatch)
    assert isolated_server.REVIEWED_DIALECT_REGISTRY is registry
    assert dict(registry) == {}


def test_ownership_is_proved_before_api_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeLifecycleApi()

    def probe(port: int, timeout: float) -> str:
        api.calls.append("api_version_probe")
        assert "tcp_listener_rows" in api.calls[:-1]
        return "0.32.1"

    result, _, _ = _capture(tmp_path, monkeypatch, api=api, api_version_probe=probe)
    assert result["failures"] == []
    assert api.calls.index("tcp_listener_rows") < api.calls.index("api_version_probe")


def test_ownership_is_reproved_before_version_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeLifecycleApi()

    def probe(port: int, timeout: float) -> str:
        api.calls.append("api_version_probe")
        return "0.32.1"

    result, _, _ = _capture(tmp_path, monkeypatch, api=api, api_version_probe=probe)
    assert result["failures"] == []
    api_index = api.calls.index("api_version_probe")
    version_index = api.calls.index("create_suspended_version")
    assert "tcp_listener_rows" in api.calls[api_index + 1 : version_index]


def test_capture_result_never_validates_as_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _capture(tmp_path, monkeypatch)
    assert validate_capture_result(result) == []
    assert validate_attestation_artifact(result)


def test_absolute_startup_deadline_enforced_before_api_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 2: the api_version_probe must never run once the startup
    deadline has already expired, and it must not receive a manufactured
    minimum allowance."""

    values = iter([0.0, 100.0])

    def clock() -> float:
        return next(values, 100.0)

    def probe(port: int, timeout: float) -> str:
        raise AssertionError("api_version_probe must not be invoked past the deadline")

    result, _, _ = _capture(
        tmp_path,
        monkeypatch,
        api_version_probe=probe,
        clock=clock,
        startup_timeout_seconds=0.1,
    )
    assert "startup_timeout" in {failure["category"] for failure in result["failures"]}


class HangingStdoutApi(FakeLifecycleApi):
    """A healthy server whose stdout pipe never reaches EOF or aborts on
    its own -- only an explicit cancellation (during cleanup) resolves
    the outstanding read."""

    def create_suspended(self, executable, arguments, environment):
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",):
            process.stdout = FakePipe(events=[("data", b"listening ready")])
        return process


def test_drain_bounded_when_server_pipes_never_reach_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 2: the post-readiness drain must not wait for a healthy,
    long-running server's pipes to reach EOF, and evidence already
    available at readiness must be retained. Cleanup still safely
    cancels the outstanding read."""

    clock = FakeClock()
    api = HangingStdoutApi()
    api.clock = clock
    result, api, _ = _capture(
        tmp_path, monkeypatch, api=api, clock=clock, startup_timeout_seconds=5.0
    )
    assert result["failures"] == []
    assert all(result["proofs"].values())
    assert clock.now < 1.0
    assert any(
        "listening" in line for line in result["observations"]["startup_evidence_lines"]
    )
    assert "cancel_overlapped_read" in api.calls
    assert "close_pipe" in api.calls


class ServeCreateFailsApi(FakeLifecycleApi):
    def create_suspended(self, executable, arguments, environment):
        if arguments == ("serve",):
            raise IsolatedServerFailure("process_create_failed")
        return super().create_suspended(executable, arguments, environment)


def test_empty_model_store_proof_false_when_create_suspended_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 3: empty_model_store_used must only become true once the
    server child was actually created with the isolated-store
    environment -- not merely because the session space was built."""

    result, _, _ = _capture(tmp_path, monkeypatch, api=ServeCreateFailsApi())
    assert result["proofs"]["empty_model_store_used"] is False
    assert "process_create_failed" in {
        failure["category"] for failure in result["failures"]
    }


def test_empty_model_store_proof_true_after_successful_suspended_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 3, positive case: the proof becomes true once the child is
    actually created using the empty-store environment."""

    result, _, _ = _capture(tmp_path, monkeypatch)
    assert result["failures"] == []
    assert result["proofs"]["empty_model_store_used"] is True


class NeverReadyApi(FakeLifecycleApi):
    def tcp_listener_rows(self) -> tuple[TcpListenerRow, ...]:
        self.calls.append("tcp_listener_rows")
        return ()


def test_evidence_state_is_constant_on_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    api = NeverReadyApi(server_log=b"")
    api.clock = clock
    result, _, _ = _capture(
        tmp_path, monkeypatch, api=api, clock=clock, startup_timeout_seconds=0.1
    )
    assert result["evidence_state"] == EVIDENCE_STATE
    assert "startup_timeout" in {failure["category"] for failure in result["failures"]}


def test_startup_evidence_keeps_only_allowlisted_lines() -> None:
    raw = (
        b"diagnostic noise\n"
        b"starting server version 0.32.1\n"
        b"unrelated allocation detail\n"
        b"listening on 127.0.0.1:54321\n"
    )
    lines, truncated = select_startup_evidence_lines(raw)
    assert lines == [
        "starting server version 0.32.1",
        "listening on <HOST>:<PORT>",
    ]
    assert truncated is False


def test_startup_evidence_caps_line_count_and_length() -> None:
    raw = b"\n".join(
        [b"ready " + b"x" * 600] + [f"ready line {index}".encode() for index in range(39)]
    )
    lines, truncated = select_startup_evidence_lines(raw)
    assert len(lines) == MAX_STARTUP_EVIDENCE_LINES
    assert all(len(line) <= MAX_EVIDENCE_LINE_CHARS for line in lines)
    assert len(lines[0]) == MAX_EVIDENCE_LINE_CHARS
    assert truncated is True


def test_version_evidence_uses_combined_four_kib_cap() -> None:
    raw = b"version evidence\n" * 1000
    bounded = BoundedVersionCapture()
    bounded.feed(raw)
    lines, truncated = select_version_command_lines(raw)
    assert len(bounded.bytes()) == MAX_VERSION_OUTPUT_BYTES
    assert sum(len(line.encode()) for line in lines) <= MAX_VERSION_OUTPUT_BYTES
    assert bounded.overflowed is True
    assert truncated is True


@pytest.mark.parametrize(
    ("raw", "token"),
    [
        (r"binary=C:\Users\foo\bar\ollama.exe", "<PATH>"),
        (r"share=\\server\share\path", "<PATH>"),
        ("store=/home/foo/.ollama/models", "<PATH>"),
    ],
)
def test_paths_are_redacted(raw: str, token: str) -> None:
    redacted = redact_evidence_line(raw)
    assert token in redacted
    assert "foo" not in redacted


def test_usernames_are_redacted_bare_and_inside_paths() -> None:
    bare = redact_evidence_line("USERNAME=foo user=bar")
    path = redact_evidence_line(r"C:\Users\alice\ollama.exe")
    assert bare == "USERNAME=<USER> user=<USER>"
    assert path == "<PATH>"
    assert not {"foo", "bar", "alice"} & {bare, path}


@pytest.mark.parametrize(
    "raw",
    [
        "127.0.0.1",
        "::1",
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
        "node.example.local",
        "workstation",
    ],
)
def test_ip_literals_and_bare_hostnames_are_redacted(raw: str) -> None:
    assert redact_evidence_line(f"listening on {raw}") == "listening on <HOST>"


def test_host_and_port_are_redacted_separately() -> None:
    redacted = redact_evidence_line("listening on 127.0.0.1:54321")
    assert redacted == "listening on <HOST>:<PORT>"
    assert "54321" not in redacted


def test_url_credentials_and_query_are_one_token() -> None:
    redacted = redact_evidence_line(
        "proxy http://user:pass@host:1234/path?token=abc, ready"
    )
    assert redacted == "proxy <URL>, ready"
    assert not any(secret in redacted for secret in ("user", "pass", "1234", "abc"))


def test_proxy_authorization_bearer_and_token_values_are_redacted() -> None:
    redacted = redact_evidence_line(
        "Authorization: Bearer sk-abcdef123456 HTTP_PROXY=http://proxy token=secret-value"
    )
    assert "<SECRET>" in redacted
    assert "<URL>" in redacted or "HTTP_PROXY=<SECRET>" in redacted
    assert not any(secret in redacted for secret in ("sk-abcdef123456", "http://proxy", "secret-value"))


def test_pid_pipe_and_handle_are_redacted() -> None:
    redacted = redact_evidence_line(
        r"pid=4821 (pid 4822) pipe=\\.\pipe\ollama-abc123 handle=0x1F4"
    )
    assert "<PID>" in redacted
    assert "<PIPE>" in redacted
    assert "<HANDLE>" in redacted
    assert not any(raw in redacted for raw in ("4821", "4822", "ollama-abc123", "0x1F4"))


def test_redaction_preserves_surrounding_grammar() -> None:
    redacted = redact_evidence_line("server listening on localhost:54321, ready")
    assert redacted == "server listening on <HOST>:<PORT>, ready"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("port=11434", "port=<PORT>"),
        ("port:11434", "port:<PORT>"),
        ("port: 11434", "port: <PORT>"),
        ("port 11434", "port <PORT>"),
        ("listening on 11434", "listening on <PORT>"),
        ("listen=11434", "listen=<PORT>"),
    ],
)
def test_bare_ports_are_redacted_in_every_keyword_form(raw: str, expected: str) -> None:
    redacted = redact_evidence_line(raw)
    assert redacted == expected
    assert "11434" not in redacted


@pytest.mark.parametrize(
    "raw",
    [
        "localhost:11434",
        "127.0.0.1:11434",
        "[::1]:11434",
    ],
)
def test_host_and_port_combinations_are_redacted(raw: str) -> None:
    redacted = redact_evidence_line(raw)
    assert "<HOST>:<PORT>" in redacted
    assert "11434" not in redacted


@pytest.mark.parametrize(
    "raw",
    [
        "pid=1234",
        "pid:1234",
        "pid: 1234",
        "PID 1234",
        "process id 1234",
        "process_id=1234",
        "(pid 1234)",
    ],
)
def test_pids_are_redacted_in_every_keyword_form(raw: str) -> None:
    redacted = redact_evidence_line(raw)
    assert "<PID>" in redacted
    assert "1234" not in redacted


@pytest.mark.parametrize(
    "raw",
    [
        "handle=0x1F4",
        "handle: 0x1F4",
        "HANDLE 500",
        "process_handle=500",
    ],
)
def test_handles_are_redacted_in_every_keyword_form(raw: str) -> None:
    redacted = redact_evidence_line(raw)
    assert "<HANDLE>" in redacted
    assert "0x1F4" not in redacted
    assert "500" not in redacted.replace("<HANDLE>", "")


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("token=abc", "abc"),
        ("token: abc", "abc"),
        ("token abc", "abc"),
        ("Authorization: Bearer abc", "abc"),
        ("Bearer abc", "abc"),
        ("HTTPS_PROXY=http://user:pass@host:8080", "pass"),
        ("proxy: secret-value", "secret-value"),
    ],
)
def test_secrets_and_credentials_are_redacted_in_every_form(raw: str, forbidden: str) -> None:
    redacted = redact_evidence_line(raw)
    assert forbidden not in redacted
    assert "<SECRET>" in redacted or "<URL>" in redacted


@pytest.mark.parametrize(
    "raw",
    [
        r'"C:\Users\Alice Smith\secret.txt"',
        r"'C:\Program Files\Ollama\ollama.exe'",
        r"C:\Users\Alice\file.txt",
        r"\\server\share\Alice\file.txt",
        "/home/alice/.ollama/models",
        r'"C:\\Users\\Alice Smith\\file.txt"',
    ],
)
def test_paths_are_redacted_without_trailing_fragments(raw: str) -> None:
    redacted = redact_evidence_line(raw)
    assert "<PATH>" in redacted
    assert "Alice" not in redacted
    assert "\\" not in redacted.replace("<PATH>", "")
    assert "/" not in redacted.replace("<PATH>", "")


@pytest.mark.parametrize(
    ("raw", "port"),
    [
        ("127.0.0.1", None),
        ("127.0.0.1:11434", "11434"),
        ("2001:0db8:85a3:0000:0000:8a2e:0370:7334", None),
        ("[::1]", None),
        ("[::1]:11434", "11434"),
        ("fe80::1%12", None),
        ("[fe80::1%12]:9000", "9000"),
        ("localhost", None),
        ("node.example.local", None),
        ("workstation", None),
        ("corp-server-01", None),
    ],
)
def test_hosts_and_addresses_are_redacted(raw: str, port: str | None) -> None:
    redacted = redact_evidence_line(f"listening on {raw}")
    assert "<HOST>" in redacted
    assert raw not in redacted
    if port is not None:
        assert "<PORT>" in redacted
        assert port not in redacted


def test_replacement_tokens_survive_the_full_pipeline_unmodified() -> None:
    line = (
        "path=<PATH> host=<HOST> port=<PORT> secret=<SECRET> pid=<PID> "
        "pipe=<PIPE> handle=<HANDLE> url=<URL>"
    )
    assert redact_evidence_line(line) == line


@pytest.mark.parametrize(
    ("payload", "category"),
    [
        (b'{"models": []}', None),
        (b'{"models": [{"name": "x"}]}', "unexpected_model_residency"),
        (b'{"models": [], "extra": 1}', "model_residency_probe_failed"),
        (b'{"models": [', "model_residency_probe_failed"),
    ],
)
def test_api_ps_accepts_only_exact_empty_shape(payload: bytes, category: str | None) -> None:
    if category is None:
        verify_empty_model_residency(payload)
    else:
        with pytest.raises(IsolatedServerFailure) as exc_info:
            verify_empty_model_residency(payload)
        assert exc_info.value.category == category


@pytest.mark.parametrize(
    ("payload", "category"),
    [
        (b'{"models": [{"name": "x"}]}', "unexpected_model_residency"),
        (b'{"models": [], "extra": 1}', "model_residency_probe_failed"),
        (b"not json", "model_residency_probe_failed"),
    ],
)
def test_capture_api_ps_failures_keep_empty_proof_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    category: str,
) -> None:
    result, _, _ = _capture(
        tmp_path, monkeypatch, model_residency_probe=lambda port, timeout: payload
    )
    assert result["observations"]["api_ps_empty"] is False
    assert result["proofs"]["api_ps_exactly_empty"] is False
    assert category in {failure["category"] for failure in result["failures"]}


class CancelFailureApi(FakeLifecycleApi):
    def create_suspended(self, executable, arguments, environment):
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",):
            process.stdout = FakePipe(events=[("data", b"listening ready")], hang=True)
            self.cancel_failures.add(process.stdout)
        return process


def test_cleanup_failure_prevents_all_true_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    api = CancelFailureApi()
    api.clock = clock
    result, _, _ = _capture(
        tmp_path, monkeypatch, api=api, clock=clock, startup_timeout_seconds=0.1
    )
    assert "io_cancellation_failed" in {
        failure["category"] for failure in result["failures"]
    }
    assert not all(result["proofs"].values())
    assert result["proofs"]["shutdown_confirmed"] is False


class EndpointSurvivorApi(FakeLifecycleApi):
    def tcp_listener_rows(self) -> tuple[TcpListenerRow, ...]:
        self.calls.append("tcp_listener_rows")
        if self.active_server_id is None:
            return ()
        return (TcpListenerRow("127.0.0.1", self.active_server_port, self.active_server_id),)


def test_endpoint_closure_failure_prevents_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _capture(tmp_path, monkeypatch, api=EndpointSurvivorApi())
    assert "port_not_closed" in {failure["category"] for failure in result["failures"]}
    assert result["proofs"]["endpoint_closed"] is False
    assert not all(result["proofs"].values())


def test_capture_writes_no_raw_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"synthetic unreviewed executable")
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no text writes")))
    monkeypatch.setattr(Path, "write_bytes", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no byte writes")))
    monkeypatch.setattr(dialect_capture, "choose_loopback_port", lambda exclusions: 54321)
    result = DialectCaptureSession(
        executable,
        startup_timeout_seconds=0.2,
        version_timeout_seconds=0.2,
        cleanup_timeout_seconds=0.2,
        api=FakeLifecycleApi(),
        api_version_probe=lambda port, timeout: "0.32.1",
        model_residency_probe=_empty_ps,
        temp_parent=tmp_path,
    ).run()
    assert result["failures"] == []
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert after == before


def _assert_no_raw_identity(value, *, raw_pids: set[int]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert "pid" not in str(key).casefold()
            _assert_no_raw_identity(child, raw_pids=raw_pids)
    elif isinstance(value, list):
        for child in value:
            _assert_no_raw_identity(child, raw_pids=raw_pids)
    elif isinstance(value, str):
        remainder = value.replace("<PATH>", "")
        assert "\\" not in remainder and "/" not in remainder
        assert all(str(pid) not in value for pid in raw_pids)


def test_success_and_failure_results_contain_no_raw_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeLifecycleApi(
        server_log=(
            rb"starting server version 0.32.1 listening on 127.0.0.1:54321 "
            rb"path=C:\Users\alice\ollama.exe pid=4201"
        )
    )
    success, _, _ = _capture(tmp_path / "success", monkeypatch, api=api)
    clock = FakeClock()
    failed_api = NeverReadyApi(server_log=b"listening on 127.0.0.1:54321")
    failed_api.clock = clock
    failure, _, _ = _capture(
        tmp_path / "failure",
        monkeypatch,
        api=failed_api,
        clock=clock,
        startup_timeout_seconds=0.1,
    )
    _assert_no_raw_identity(success, raw_pids=set(api.processes))
    _assert_no_raw_identity(failure, raw_pids=set(failed_api.processes))


def test_capture_generates_no_dialect_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = isolated_server.REVIEWED_DIALECT_REGISTRY
    result, _, _ = _capture(tmp_path, monkeypatch)
    assert result["failures"] == []
    assert isolated_server.REVIEWED_DIALECT_REGISTRY is registry
    assert dict(registry) == {}


def test_capture_validator_rejects_unknown_keys_and_non_lowercase_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _capture(tmp_path, monkeypatch)
    result["unknown"] = True
    assert validate_capture_result(result)
    del result["unknown"]
    result["executable"]["sha256"] = result["executable"]["sha256"].upper()
    assert validate_capture_result(result)


def test_validator_accepts_a_valid_normal_capture_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 5, baseline: an ordinary successful capture still validates."""

    result, _, _ = _capture(tmp_path, monkeypatch)
    assert result["failures"] == []
    assert validate_capture_result(result) == []


def test_validator_accepts_designated_replacement_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 5: designated redaction tokens must validate cleanly."""

    result, _, _ = _capture(tmp_path, monkeypatch)
    result["observations"]["version_command_lines"] = [
        "path=<PATH> host=<HOST> port=<PORT> secret=<SECRET> "
        "pid=<PID> pipe=<PIPE> handle=<HANDLE> url=<URL>"
    ]
    assert validate_capture_result(result) == []


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\alice\ollama.exe",
        r"\\server\share\path",
        "/home/alice/.ollama/models",
        "127.0.0.1",
        "listening on 127.0.0.1:54321",
        "host=corp-server-01",
        "port=11434",
        "http://example.com/path",
        "http://user:pass@host:1234/path?token=abc",
        "Authorization: Bearer sk-abcdef123456",
        "Bearer sk-abcdef123456",
        "api_key=sk-live-abc123",
        "password=hunter2",
        "HTTPS_PROXY=http://proxy.internal:8080",
        "pid=4821",
        r"\\.\pipe\ollama-abc123",
        "handle=0x1F4",
        "value\x00withnul",
    ],
)
def test_validator_rejects_every_injected_forbidden_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Blocker 5: validate_capture_result must independently reject raw
    sensitive forms injected directly, without ever calling the
    redactor."""

    result, _, _ = _capture(tmp_path, monkeypatch)
    assert validate_capture_result(result) == []
    result["observations"]["version_command_lines"] = [value]
    assert validate_capture_result(result) != []


def test_validator_rejects_aggregate_version_lines_over_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 5: many individually valid small lines whose combined
    UTF-8 size exceeds MAX_VERSION_OUTPUT_BYTES must be rejected, even
    though no single line trips the per-line character cap."""

    result, _, _ = _capture(tmp_path, monkeypatch)
    line = "v0.1.0"
    assert len(line) <= MAX_EVIDENCE_LINE_CHARS
    count = MAX_VERSION_OUTPUT_BYTES // len(line.encode("utf-8")) + 1
    result["observations"]["version_command_lines"] = [line] * count
    assert validate_capture_result(result) != []


def test_validator_rejects_multibyte_lines_over_byte_cap_under_char_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 5: a multibyte Unicode collection that stays under the
    per-line character cap can still exceed the aggregate byte cap, and
    must be rejected on encoded bytes, not Python character count."""

    result, _, _ = _capture(tmp_path, monkeypatch)
    line = "é" * 300  # 300 chars (< 512 char cap), 600 UTF-8 bytes
    assert len(line) < MAX_EVIDENCE_LINE_CHARS
    lines = [line] * 8  # 8 * 600 = 4800 bytes > MAX_VERSION_OUTPUT_BYTES
    assert sum(len(entry) for entry in lines) < MAX_VERSION_OUTPUT_BYTES
    assert sum(len(entry.encode("utf-8")) for entry in lines) > MAX_VERSION_OUTPUT_BYTES
    result["observations"]["version_command_lines"] = lines
    assert validate_capture_result(result) != []
