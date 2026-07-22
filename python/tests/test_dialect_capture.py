from __future__ import annotations

import json
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
