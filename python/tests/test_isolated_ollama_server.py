from __future__ import annotations

import hashlib
import io
import json
import re
import threading
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
    PHASE1_FAILURE_CATEGORIES,
    USER_OVERRIDE_ENV_KEYS,
    BinaryIdentity,
    BoundedLogCapture,
    CreatedProcess,
    IsolatedOllamaServer,
    IsolatedServerFailure,
    Phase1ContractError,
    StartupDialect,
    WindowsLifecycleApi,
    build_binary_identity,
    build_child_environment,
    build_dry_run_plan,
    choose_loopback_port,
    compare_requested_attestation,
    create_session_space,
    empty_attestation_artifact,
    normalize_ollama_version,
    parse_startup_attestation,
    hash_executable,
    run_owned_version_probe,
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
    identity = build_binary_identity(executable, "ollama version is 0.32.1")
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
            "no_cloud": (re.compile(r'"OLLAMA_NO_CLOUD":"([^"]+)"'), "startup_log"),
            "flash_attention": (re.compile(r"--flash-attn\s+(\S+)"), "runner_log"),
            "kv_cache_type": (re.compile(r"--cache-type-k\s+(\S+)"), "runner_log"),
        },
    )


def _identity() -> BinaryIdentity:
    return BinaryIdentity("ollama.exe", "a" * 64, "0.32.1")


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


class FakeAttributeKernel:
    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.fail_stage = fail_stage
        self.initialize_calls = 0
        self.updated_handles: tuple[int, ...] = ()
        self.deleted = 0

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

    def UpdateProcThreadAttribute(
        self, target, flags, attribute, value, size, previous, return_size
    ) -> int:
        handle_array = isolated_server.ctypes.cast(
            value,
            isolated_server.ctypes.POINTER(isolated_server.ctypes.c_void_p * 2),
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


def test_startupinfoex_inherits_only_stdout_and_stderr_handles() -> None:
    kernel = FakeAttributeKernel()
    api = _attribute_api(kernel)
    attributes = api._create_attribute_list(11, 12)
    api._delete_attribute_list(attributes)
    assert kernel.updated_handles == (11, 12)
    assert 999 not in kernel.updated_handles  # unrelated inheritable parent handle
    assert kernel.deleted == 1


@pytest.mark.parametrize("stage", ["initialize", "update"])
def test_attribute_list_api_failures_are_closed(stage: str) -> None:
    api = _attribute_api(FakeAttributeKernel(fail_stage=stage))
    with pytest.raises(IsolatedServerFailure, match="process_attribute_list_failed"):
        api._create_attribute_list(11, 12)


def test_attribute_list_cleanup_failure_is_closed() -> None:
    api = _attribute_api(FakeAttributeKernel(fail_stage="delete"))
    attributes = api._create_attribute_list(11, 12)
    with pytest.raises(IsolatedServerFailure, match="process_attribute_list_cleanup_failed"):
        api._delete_attribute_list(attributes)


def _register_test_dialect(
    monkeypatch: pytest.MonkeyPatch, executable: Path, dialect_number: int = 1
) -> BinaryIdentity:
    basename, digest = hash_executable(executable)
    identity = BinaryIdentity(basename, digest, "0.32.1")
    monkeypatch.setattr(
        isolated_server,
        "REVIEWED_DIALECT_REGISTRY",
        MappingProxyType({digest: _dialect(identity, dialect_number)}),
    )
    return identity


class FakeLifecycleApi:
    def __init__(
        self,
        *,
        logs: bytes = b"server_version=0.32.1 noprune=true no_cloud=true",
        version_output: bytes = b"ollama version is 0.32.1",
        server_descendants: set[int] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.logs = logs
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

    def _kind(self, process: CreatedProcess) -> str:
        return self.process_kind[process.process_id]

    def create_suspended(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> CreatedProcess:
        kind = "version" if arguments == ("--version",) else "server"
        self.calls.append(f"create_suspended_{kind}")
        assert set(environment) == FIXED_INTERNAL_ENV_KEYS
        self.environments.append(dict(environment))
        self.arguments.append(arguments)
        self.next_id += 1
        process_id = self.next_id
        output = self.version_output if kind == "version" else self.logs
        process = CreatedProcess(
            process_id,
            f"process-{process_id}",
            f"thread-{process_id}",
            io.BytesIO(output),
            io.BytesIO(),
        )
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

    def listener_owner(self, port: int) -> int | None:
        active = self.active_server_id
        post = active is None or active in self.terminated
        self.calls.append("listener_owner_post" if post else "listener_owner_pre")
        if post or port != self.active_server_port:
            return None
        return active

    def process_id_in_job(self, job: str, process_id: int) -> bool:
        self.calls.append("process_id_in_job")
        return self.job_process.get(job) == process_id

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

    def close_handle(self, handle: str) -> None:
        self.calls.append(f"close_{handle}")


def _owned_version_fixture(
    tmp_path: Path, api: FakeLifecycleApi, *, clock=None, sleeper=None
) -> bytes:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"owned version fixture")
    basename, digest = hash_executable(executable)
    space = create_session_space(tmp_path)
    try:
        environment = build_child_environment(
            executable=executable,
            space=space,
            port=1,
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
            clock=time.monotonic if clock is None else clock,
            sleeper=time.sleep if sleeper is None else sleeper,
        )
    finally:
        teardown_session_space(space)


def test_version_probe_is_job_owned_environment_isolated_and_bounded(tmp_path: Path) -> None:
    api = FakeLifecycleApi()
    output = _owned_version_fixture(tmp_path, api)
    assert output == b"ollama version is 0.32.1"
    assert api.arguments == [("--version",)]
    assert not ({"HTTP_PROXY", "OLLAMA_API_KEY"} & set(api.environments[0]))
    assert api.calls.index("create_suspended_version") < api.calls.index("create_job")
    assert api.calls.index("verify_job") < api.calls.index("resume_version")
    assert api.calls.index("terminate_job") < api.calls.index("descendant_process_ids")


def test_version_probe_rejects_finite_infinite_output_fixture(tmp_path: Path) -> None:
    api = FakeLifecycleApi(version_output=b"x" * 100_000)
    with pytest.raises(IsolatedServerFailure, match="version_probe_output_overflow"):
        _owned_version_fixture(tmp_path, api)


def test_version_capture_retains_at_most_four_kibibytes() -> None:
    capture = isolated_server.BoundedVersionCapture()
    capture.feed(b"x" * 100_000)
    assert len(capture.bytes()) == 4096
    assert capture.observed == 100_000
    assert capture.overflowed is True


class ContinuousPipe:
    def __init__(self) -> None:
        self.stopped = threading.Event()

    def read(self, size: int) -> bytes:
        if self.stopped.is_set():
            return b""
        return b"x" * min(size, 1024)

    def close(self) -> None:
        self.stopped.set()


class ContinuousVersionApi(FakeLifecycleApi):
    def __init__(self) -> None:
        super().__init__()
        self.pipe = ContinuousPipe()

    def create_suspended(
        self, executable: Path, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> CreatedProcess:
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("--version",):
            process.stdout = self.pipe
        return process

    def process_exit_code(self, process: CreatedProcess) -> int | None:
        if self._kind(process) == "version" and process.process_id not in self.terminated:
            return None
        return super().process_exit_code(process)

    def terminate_job(self, job: str) -> None:
        super().terminate_job(job)
        self.pipe.stopped.set()


def test_continuously_producing_version_output_is_killed_and_drained(tmp_path: Path) -> None:
    api = ContinuousVersionApi()
    with pytest.raises(IsolatedServerFailure, match="version_probe_output_overflow"):
        _owned_version_fixture(tmp_path, api)
    assert api.pipe.stopped.is_set()
    assert "terminate_job" in api.calls


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


class VersionTimeoutApi(FakeLifecycleApi):
    def process_exit_code(self, process: CreatedProcess) -> int | None:
        if self._kind(process) == "version" and process.process_id not in self.terminated:
            return None
        return super().process_exit_code(process)


def test_version_probe_enforces_true_wall_clock_timeout(tmp_path: Path) -> None:
    clock = FakeClock()
    api = VersionTimeoutApi(version_output=b"")
    with pytest.raises(IsolatedServerFailure, match="version_probe_timeout"):
        _owned_version_fixture(tmp_path, api, clock=clock, sleeper=clock.sleep)
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


class ConfigureFailureApi(FakeLifecycleApi):
    def __init__(self) -> None:
        super().__init__()
        self.configure_count = 0

    def configure_kill_on_close(self, job: str) -> None:
        self.configure_count += 1
        self.calls.append("configure_job")
        if self.configure_count == 2:
            raise IsolatedServerFailure("job_limit_configuration_failed", win32_code=5)


class AlternateOwnerApi(FakeLifecycleApi):
    def __init__(self, *, in_job: bool) -> None:
        super().__init__()
        self.in_job = in_job

    def listener_owner(self, port: int) -> int | None:
        active = self.active_server_id
        post = active is None or active in self.terminated
        self.calls.append("listener_owner_post" if post else "listener_owner_pre")
        return None if post else 7777

    def process_id_in_job(self, job: str, process_id: int) -> bool:
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
        temp_parent=tmp_path,
    ).run()
    assert "port_hijacked" in {failure["category"] for failure in artifact["failures"]}
    assert contacted is False


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
        temp_parent=tmp_path,
    ).run()
    assert [failure["category"] for failure in artifact["failures"]] == [
        "job_limit_configuration_failed"
    ]
    assert "terminate_process" in api.calls


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
        temp_parent=tmp_path,
    ).run()
    assert validate_attestation_artifact(artifact) == []
    assert artifact["schema_version"] == ATTESTATION_SCHEMA_VERSION
    assert artifact["artifact_kind"] == ATTESTATION_ARTIFACT_KIND
    assert artifact["runtime_identity"]["startup_version"]["state"] == "attested"
    assert artifact["overall_diagnostic_evidence_state"] == "complete"
    assert artifact["failures"] == []
    serialized = json.dumps(artifact, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert all(str(process_id) not in serialized for process_id in api.processes)
    assert "127.0.0.1" not in serialized
    assert api.arguments == [("--version",), ("serve",)]
    assert all(set(environment) == FIXED_INTERNAL_ENV_KEYS for environment in api.environments)


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
        temp_parent=tmp_path,
    ).run()
    assert artifact["orphan_verification"] == "survivor_detected"
    assert {failure["category"] for failure in artifact["failures"]} == {
        "orphaned_runner"
    }
    assert "9876" not in json.dumps(artifact)


def test_unknown_dialect_refuses_before_any_process_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"unknown dialect fixture")
    monkeypatch.setattr(
        isolated_server, "REVIEWED_DIALECT_REGISTRY", MappingProxyType({})
    )
    api = FakeLifecycleApi()
    artifact = IsolatedOllamaServer(executable, api=api, temp_parent=tmp_path).run()
    assert [failure["category"] for failure in artifact["failures"]] == [
        "attestation_dialect_unavailable"
    ]
    assert api.arguments == []
    assert artifact["temporary_space_torn_down"] is False


class DelayedPipe:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.release = threading.Event()
        self.sent = False

    def read(self, size: int) -> bytes:
        self.release.wait(timeout=2.0)
        if not self.release.is_set() or self.sent:
            return b""
        self.sent = True
        return self.payload

    def close(self) -> None:
        self.release.set()


class DelayedLogApi(FakeLifecycleApi):
    def __init__(self) -> None:
        super().__init__(logs=b"")
        self.delayed = DelayedPipe(
            b"server_version=0.32.1 noprune=true no_cloud=true"
        )

    def create_suspended(
        self, executable: Path, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> CreatedProcess:
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",):
            process.stdout = self.delayed
        return process


def test_attestation_waits_for_configuration_logs_after_api_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"delayed logs fixture")
    _register_test_dialect(monkeypatch, executable)
    api = DelayedLogApi()
    api_ready = False
    wait_count = 0

    def version_api(port: int, timeout: float) -> str:
        nonlocal api_ready
        api_ready = True
        return "0.32.1"

    def release_after_readiness(duration: float) -> None:
        nonlocal wait_count
        assert api_ready is True
        wait_count += 1
        api.delayed.release.set()
        time.sleep(0.01)

    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=version_api,
        sleeper=release_after_readiness,
        temp_parent=tmp_path,
    ).run()
    assert artifact["failures"] == []
    assert artifact["attested_settings"]["noprune"]["value"] == "true"
    assert wait_count >= 1


def test_attestation_deadline_is_separate_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"missing markers fixture")
    _register_test_dialect(monkeypatch, executable)
    clock = FakeClock()
    api = FakeLifecycleApi(logs=b"server_version=0.32.1")
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        attestation_timeout_seconds=0.2,
        clock=clock,
        sleeper=clock.sleep,
        temp_parent=tmp_path,
    ).run()
    missing = next(
        failure for failure in artifact["failures"] if failure["category"] == "attestation_missing"
    )
    assert missing["numeric_metadata"] == {"timeout_ms": 200}
    assert artifact["readiness_duration_ms"] == 0


class SlowPrelaunchApi(FakeLifecycleApi):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    def create_suspended(
        self, executable: Path, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> CreatedProcess:
        if arguments == ("serve",):
            self.clock.now += 50.0
        return super().create_suspended(executable, arguments, environment)

    def resume_process(self, process: CreatedProcess) -> None:
        if self._kind(process) == "server":
            self.clock.now += 0.1
        super().resume_process(process)


def test_readiness_duration_excludes_all_prelaunch_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"readiness clock fixture")
    _register_test_dialect(monkeypatch, executable)
    clock = FakeClock()
    api = SlowPrelaunchApi(clock)

    def api_probe(port: int, timeout: float) -> str:
        clock.now += 0.2
        return "0.32.1"

    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=api_probe,
        clock=clock,
        sleeper=clock.sleep,
        temp_parent=tmp_path,
    ).run()
    assert artifact["readiness_duration_ms"] == 300
    assert artifact["readiness_duration_ms"] < 50_000


class ReplacingExecutableApi(FakeLifecycleApi):
    def __init__(self, executable: Path) -> None:
        super().__init__()
        self.executable = executable

    def create_suspended(
        self, executable: Path, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> CreatedProcess:
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",):
            self.executable.write_bytes(b"replaced after suspended creation")
        return process


def test_executable_replacement_is_detected_before_server_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"original executable fixture")
    _register_test_dialect(monkeypatch, executable)
    api = ReplacingExecutableApi(executable)
    artifact = IsolatedOllamaServer(
        executable,
        api=api,
        api_version_probe=lambda port, timeout: "0.32.1",
        temp_parent=tmp_path,
    ).run()
    assert [failure["category"] for failure in artifact["failures"]] == [
        "runtime_identity_mismatch"
    ]
    assert "resume_server" not in api.calls
    assert api.arguments == [("--version",), ("serve",)]
    serialized = json.dumps(artifact)
    assert str(tmp_path) not in serialized


class BindRaceApi(FakeLifecycleApi):
    def __init__(self, *, races: int) -> None:
        super().__init__()
        self.races_remaining = races
        self.racing_processes: set[int] = set()

    def create_suspended(
        self, executable: Path, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> CreatedProcess:
        process = super().create_suspended(executable, arguments, environment)
        if arguments == ("serve",) and self.races_remaining > 0:
            self.races_remaining -= 1
            self.racing_processes.add(process.process_id)
        return process

    def listener_owner(self, port: int) -> int | None:
        active = self.active_server_id
        if active in self.racing_processes and active not in self.terminated:
            self.calls.append("listener_owner_race")
            return 7777
        return super().listener_owner(port)

    def process_exit_code(self, process: CreatedProcess) -> int | None:
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
    first_post = api.calls.index("listener_owner_post")
    second_create = api.calls.index("create_suspended_server", api.calls.index("create_suspended_server") + 1)
    assert first_post < second_create
    assert all(process.stdout.closed and process.stderr.closed for process in api.processes.values())
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
        launch_attempts=3,
        temp_parent=tmp_path,
    ).run()
    failure = next(
        failure for failure in artifact["failures"] if failure["category"] == "port_bind_failed"
    )
    assert failure["numeric_metadata"] == {"attempts": 3}
    assert api.arguments.count(("serve",)) == 3


class ArbitraryCrashApi(FakeLifecycleApi):
    def listener_owner(self, port: int) -> None:
        self.calls.append("listener_owner_none")
        return None

    def process_exit_code(self, process: CreatedProcess) -> int | None:
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
        launch_attempts=3,
        temp_parent=tmp_path,
    ).run()
    assert "startup_process_exit" in {
        failure["category"] for failure in artifact["failures"]
    }
    assert api.arguments.count(("serve",)) == 1


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
