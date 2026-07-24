"""Tests for the Colibrì Stage 2A real one-token runner (Part 5).

Every test here uses a synthetic fake ``LifecycleApi`` and a fake clock.
No test launches a real process, touches the network, or reads the
ordinary Ollama/Colibrì model store.
"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from odysseus_desktop_backend.runtime_bench.isolated_server import CreatedProcess, IsolatedServerFailure
from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services import colibri_stage2_manifest as manifest_mod
from odysseus_desktop_backend.services import colibri_stage2_reference as ref_mod
from odysseus_desktop_backend.services import colibri_stage2_runner as runner


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakePipe:
    def __init__(self, events: list[tuple[str, bytes]]) -> None:
        self.events = list(events)
        self.pending = False
        self.eof = False


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def time(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeApi:
    def __init__(
        self,
        *,
        stdout: bytes = b"Matching tokens: 1/1\n",
        stderr: bytes = b"",
        exit_code: int | None = 0,
        descendants: set[int] | None = None,
        clock: FakeClock | None = None,
        fail_wait_process: bool = False,
        image_matches: bool = True,
        job_assignment_ok: bool = True,
    ) -> None:
        self.stdout_events: list[tuple[str, bytes]] = [("data", stdout)] if stdout else []
        self.stdout_events.append(("eof", b""))
        self.stderr_events: list[tuple[str, bytes]] = [("data", stderr)] if stderr else []
        self.stderr_events.append(("eof", b""))
        self.exit_code = exit_code
        self.descendants = set() if descendants is None else descendants
        self.clock = clock
        self.fail_wait_process = fail_wait_process
        self.image_matches = image_matches
        self.job_assignment_ok = job_assignment_ok
        self.calls: list[str] = []
        self.create_suspended_calls: list[tuple[Path, tuple[str, ...], dict[str, str]]] = []
        self.terminated: list[str] = []
        self.closed_handles: list[Any] = []
        self.job_process: dict[str, int] = {}
        self._never_exits = False

    def create_suspended(self, executable: Path, arguments: tuple[str, ...], environment) -> CreatedProcess:
        self.calls.append("create_suspended")
        self.create_suspended_calls.append((executable, arguments, dict(environment)))
        stdout = FakePipe(self.stdout_events)
        stderr = FakePipe(self.stderr_events)
        return CreatedProcess(9001, "process-handle", "thread-handle", stdout, stderr)

    def process_image_matches(self, process: CreatedProcess, executable: Path) -> bool:
        self.calls.append("process_image_matches")
        return self.image_matches

    def create_job(self) -> str:
        self.calls.append("create_job")
        return "job-1"

    def configure_kill_on_close(self, job: str) -> None:
        self.calls.append("configure_kill_on_close")

    def assign_process(self, job: str, process: CreatedProcess) -> None:
        self.calls.append("assign_process")
        self.job_process[job] = process.process_id

    def verify_job_assignment(self, job: str, process: CreatedProcess) -> bool:
        self.calls.append("verify_job_assignment")
        return self.job_assignment_ok and self.job_process.get(job) == process.process_id

    def resume_process(self, process: CreatedProcess) -> None:
        self.calls.append("resume_process")

    def process_exit_code(self, process: CreatedProcess) -> int | None:
        self.calls.append("process_exit_code")
        return self.exit_code

    def terminate_job(self, job: str) -> None:
        self.calls.append("terminate_job")
        self.terminated.append(job)

    def terminate_process(self, process: CreatedProcess) -> None:
        self.calls.append("terminate_process")
        self.terminated.append(f"process-{process.process_id}")

    def wait_process(self, process: CreatedProcess, timeout_ms: int) -> bool:
        self.calls.append("wait_process")
        return not self.fail_wait_process

    def descendant_process_ids(self, process_id: int) -> set[int]:
        self.calls.append("descendant_process_ids")
        return set(self.descendants)

    def post_overlapped_read(self, pipe: FakePipe) -> None:
        if not pipe.eof:
            pipe.pending = True

    def finish_overlapped_read(self, pipe: FakePipe) -> tuple[str, bytes]:
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
        pass

    def wait_for_completion(self, pipes, process, timeout_ms: int) -> None:
        if self.clock is not None:
            self.clock.advance(timeout_ms / 1000.0)

    def close_pipe(self, pipe: FakePipe) -> None:
        pass

    def close_handle(self, handle: Any) -> None:
        self.closed_handles.append(handle)


class NeverExitsApi(FakeApi):
    """process_exit_code always returns None -- forces the deadline path.

    Overlapped reads stay pending indefinitely (as a real hung child's
    would) until ``cancel_overlapped_read`` is called during cleanup, at
    which point they resolve as ``aborted`` -- exactly what a real
    ``CancelIoEx`` confirmation looks like, so cleanup can complete
    cleanly after the timeout fires.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cancelled: set[int] = set()

    def process_exit_code(self, process: CreatedProcess) -> int | None:
        self.calls.append("process_exit_code")
        return None

    def post_overlapped_read(self, pipe: FakePipe) -> None:
        pipe.pending = True

    def finish_overlapped_read(self, pipe: FakePipe) -> tuple[str, bytes]:
        if id(pipe) in self._cancelled:
            return ("aborted", b"")
        return ("pending", b"")

    def cancel_overlapped_read(self, pipe: FakePipe) -> None:
        self._cancelled.add(id(pipe))


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------


def _make_manifest(*, engine_bytes: bytes, config_bytes: bytes, shard_bytes: tuple[bytes, bytes, bytes]) -> manifest_mod.OlmoeModelManifest:
    ref_bytes = ref_mod.canonical_reference_bytes()
    return manifest_mod.OlmoeModelManifest(
        model_repository=common.PINNED_MODEL_REPOSITORY,
        model_revision=common.PINNED_MODEL_REVISION,
        license_identifier=common.PINNED_LICENSE_IDENTIFIER,
        colibri_commit=common.PINNED_COLIBRI_COMMIT,
        converter_source_sha256="a" * 64,
        engine_basename=common.EXPECTED_ENGINE_BASENAME,
        engine_size_bytes=len(engine_bytes),
        engine_sha256=_sha256_bytes(engine_bytes),
        config_basename=common.EXPECTED_CONFIG_BASENAME,
        config_size_bytes=len(config_bytes),
        config_sha256=_sha256_bytes(config_bytes),
        shard_basenames=common.EXPECTED_SHARD_BASENAMES,
        shard_size_bytes=tuple(len(b) for b in shard_bytes),
        shard_sha256=tuple(_sha256_bytes(b) for b in shard_bytes),
        ref_basename=common.EXPECTED_REF_BASENAME,
        ref_size_bytes=len(ref_bytes),
        ref_sha256=ref_mod.canonical_reference_sha256(),
        conversion_dependency_versions={"python": "3.11.9"},
        evidence_schema_version=common.MANIFEST_EVIDENCE_SCHEMA_VERSION,
    )


class _Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.exe = tmp_path / "olmoe.exe"
        self.exe.write_bytes(b"fake engine bytes")
        self.model_dir = tmp_path / "converted"
        self.model_dir.mkdir()
        self.config = self.model_dir / "config.json"
        self.config.write_bytes(b'{"fake": true}')
        self.shard_bytes = (b"shard-0", b"shard-1", b"shard-2")
        for name, data in zip(common.EXPECTED_SHARD_BASENAMES, self.shard_bytes):
            (self.model_dir / name).write_bytes(data)
        self.manifest = _make_manifest(
            engine_bytes=self.exe.read_bytes(),
            config_bytes=self.config.read_bytes(),
            shard_bytes=self.shard_bytes,
        )


@pytest.fixture()
def fixture(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


@pytest.fixture()
def registered(fixture: _Fixture, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: fixture.manifest}),
    )
    return fixture


def _run(fixture: _Fixture, api: FakeApi, **overrides: Any) -> runner.OneTokenRunResult:
    kwargs: dict[str, Any] = dict(
        olmoe_exe=fixture.exe,
        converted_model_dir=fixture.model_dir,
        api=api,
        approved=True,
        interactive_check=lambda: True,
        reference_session_parent=fixture.root,
    )
    kwargs.update(overrides)
    if "clock" not in kwargs and api.clock is not None:
        kwargs["clock"] = api.clock.time
    return runner.run_one_token_proof(**kwargs)


# ---------------------------------------------------------------------------
# Manifest gate: impossible to launch before process creation
# ---------------------------------------------------------------------------


def test_empty_registry_blocks_before_process_creation(fixture: _Fixture) -> None:
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="reviewed_model_manifest_unavailable"):
        _run(fixture, api)
    assert api.calls == []


def test_malformed_registry_entry_blocks_before_process_creation(
    fixture: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        manifest_mod, "REVIEWED_OLMOE_MODEL_REGISTRY", MappingProxyType({"wrong-key": fixture.manifest})
    )
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="reviewed_model_manifest_unavailable"):
        _run(fixture, api)
    assert api.calls == []


def test_noninteractive_approval_is_rejected_before_process_creation(registered: _Fixture) -> None:
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="noninteractive_approval_rejected"):
        _run(registered, api, interactive_check=lambda: False)
    assert api.calls == []


def test_missing_approval_flag_is_rejected_before_process_creation(registered: _Fixture) -> None:
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="noninteractive_approval_rejected"):
        _run(registered, api, approved=False)
    assert api.calls == []


# ---------------------------------------------------------------------------
# Converted-input identity checks (before process creation)
# ---------------------------------------------------------------------------


def test_unknown_engine_hash_is_rejected(registered: _Fixture) -> None:
    registered.exe.write_bytes(b"a different engine entirely")
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="runtime_identity_mismatch"):
        _run(registered, api)
    assert api.calls == []


def test_missing_shard_is_rejected(registered: _Fixture) -> None:
    (registered.model_dir / common.EXPECTED_SHARD_BASENAMES[0]).unlink()
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="missing_converted_shard"):
        _run(registered, api)
    assert api.calls == []


def test_wrong_shard_content_is_rejected(registered: _Fixture) -> None:
    (registered.model_dir / common.EXPECTED_SHARD_BASENAMES[1]).write_bytes(b"tampered content")
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="missing_converted_shard"):
        _run(registered, api)
    assert api.calls == []


def test_extra_unexpected_safetensor_shard_is_rejected(registered: _Fixture) -> None:
    (registered.model_dir / "sneaky-extra.safetensors").write_bytes(b"extra")
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="unknown_converted_shard"):
        _run(registered, api)
    assert api.calls == []


def test_config_size_mismatch_is_rejected(registered: _Fixture) -> None:
    registered.config.write_bytes(b'{"fake": true, "padded": "xxxxxxxxxxxxxxxxxxxx"}')
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="missing_converted_shard"):
        _run(registered, api)
    assert api.calls == []


def test_reparse_point_escape_is_rejected(registered: _Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    real_lstat = runner.os.lstat
    reparse_target = registered.model_dir / common.EXPECTED_SHARD_BASENAMES[0]

    class _FakeStatResult:
        def __init__(self, real_result: Any) -> None:
            self._real = real_result
            self.st_mode = real_result.st_mode
            self.st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

    def _fake_lstat(path: Any, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == reparse_target:
            return _FakeStatResult(result)
        return result

    monkeypatch.setattr(runner.os, "lstat", _fake_lstat)
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="reparse_point_rejected"):
        _run(registered, api)
    assert api.calls == []


# ---------------------------------------------------------------------------
# argv / environment / cap / bits / no external tokenizer
# ---------------------------------------------------------------------------


def test_argv_and_environment_are_exact(registered: _Fixture) -> None:
    api = FakeApi()
    result = _run(registered, api)
    assert result.ok is True
    assert len(api.create_suspended_calls) == 1
    executable, arguments, environment = api.create_suspended_calls[0]
    assert executable == registered.exe
    assert arguments[0] == "8"
    assert arguments[1] == "8"
    assert len(arguments) == 3
    ref_path = Path(arguments[2])
    assert ref_path.name == common.EXPECTED_REF_BASENAME
    assert set(environment) == runner.CHILD_ENV_FIXED_KEYS
    assert environment["SNAP"] == str(registered.model_dir)
    assert environment["OMP_NUM_THREADS"] == "12"


def test_cap_and_bits_constants_are_eight() -> None:
    assert common.CAP_ARGUMENT == "8"
    assert common.BITS_ARGUMENT == "8"


def test_run_one_token_proof_has_no_external_tokenizer_or_ref_path_parameter() -> None:
    signature = inspect.signature(runner.run_one_token_proof)
    names = set(signature.parameters)
    assert "ref_path" not in names
    assert "tokenizer" not in names
    assert "tokenizer_path" not in names
    assert "prompt" not in names


# ---------------------------------------------------------------------------
# Output parsing: success and every rejection shape
# ---------------------------------------------------------------------------


def test_exact_one_of_one_success(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"Matching tokens: 1/1\n")
    result = _run(registered, api)
    assert result.ok is True
    assert result.category == "passed"
    assert result.matched_count == 1
    assert result.expected_count == 1
    assert result.token_id == 7785
    assert result.exit_code == 0
    assert result.cleanup_complete is True
    assert result.reference_removed is True
    assert result.vram_state == "not_applicable"
    assert result.evidence_sha256 is not None


def test_zero_of_one_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"Matching tokens: 0/1\n")
    with pytest.raises(runner.ColibriStage2Failure, match="match_count_mismatch"):
        _run(registered, api)


def test_unexpected_denominator_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"Matching tokens: 1/2\n")
    with pytest.raises(runner.ColibriStage2Failure, match="match_count_mismatch"):
        _run(registered, api)


def test_duplicate_matching_lines_are_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"Matching tokens: 1/1\nMatching tokens: 1/1\n")
    with pytest.raises(runner.ColibriStage2Failure, match="duplicate_match_line"):
        _run(registered, api)


def test_malformed_output_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"nothing useful here\n")
    with pytest.raises(runner.ColibriStage2Failure, match="malformed_output"):
        _run(registered, api)


def test_unexpected_extra_output_around_match_line_still_parses(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"warming up\nMatching tokens: 1/1\ndone\n")
    result = _run(registered, api)
    assert result.ok is True


def test_nonzero_exit_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(exit_code=1)
    with pytest.raises(runner.ColibriStage2Failure, match="nonzero_exit"):
        _run(registered, api)


def test_stderr_present_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stderr=b"unexpected warning\n")
    with pytest.raises(runner.ColibriStage2Failure, match="stderr_present"):
        _run(registered, api)


def test_stdout_overflow_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"x" * 5000)
    with pytest.raises(runner.ColibriStage2Failure, match="output_overflow"):
        _run(registered, api)


def test_stderr_overflow_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stderr=b"x" * 5000)
    with pytest.raises(runner.ColibriStage2Failure, match="output_overflow"):
        _run(registered, api)


# ---------------------------------------------------------------------------
# Deadlines, cleanup, orphans
# ---------------------------------------------------------------------------


def test_absolute_timeout_terminates_job_and_fails_closed(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the absolute deadline so the fake clock only needs a handful
    # of wait-slice advances to reach it, instead of simulating the full
    # 900-second production window one wait-slice at a time.
    monkeypatch.setattr(runner, "_TOTAL_RUN_DEADLINE_SECONDS", 0.2)
    clock = FakeClock()
    api = NeverExitsApi(clock=clock)
    with pytest.raises(runner.ColibriStage2Failure, match="timeout"):
        _run(registered, api, clock=clock.time)
    assert "job-1" in api.terminated
    assert "wait_process" in api.calls


def test_cleanup_failure_from_failed_wait_process_overrides_success(registered: _Fixture) -> None:
    api = FakeApi(fail_wait_process=True)
    with pytest.raises(runner.ColibriStage2Failure, match="cleanup_failed"):
        _run(registered, api)


def test_orphan_descendant_is_detected_and_fails_closed(registered: _Fixture) -> None:
    api = FakeApi(descendants={12345})
    with pytest.raises(runner.ColibriStage2Failure, match="cleanup_failed"):
        _run(registered, api)
    assert "descendant_process_ids" in api.calls


def test_job_assignment_failure_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(job_assignment_ok=False)
    with pytest.raises(runner.ColibriStage2Failure, match="job_assignment_failed"):
        _run(registered, api)


def test_process_image_mismatch_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(image_matches=False)
    with pytest.raises(runner.ColibriStage2Failure, match="runtime_identity_mismatch"):
        _run(registered, api)


def test_underlying_isolated_server_failure_is_translated(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RaisingApi(FakeApi):
        def create_job(self) -> str:
            raise IsolatedServerFailure("job_create_failed")

    api = RaisingApi()
    with pytest.raises(runner.ColibriStage2Failure, match="job_create_failed"):
        _run(registered, api)


def test_isolated_server_failure_mapping_is_not_indiscriminate(registered: _Fixture) -> None:
    # Different underlying PR #40 categories must map to different, truthful
    # Stage 2 categories -- never one blanket category regardless of cause.
    class ResumeFailsApi(FakeApi):
        def resume_process(self, process: Any) -> None:
            raise IsolatedServerFailure("process_resume_failed")

    class AssignmentFailsApi(FakeApi):
        def assign_process(self, job: str, process: Any) -> None:
            raise IsolatedServerFailure("job_assignment_failed")

    with pytest.raises(runner.ColibriStage2Failure, match="process_resume_failed"):
        _run(registered, ResumeFailsApi())
    with pytest.raises(runner.ColibriStage2Failure, match="job_assignment_failed"):
        _run(registered, AssignmentFailsApi())


def test_unmapped_isolated_server_failure_falls_back_to_process_create_failed(
    registered: _Fixture,
) -> None:
    class UnmappedFailsApi(FakeApi):
        def create_suspended(self, executable: Path, arguments: tuple, environment) -> CreatedProcess:
            raise IsolatedServerFailure("port_bind_failed")

    with pytest.raises(runner.ColibriStage2Failure, match="process_create_failed"):
        _run(registered, UnmappedFailsApi())


def test_successful_run_removes_temporary_reference(registered: _Fixture) -> None:
    api = FakeApi()
    session_parent = registered.root / "sessions"
    session_parent.mkdir()
    result = _run(registered, api, reference_session_parent=session_parent)
    assert result.reference_removed is True
    # No stray session directories should remain.
    assert list(session_parent.iterdir()) == []


# ---------------------------------------------------------------------------
# No raw evidence ever escapes
# ---------------------------------------------------------------------------


def test_result_never_exposes_paths_or_raw_output(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"Matching tokens: 1/1\n")
    result = _run(registered, api)
    serialized = repr(result)
    assert str(registered.root) not in serialized
    assert "Matching tokens" not in serialized
    assert "fake engine bytes" not in serialized


def test_failure_metadata_never_carries_strings() -> None:
    with pytest.raises(ValueError):
        runner.ColibriStage2Failure("nonzero_exit", exit_code="1")  # type: ignore[arg-type]


def test_environment_never_inherits_arbitrary_user_variables(registered: _Fixture) -> None:
    api = FakeApi()
    _run(registered, api)
    _, _, environment = api.create_suspended_calls[0]
    assert "OLLAMA_API_KEY" not in environment
    assert "HTTP_PROXY" not in environment
    assert "PATH" not in environment
    assert "USERPROFILE" not in environment


# ---------------------------------------------------------------------------
# Directory-chain path safety (Blocker 3)
# ---------------------------------------------------------------------------


def test_olmoe_exe_directory_reparse_point_is_rejected(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_lstat = runner.os.lstat
    reparse_target = registered.exe.parent

    class _FakeStatResult:
        def __init__(self, real_result: Any) -> None:
            self.st_mode = real_result.st_mode
            self.st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

    def _fake_lstat(path: Any, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == reparse_target:
            return _FakeStatResult(result)
        return result

    monkeypatch.setattr(runner.os, "lstat", _fake_lstat)
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="reparse_point_rejected"):
        _run(registered, api)
    assert api.calls == []


def test_converted_model_dir_reparse_point_is_rejected(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_lstat = runner.os.lstat
    reparse_target = registered.model_dir

    class _FakeStatResult:
        def __init__(self, real_result: Any) -> None:
            self.st_mode = real_result.st_mode
            self.st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

    def _fake_lstat(path: Any, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == reparse_target:
            return _FakeStatResult(result)
        return result

    monkeypatch.setattr(runner.os, "lstat", _fake_lstat)
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="reparse_point_rejected"):
        _run(registered, api)
    assert api.calls == []


def test_missing_converted_model_dir_is_rejected_before_process_creation(
    registered: _Fixture,
) -> None:
    import shutil

    shutil.rmtree(registered.model_dir)
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="missing_converted_shard"):
        _run(registered, api)
    assert api.calls == []


# ---------------------------------------------------------------------------
# Private session TEMP/TMP isolation (runner correction)
# ---------------------------------------------------------------------------


def test_environment_temp_and_tmp_point_to_private_session(registered: _Fixture) -> None:
    api = FakeApi()
    _run(registered, api)
    _, _, environment = api.create_suspended_calls[0]
    assert environment["TEMP"] == environment["TMP"]
    session_path = Path(environment["TEMP"])
    assert session_path.parent == registered.root
    assert session_path.name.startswith("odysseus-colibri-stage2-ref-")


# ---------------------------------------------------------------------------
# Resource probe ordering (runner correction)
# ---------------------------------------------------------------------------


def test_resource_probe_runs_before_handles_are_closed(registered: _Fixture) -> None:
    api = FakeApi()
    closed_handle_counts_at_probe_time: list[int] = []

    def probe(job: Any, process: CreatedProcess) -> runner.ResourceEvidence:
        closed_handle_counts_at_probe_time.append(len(api.closed_handles))
        return runner._UNAVAILABLE_RESOURCE_EVIDENCE

    result = _run(registered, api, resource_probe=probe)
    assert result.ok is True
    assert closed_handle_counts_at_probe_time == [0]
    assert len(api.closed_handles) > 0


def test_resource_probe_is_not_invoked_when_absent(registered: _Fixture) -> None:
    api = FakeApi()
    result = _run(registered, api)
    assert result.resources.cpu_time_state == "unavailable"
    assert result.resources.process_memory_state == "unavailable"
    assert result.resources.disk_read_state == "unavailable"


# ---------------------------------------------------------------------------
# Evidence hash binding (runner correction)
# ---------------------------------------------------------------------------


def test_evidence_hash_changes_if_engine_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = tmp_path / "engine-a"
    root_a.mkdir()
    root_b = tmp_path / "engine-b"
    root_b.mkdir()
    fixture_a = _Fixture(root_a)
    fixture_b = _Fixture(root_b)
    fixture_b.exe.write_bytes(b"a completely different fake engine")
    fixture_b.manifest = _make_manifest(
        engine_bytes=fixture_b.exe.read_bytes(),
        config_bytes=fixture_b.config.read_bytes(),
        shard_bytes=fixture_b.shard_bytes,
    )

    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: fixture_a.manifest}),
    )
    result_a = _run(fixture_a, FakeApi())

    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: fixture_b.manifest}),
    )
    result_b = _run(fixture_b, FakeApi())

    assert result_a.evidence_sha256 is not None
    assert result_b.evidence_sha256 is not None
    assert result_a.evidence_sha256 != result_b.evidence_sha256


def test_evidence_hash_changes_if_shard_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = tmp_path / "shard-a"
    root_a.mkdir()
    root_b = tmp_path / "shard-b"
    root_b.mkdir()
    fixture_a = _Fixture(root_a)
    fixture_b = _Fixture(root_b)
    tampered_shard_bytes = (b"different-shard-0", fixture_b.shard_bytes[1], fixture_b.shard_bytes[2])
    for name, data in zip(common.EXPECTED_SHARD_BASENAMES, tampered_shard_bytes):
        (fixture_b.model_dir / name).write_bytes(data)
    fixture_b.manifest = _make_manifest(
        engine_bytes=fixture_b.exe.read_bytes(),
        config_bytes=fixture_b.config.read_bytes(),
        shard_bytes=tampered_shard_bytes,
    )

    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: fixture_a.manifest}),
    )
    result_a = _run(fixture_a, FakeApi())

    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: fixture_b.manifest}),
    )
    result_b = _run(fixture_b, FakeApi())

    assert result_a.evidence_sha256 != result_b.evidence_sha256


# ---------------------------------------------------------------------------
# Native build verifier script contract (Blocker 1) -- structural
# assertions only; the real MSYS2/gcc/make toolchain is never invoked.
# ---------------------------------------------------------------------------


_NATIVE_VERIFIER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-colibri-native-repro.ps1"


def _native_verifier_text() -> str:
    return _NATIVE_VERIFIER_SCRIPT.read_text(encoding="utf-8")


def test_native_verifier_script_exists() -> None:
    assert _NATIVE_VERIFIER_SCRIPT.is_file()


def test_native_verifier_never_mutates_parent_environment() -> None:
    text = _native_verifier_text()
    assert "$env:SOURCE_DATE_EPOCH" not in text
    assert "Env:SOURCE_DATE_EPOCH" not in text
    assert "EnvironmentVariables['SOURCE_DATE_EPOCH']" in text


def test_native_verifier_redirects_and_bounds_build_output() -> None:
    text = _native_verifier_text()
    assert "$startInfo.RedirectStandardOutput = $true" in text
    assert "$startInfo.RedirectStandardError = $true" in text
    assert "MaxBuildStreamBytes" in text


def test_native_verifier_kills_full_process_tree_on_timeout_or_overflow() -> None:
    text = _native_verifier_text()
    assert "function Stop-ProcessTree" in text
    assert "Kill', [Type[]]@([bool])" in text
    assert "taskkill" in text.lower()
    assert "function Get-DescendantProcessIds" in text


def test_native_verifier_waits_bounded_after_termination_and_fails_if_not_confirmed() -> None:
    text = _native_verifier_text()
    assert "$BuildTerminationWaitMilliseconds" in text
    assert "native build launcher did not exit after termination" in text
    assert "native build left a compiler descendant running" in text


def test_native_verifier_final_output_is_json_only() -> None:
    text = _native_verifier_text()
    stripped = text.rstrip()
    assert stripped.endswith("ConvertTo-Json -Depth 8")
    assert "Format-Table" not in text
    assert "Write-Host" not in text
    assert "Out-Default" not in text


def test_native_verifier_json_object_carries_no_paths() -> None:
    text = _native_verifier_text()
    result_block = text[text.index("$result = [pscustomobject]@{") :]
    forbidden = (
        "$target",
        "$cRoot",
        "$resolvedSource",
        "$resolvedBuildRoot",
        "$launcher",
        "$gcc",
        "$make",
        "SourceRoot",
        "BuildRoot",
        "WorkingDirectory",
    )
    for token in forbidden:
        assert token not in result_block


def test_native_verifier_preserves_two_build_independence_and_olmoe_double_build() -> None:
    text = _native_verifier_text()
    assert "clean build target already exists" in text
    assert "make olmoe.exe ARCH=x86-64-v3" in text
    assert "DeterministicallyEqual" in text
    assert text.count("Invoke-BoundedBuild -Launcher") == 1  # single call site, looped for build-a/build-b


def test_native_verifier_preserves_oracle_proof_semantics() -> None:
    text = _native_verifier_text()
    assert "function Invoke-BoundedOracle" in text
    assert "idot kernel exactness (avx2): ok" in text
