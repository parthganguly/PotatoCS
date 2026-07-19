from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import pytest

from odysseus_desktop_backend.runtime_bench.__main__ import main
from odysseus_desktop_backend.runtime_bench.isolated_server import (
    ATTESTATION_ARTIFACT_KIND,
    ATTESTATION_SCHEMA_VERSION,
    FIRST_LOG_BYTES,
    FIXED_INTERNAL_ENV_KEYS,
    LAST_LOG_BYTES,
    PHASE1_FAILURE_CATEGORIES,
    USER_OVERRIDE_ENV_KEYS,
    BinaryIdentity,
    BoundedLogCapture,
    CreatedProcess,
    IsolatedOllamaServer,
    IsolatedServerFailure,
    Phase1ContractError,
    StartupDialect,
    build_child_environment,
    build_dry_run_plan,
    choose_loopback_port,
    compare_requested_attestation,
    create_session_space,
    empty_attestation_artifact,
    normalize_ollama_version,
    parse_startup_attestation,
    probe_binary_identity,
    teardown_session_space,
    validate_attestation_artifact,
    validate_user_overrides,
)


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
            port=54321,
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


def test_binary_identity_persists_only_basename_hash_and_normalized_version(tmp_path: Path) -> None:
    executable = tmp_path / "private" / "ollama.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    identity = probe_binary_identity(executable, command_probe=lambda _: "ollama version is 0.32.1")
    assert identity == BinaryIdentity(
        executable_basename="ollama.exe",
        executable_sha256=hashlib.sha256(b"synthetic executable").hexdigest(),
        binary_version="0.32.1",
    )
    assert str(tmp_path) not in repr(identity)


def _dialect(identity: BinaryIdentity, dialect: int = 1) -> StartupDialect:
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
            "flash_attention": (re.compile(r"--flash-attn\s+(\S+)"), "runner_log"),
            "kv_cache_type": (re.compile(r"--cache-type-k\s+(\S+)"), "runner_log"),
        },
    )


def _identity() -> BinaryIdentity:
    return BinaryIdentity("ollama.exe", "a" * 64, "0.32.1")


def test_attestation_parser_supports_fixture_dialect_one() -> None:
    identity, settings = parse_startup_attestation(
        b"server_version=0.32.1 noprune=true no_cloud=true",
        binary_identity=_identity(),
        api_version_raw="0.32.1",
        dialect=_dialect(_identity(), 1),
    )
    assert identity["startup_version"] == {"state": "attested", "value": "0.32.1", "source": "startup_log"}
    assert settings["noprune"] == {"state": "attested", "value": "true", "source": "startup_log"}


def test_attestation_parser_supports_fixture_dialect_two() -> None:
    _, settings = parse_startup_attestation(
        b'{"version":"0.32.1","OLLAMA_NOPRUNE":"1"}\nrunner --flash-attn on --cache-type-k q8_0',
        binary_identity=_identity(),
        api_version_raw="ollama version 0.32.1",
        dialect=_dialect(_identity(), 2),
    )
    assert settings["flash_attention"]["value"] == "on"
    assert settings["kv_cache_type"]["value"] == "q8_0"


def test_unparseable_startup_version_is_typed_unattested_not_failure() -> None:
    identity, settings = parse_startup_attestation(
        b"a future log dialect with no stable version field",
        binary_identity=_identity(),
        api_version_raw="0.32.1",
        dialect=_dialect(_identity(), 1),
    )
    assert identity["startup_version"] == {"state": "unattested", "value": None, "source": "unattested"}
    assert all(record["state"] == "unattested" for record in settings.values())


def test_requested_settings_require_attestation_and_must_match() -> None:
    unattested = {key: {"state": "unattested", "value": None, "source": "unattested"} for key in {
        "noprune", "no_cloud", "flash_attention", "kv_cache_type", "keep_alive", "context_length"
    }}
    assert [failure.category for failure in compare_requested_attestation({}, unattested)] == ["attestation_missing"]
    attested = {key: dict(value) for key, value in unattested.items()}
    attested["noprune"] = {"state": "attested", "value": "1", "source": "startup_log"}
    attested["no_cloud"] = {"state": "attested", "value": "true", "source": "startup_log"}
    attested["flash_attention"] = {"state": "attested", "value": "off", "source": "runner_log"}
    failures = compare_requested_attestation({"OLLAMA_FLASH_ATTENTION": "1"}, attested)
    assert [failure.category for failure in failures] == [
        "attestation_mismatch"
    ]


@pytest.mark.parametrize(("api", "startup"), [("0.32.2", None), ("0.32.1", b"server_version=0.32.2")])
def test_normalized_runtime_identity_mismatch_fails_closed(api: str, startup: bytes | None) -> None:
    raw = startup if startup is not None else b"future dialect"
    with pytest.raises(IsolatedServerFailure, match="runtime_identity_mismatch"):
        parse_startup_attestation(
            raw,
            binary_identity=_identity(),
            api_version_raw=api,
            dialect=_dialect(_identity(), 1),
        )


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


class _BrokenPipe:
    def read(self, size: int) -> bytes:
        raise OSError("synthetic read failure")


def test_log_reader_failure_is_typed() -> None:
    capture = BoundedLogCapture()
    capture.drain(_BrokenPipe())
    assert capture.reader_failed is True


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


class FakeLifecycleApi:
    def __init__(self, *, logs: bytes = b"", descendants: set[int] | None = None) -> None:
        self.calls: list[str] = []
        self.terminated = False
        self.logs = logs
        self.descendants = set() if descendants is None else descendants

    def create_suspended(self, executable: Path, environment: dict[str, str]) -> CreatedProcess:
        self.calls.append("create_suspended")
        assert set(environment) == FIXED_INTERNAL_ENV_KEYS
        return CreatedProcess(4242, "process", "thread", io.BytesIO(self.logs), io.BytesIO())

    def create_job(self) -> str:
        self.calls.append("create_job")
        return "job"

    def configure_kill_on_close(self, job: str) -> None:
        self.calls.append("configure_job")

    def assign_process(self, job: str, process: CreatedProcess) -> None:
        self.calls.append("assign_process")

    def verify_job_assignment(self, job: str, process: CreatedProcess) -> bool:
        self.calls.append("verify_job")
        return True

    def resume_process(self, process: CreatedProcess) -> None:
        self.calls.append("resume_process")

    def process_exit_code(self, process: CreatedProcess) -> None:
        self.calls.append("process_exit_code")
        return None

    def listener_owner(self, port: int) -> int | None:
        self.calls.append("listener_owner_post" if self.terminated else "listener_owner_pre")
        return None if self.terminated else 4242

    def process_id_in_job(self, job: str, process_id: int) -> bool:
        self.calls.append("process_id_in_job")
        return False

    def terminate_job(self, job: str) -> None:
        self.calls.append("terminate_job")
        self.terminated = True

    def terminate_process(self, process: CreatedProcess) -> None:
        self.calls.append("terminate_process")
        self.terminated = True

    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool:
        self.calls.append("wait_process")
        return True

    def descendant_process_ids(self, process_id: int) -> set[int]:
        self.calls.append("descendant_process_ids")
        return self.descendants

    def close_handle(self, handle: str) -> None:
        self.calls.append(f"close_{handle}")


class ConfigureFailureApi(FakeLifecycleApi):
    def configure_kill_on_close(self, job: str) -> None:
        self.calls.append("configure_job")
        raise IsolatedServerFailure("job_limit_configuration_failed", win32_code=5)


class AlternateOwnerApi(FakeLifecycleApi):
    def __init__(self, *, in_job: bool) -> None:
        super().__init__()
        self.in_job = in_job

    def listener_owner(self, port: int) -> int | None:
        self.calls.append("listener_owner_post" if self.terminated else "listener_owner_pre")
        return None if self.terminated else 7777

    def process_id_in_job(self, job: str, process_id: int) -> bool:
        self.calls.append("process_id_in_job")
        assert process_id == 7777
        return self.in_job


def test_job_member_listener_is_owned_but_foreign_listener_is_never_contacted(tmp_path: Path) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    owned = AlternateOwnerApi(in_job=True)
    artifact = IsolatedOllamaServer(
        executable,
        api=owned,
        command_probe=lambda _: "0.32.1",
        api_version_probe=lambda port, timeout: "0.32.1",
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
        command_probe=lambda _: "0.32.1",
        api_version_probe=forbidden_probe,
        temp_parent=tmp_path,
    ).run()
    assert "port_hijacked" in {failure["category"] for failure in artifact["failures"]}
    assert contacted is False


def test_pre_assignment_failure_directly_terminates_suspended_process(tmp_path: Path) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    api = ConfigureFailureApi()
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        command_probe=lambda _: "0.32.1",
        api_version_probe=lambda port, timeout: "0.32.1",
        temp_parent=tmp_path,
    ).run()
    assert [failure["category"] for failure in artifact["failures"]] == ["job_limit_configuration_failed"]
    assert "terminate_process" in api.calls
    assert "terminate_job" not in api.calls
    assert api.calls.index("terminate_process") < api.calls.index("wait_process")


def test_synthetic_end_to_end_lifecycle_produces_private_closed_artifact(tmp_path: Path) -> None:
    executable = tmp_path / "ollama.exe"
    executable_bytes = b"synthetic ollama fixture"
    executable.write_bytes(executable_bytes)
    identity = BinaryIdentity("ollama.exe", hashlib.sha256(executable_bytes).hexdigest(), "0.32.1")
    api = FakeLifecycleApi(logs=b"server_version=0.32.1 noprune=true no_cloud=true")
    server = IsolatedOllamaServer(
        executable,
        api=api,
        command_probe=lambda _: "ollama version is 0.32.1",
        api_version_probe=lambda port, timeout: "0.32.1",
        dialect=_dialect(identity, 1),
        temp_parent=tmp_path,
    )
    artifact = server.run()
    assert validate_attestation_artifact(artifact) == []
    assert artifact["schema_version"] == ATTESTATION_SCHEMA_VERSION
    assert artifact["artifact_kind"] == ATTESTATION_ARTIFACT_KIND
    assert artifact["runtime_identity"]["startup_version"]["state"] == "attested"
    assert artifact["overall_diagnostic_evidence_state"] == "complete"
    assert artifact["failures"] == []
    serialized = json.dumps(artifact, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "4242" not in serialized
    assert "127.0.0.1" not in serialized
    assert api.calls[:6] == [
        "create_suspended",
        "create_job",
        "configure_job",
        "assign_process",
        "verify_job",
        "resume_process",
    ]
    assert api.calls.index("listener_owner_pre") < api.calls.index("terminate_job")
    assert api.calls.index("terminate_job") < api.calls.index("listener_owner_post")


def test_synthetic_orphan_is_reported_without_persisting_process_identity(tmp_path: Path) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    api = FakeLifecycleApi(descendants={9876})
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        command_probe=lambda _: "0.32.1",
        api_version_probe=lambda port, timeout: "0.32.1",
        temp_parent=tmp_path,
    ).run()
    assert artifact["orphan_verification"] == "survivor_detected"
    assert {failure["category"] for failure in artifact["failures"]} == {"attestation_missing", "orphaned_runner"}
    assert "9876" not in json.dumps(artifact)


def test_attestation_artifact_schema_rejects_unknown_and_private_fields(tmp_path: Path) -> None:
    artifact = empty_attestation_artifact()
    assert validate_attestation_artifact(artifact) == []
    artifact["pid"] = 123
    assert validate_attestation_artifact(artifact)
    artifact = empty_attestation_artifact()
    artifact["runtime_identity"]["executable_basename"] = str(tmp_path / "ollama.exe")
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


def test_cli_attest_defaults_to_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["attest"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run"
    assert output["would_spawn"] is False
