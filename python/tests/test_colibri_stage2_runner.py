"""Tests for the Colibrì Stage 2A real one-token runner (Part 5).

Every test here uses a synthetic fake ``LifecycleApi`` and a fake clock.
No test launches a real process, touches the network, or reads the
ordinary Ollama/Colibrì model store.
"""

from __future__ import annotations

import dataclasses
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


# The pinned engine's real one-token output dialect, transcribed from
# `c/olmoe.c` at commit 72d3d372 -- the streaming-engine banner, the complete
# resident-weights/RSS line, the Reference/C-engine pair (each token emitted
# as `printf("%d ")`, hence the trailing space), Matching tokens, PEAK RSS,
# the cache-hit line, and Speed.
def engine_output(
    *,
    reference_ids: str = "7785 ",
    generated_ids: str = "7785 ",
    matched: str = "1",
    expected: str = "1",
    model_load_seconds: str = "12.5",
    rss_after_load_gb: str = "6.42",
    peak_rss_gb: str = "6.51",
    rate: str = "1.85",
    generation_seconds: str = "0.5",
    generated_count: str = "1",
    banner: bool = True,
    model_load_line: str | None = None,
) -> bytes:
    lines: list[str] = []
    if banner:
        lines.append("== Streaming C engine, cache = 8 experts/layer, experts @ 8-bit ==")
    if model_load_line is None:
        model_load_line = (
            f"resident weights loaded in {model_load_seconds}s"
            f" | RSS after load: {rss_after_load_gb} GB"
        )
    lines.append(model_load_line)
    lines.append("")
    lines.append(f"Reference: {reference_ids}")
    lines.append(f"C engine : {generated_ids}")
    lines.append(f"Matching tokens: {matched}/{expected}")
    lines.append("")
    lines.append(f"PEAK RSS: {peak_rss_gb} GB")
    lines.append("Expert cache hit rate: 92.3%  (hit=1187 miss=98)")
    lines.append(f"Speed: {rate} tok/s ({generation_seconds}s for {generated_count} tokens)")
    return ("\n".join(lines) + "\n").encode("utf-8")


GOOD_OUTPUT = engine_output()


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
        stdout: bytes | None = None,
        stderr: bytes = b"",
        exit_code: int | None = 0,
        descendants: set[int] | None = None,
        clock: FakeClock | None = None,
        fail_wait_process: bool = False,
        image_matches: bool = True,
        job_assignment_ok: bool = True,
    ) -> None:
        if stdout is None:
            stdout = GOOD_OUTPUT
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
    bounded = common.REVIEWED_BOUNDED_CONVERTER_IDENTITY
    return manifest_mod.OlmoeModelManifest(
        model_repository=common.PINNED_MODEL_REPOSITORY,
        model_revision=common.PINNED_MODEL_REVISION,
        license_identifier=common.PINNED_LICENSE_IDENTIFIER,
        colibri_commit=common.PINNED_COLIBRI_COMMIT,
        converter_kind=common.CONVERTER_KIND_BOUNDED,
        converter_basename=bounded.basename,
        converter_size_bytes=bounded.size_bytes,
        converter_source_sha256=bounded.sha256,
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
        cap_argument=common.CAP_ARGUMENT,
        bits_argument=common.BITS_ARGUMENT,
        prompt_token_ids=common.PROMPT_TOKEN_IDS,
        expected_generated_token_id=common.EXPECTED_GENERATED_TOKEN_ID,
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
        # Assigned by `_build_fixture`, which first patches
        # common.REVIEWED_ENGINE_IDENTITY to match this fixture's own
        # synthetic engine bytes -- OlmoeModelManifest now requires an
        # exact match against the reviewed engine identity, so a fixture
        # using arbitrary fake engine content can only construct a valid
        # manifest once that identity is patched to agree with it.
        self.manifest: manifest_mod.OlmoeModelManifest


def _patch_reviewed_engine_identity(monkeypatch: pytest.MonkeyPatch, engine_bytes: bytes) -> None:
    monkeypatch.setattr(
        common,
        "REVIEWED_ENGINE_IDENTITY",
        common.ReviewedEngineIdentity(
            colibri_commit=common.PINNED_COLIBRI_COMMIT,
            basename=common.EXPECTED_ENGINE_BASENAME,
            size_bytes=len(engine_bytes),
            sha256=_sha256_bytes(engine_bytes),
            source_date_epoch=common.REVIEWED_ENGINE_IDENTITY.source_date_epoch,
            deterministic_build_count=2,
        ),
    )


def _build_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    result = _Fixture(tmp_path)
    _patch_reviewed_engine_identity(monkeypatch, result.exe.read_bytes())
    result.manifest = _make_manifest(
        engine_bytes=result.exe.read_bytes(),
        config_bytes=result.config.read_bytes(),
        shard_bytes=result.shard_bytes,
    )
    return result


@pytest.fixture()
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    return _build_fixture(tmp_path, monkeypatch)


@pytest.fixture()
def registered(fixture: _Fixture, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: fixture.manifest}),
    )
    return fixture


def _run_kwargs(fixture: _Fixture, api: FakeApi, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        olmoe_exe=fixture.exe,
        converted_model_dir=fixture.model_dir,
        api=api,
        approved=True,
        interactive_check=lambda: True,
        reference_session_parent=fixture.root,
        # The real probe cannot read a synthetic job handle, so tests state
        # explicitly what the Job Object reports. Zero members is the normal
        # case; tests that care about an unavailable or non-empty job override
        # this and say so.
        job_member_probe=lambda job: 0,
        # Never a real sleep in tests: advance the fake clock instead, so a
        # bounded poll loop terminates without wall-clock delay.
        sleep=(lambda seconds: api.clock.advance(seconds)) if api.clock is not None else (lambda seconds: None),
    )
    kwargs.update(overrides)
    if "clock" not in kwargs and api.clock is not None:
        kwargs["clock"] = api.clock.time
    return kwargs


def _run(fixture: _Fixture, api: FakeApi, **overrides: Any) -> runner.OneTokenRunResult:
    return runner.run_one_token_proof(**_run_kwargs(fixture, api, **overrides))


def _attempt(fixture: _Fixture, api: FakeApi, **overrides: Any) -> runner.OneTokenRunResult:
    return runner.attempt_one_token_proof(**_run_kwargs(fixture, api, **overrides))


# ---------------------------------------------------------------------------
# Manifest gate: impossible to launch before process creation
# ---------------------------------------------------------------------------


def test_empty_registry_blocks_before_process_creation(
    fixture: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manifest_mod, "REVIEWED_OLMOE_MODEL_REGISTRY", MappingProxyType({}))
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="reviewed_model_manifest_unavailable"):
        _run(fixture, api)
    assert api.calls == []


def test_shipped_registry_cannot_authorize_a_foreign_engine_or_model(fixture: _Fixture) -> None:
    # With the real reviewed registry in place (no monkeypatching), the
    # synthetic fixture engine and artifact set must be refused: the entry
    # authorizes exactly one engine and one artifact set, and nothing here
    # matches it.
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="runtime_identity_mismatch"):
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


def test_extra_non_shard_file_is_rejected(registered: _Fixture) -> None:
    # The whole directory listing is checked, not only `*.safetensors`.
    (registered.model_dir / "notes.txt").write_bytes(b"hello")
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="unknown_converted_shard"):
        _run(registered, api)
    assert api.calls == []


def test_extra_subdirectory_is_rejected(registered: _Fixture) -> None:
    (registered.model_dir / "leftover").mkdir()
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="unknown_converted_shard"):
        _run(registered, api)
    assert api.calls == []


def test_resume_ledger_is_tolerated_but_never_read(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bounded converter legitimately leaves its resume ledger beside the
    # artifacts. Its presence must not fail the run -- and it must never be
    # opened, so a ledger full of contradictory nonsense changes nothing.
    ledger = registered.model_dir / common.RESUME_LEDGER_BASENAME
    ledger.write_bytes(b"{ this is not even valid json and claims nothing true }")

    opened: list[str] = []
    real_open = Path.open

    def tracking_open(self: Path, *args: Any, **kwargs: Any):
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    result = _run(registered, FakeApi())
    assert result.ok is True
    assert common.RESUME_LEDGER_BASENAME not in opened


def test_shard_basename_order_is_fixed_by_the_manifest(registered: _Fixture) -> None:
    # A reordered shard tuple cannot even be expressed: the manifest pins the
    # three names in order, so no run can be authorized against a permuted
    # artifact set.
    reordered = tuple(reversed(common.EXPECTED_SHARD_BASENAMES))
    with pytest.raises(ValueError, match="shard_basenames"):
        manifest_mod.OlmoeModelManifest(
            **{
                **{
                    field: getattr(registered.manifest, field)
                    for field in registered.manifest.__dataclass_fields__
                },
                "shard_basenames": reordered,
                "conversion_dependency_versions": dict(
                    registered.manifest.conversion_dependency_versions
                ),
            }
        )


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


def test_build_token_command_grammar_is_exactly_exe_cap_bits_ref(registered: _Fixture) -> None:
    reference = registered.root / common.EXPECTED_REF_BASENAME
    executable, arguments = runner.build_token_command(
        registered.manifest, registered.exe, reference
    )
    assert executable == registered.exe
    assert arguments == ("8", "8", str(reference))
    assert len(arguments) == 3


def test_build_token_command_takes_cap_and_bits_from_the_manifest(registered: _Fixture) -> None:
    # Not from a module constant and not from a caller: the reviewed entry is
    # the only source, so the command can never be built with another cap or
    # quantization width.
    assert registered.manifest.cap_argument == "8"
    assert registered.manifest.bits_argument == "8"
    _, arguments = runner.build_token_command(
        registered.manifest, registered.exe, registered.root / common.EXPECTED_REF_BASENAME
    )
    assert arguments[:2] == (registered.manifest.cap_argument, registered.manifest.bits_argument)


def test_build_token_command_rejects_a_foreign_reference_basename(registered: _Fixture) -> None:
    with pytest.raises(runner.ColibriStage2Failure, match="reference_hash_mismatch"):
        runner.build_token_command(registered.manifest, registered.exe, registered.root / "other.json")


def test_build_token_command_rejects_a_foreign_executable_basename(registered: _Fixture) -> None:
    with pytest.raises(runner.ColibriStage2Failure, match="executable_not_found"):
        runner.build_token_command(
            registered.manifest, registered.root / "glm.exe", registered.root / common.EXPECTED_REF_BASENAME
        )


def test_command_line_carries_no_prompt_model_path_or_tokenizer(registered: _Fixture) -> None:
    api = FakeApi()
    _run(registered, api)
    _, arguments, _ = api.create_suspended_calls[0]
    joined = " ".join(arguments)
    assert "prompt" not in joined.lower()
    assert str(registered.model_dir) not in joined
    assert "tokenizer" not in joined.lower()
    assert ".safetensors" not in joined


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
    api = FakeApi(stdout=GOOD_OUTPUT)
    result = _run(registered, api)
    assert result.ok is True
    assert result.category == "passed"
    assert result.matched_count == 1
    assert result.contract_expected_count == 1
    assert result.engine_reported_expected_count == 1
    assert result.expected_token_id == 7785
    assert result.generated_token_id == 7785
    assert result.exit_code == 0
    assert result.exit_category == "clean_exit"
    assert result.cleanup_complete is True
    assert result.orphan_free is True
    assert result.reference_removed is True
    assert result.vram_state == "not_applicable"
    assert result.evidence_sha256 is not None
    assert result.evidence_schema_version == "colibri-stage2-olmoe-token-evidence-v2"
    assert result.job_empty_proven is True
    assert result.job_member_count == 0
    assert result.root_exit_confirmed is True


def test_wrong_generated_token_is_rejected_even_when_engine_claims_a_match(
    registered: _Fixture,
) -> None:
    # The engine asserts 1/1 while its own C-engine line shows a different
    # token. The independent comparison must reject it -- this is precisely
    # the case a match-line-only oracle would have passed.
    api = FakeApi(stdout=engine_output(generated_ids="7786", matched="1", expected="1"))
    with pytest.raises(runner.ColibriStage2Failure, match="token_identity_mismatch"):
        _run(registered, api)


def test_zero_of_one_with_a_differing_token_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=engine_output(generated_ids="42", matched="0", expected="1"))
    with pytest.raises(runner.ColibriStage2Failure, match="token_identity_mismatch"):
        _run(registered, api)


def test_engine_contradicting_its_own_token_lines_is_rejected(registered: _Fixture) -> None:
    # Identical reference and generated tokens, but the engine reports 0/1.
    # Its own lines contradict its own count.
    api = FakeApi(stdout=engine_output(matched="0", expected="1"))
    with pytest.raises(runner.ColibriStage2Failure, match="output_internally_inconsistent"):
        _run(registered, api)


def test_unexpected_denominator_is_rejected(registered: _Fixture) -> None:
    # One reference token but an expected count of 2: internally inconsistent.
    api = FakeApi(stdout=engine_output(matched="1", expected="2"))
    with pytest.raises(runner.ColibriStage2Failure, match="output_internally_inconsistent"):
        _run(registered, api)


def test_wrong_reference_line_is_rejected(registered: _Fixture) -> None:
    # The engine compared against a token that is not the reviewed one, so its
    # match count is meaningless whatever it says.
    api = FakeApi(stdout=engine_output(reference_ids="1234", generated_ids="1234"))
    with pytest.raises(runner.ColibriStage2Failure, match="reference_line_mismatch"):
        _run(registered, api)


def test_extra_generated_tokens_are_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=engine_output(generated_ids="7785 7785", matched="1", expected="1"))
    with pytest.raises(runner.ColibriStage2Failure, match="generated_token_count_unexpected"):
        _run(registered, api)


def test_missing_generated_token_line_is_rejected(registered: _Fixture) -> None:
    without_c_engine = b"\n".join(
        line for line in GOOD_OUTPUT.split(b"\n") if not line.startswith(b"C engine")
    )
    api = FakeApi(stdout=without_c_engine)
    with pytest.raises(runner.ColibriStage2Failure, match="malformed_output"):
        _run(registered, api)


def test_missing_reference_line_is_rejected(registered: _Fixture) -> None:
    without_reference = b"\n".join(
        line for line in GOOD_OUTPUT.split(b"\n") if not line.startswith(b"Reference:")
    )
    api = FakeApi(stdout=without_reference)
    with pytest.raises(runner.ColibriStage2Failure, match="malformed_output"):
        _run(registered, api)


def test_duplicate_matching_lines_are_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=GOOD_OUTPUT + b"Matching tokens: 1/1\n")
    with pytest.raises(runner.ColibriStage2Failure, match="duplicate_match_line"):
        _run(registered, api)


def test_duplicate_reference_line_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=GOOD_OUTPUT + b"Reference: 7785\n")
    with pytest.raises(runner.ColibriStage2Failure, match="duplicate_output_line"):
        _run(registered, api)


def test_duplicate_generated_token_line_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=GOOD_OUTPUT + b"C engine : 7785\n")
    with pytest.raises(runner.ColibriStage2Failure, match="duplicate_output_line"):
        _run(registered, api)


def test_malformed_output_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"nothing useful here\n")
    with pytest.raises(runner.ColibriStage2Failure, match="malformed_output"):
        _run(registered, api)


def test_undecodable_output_is_rejected(registered: _Fixture) -> None:
    api = FakeApi(stdout=b"\xff\xfe not utf-8 at all\n")
    with pytest.raises(runner.ColibriStage2Failure, match="output_decode_failed"):
        _run(registered, api)


def test_unexpected_extra_output_around_the_dialect_still_parses(registered: _Fixture) -> None:
    api = FakeApi(
        stdout=b"warming up\n" + GOOD_OUTPUT + b"done\nsome trailing banner\n"
    )
    result = _run(registered, api)
    assert result.ok is True
    assert result.generated_token_id == 7785


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
    # A surviving descendant is reported as its own fact, not folded into
    # generic cleanup uncertainty.
    api = FakeApi(descendants={12345})
    with pytest.raises(runner.ColibriStage2Failure, match="orphan_detected"):
        _run(registered, api)
    assert "descendant_process_ids" in api.calls


def test_inconclusive_orphan_probe_fails_closed_as_cleanup_uncertainty(
    registered: _Fixture,
) -> None:
    class ProbeFailsApi(FakeApi):
        def descendant_process_ids(self, process_id: int) -> set[int]:
            self.calls.append("descendant_process_ids")
            # The exact category the real WindowsLifecycleApi raises when the
            # process snapshot cannot be taken.
            raise IsolatedServerFailure("ownership_probe_unavailable")

    api = ProbeFailsApi()
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
    api = FakeApi(stdout=GOOD_OUTPUT)
    result = _run(registered, api)
    serialized = repr(result)
    assert str(registered.root) not in serialized
    assert "Matching tokens" not in serialized
    assert "C engine" not in serialized
    assert "resident weights" not in serialized
    assert "fake engine bytes" not in serialized


def test_result_evidence_is_closed_and_privacy_safe(registered: _Fixture) -> None:
    import getpass
    import os as os_module

    api = FakeApi(stdout=b"warming up with a chatty banner\n" + GOOD_OUTPUT + b"done\n")
    result = _run(registered, api)
    serialized = repr(result)

    # No captured stream content, no path, no environment value, no username.
    for forbidden in (
        "Matching tokens",
        "warming up",
        "chatty banner",
        str(registered.root),
        str(registered.model_dir),
        str(registered.exe),
        common.EXPECTED_REF_BASENAME,
        "OMP_NUM_THREADS",
        "SNAP",
    ):
        assert forbidden not in serialized, forbidden
    try:
        username = getpass.getuser()
    except Exception:  # noqa: BLE001 - not every environment exposes a username
        username = ""
    if username:
        assert username not in serialized
    assert os_module.fspath(registered.root) not in serialized

    # The prompt itself is never carried: no field of the result (or of its
    # nested identity record) holds the prompt token sequence. A substring
    # scan would be meaningless here -- 64-character digests contain short
    # digit runs by chance -- so this is a structural check on field values.
    import dataclasses

    def field_values(record: Any) -> list[Any]:
        values: list[Any] = []
        for field in dataclasses.fields(record):
            value = getattr(record, field.name)
            values.append(value)
            if dataclasses.is_dataclass(value):
                values.extend(field_values(value))
        return values

    values = field_values(result)
    assert common.PROMPT_TOKEN_IDS not in values
    assert list(common.PROMPT_TOKEN_IDS) not in values
    assert common.FULL_TOKEN_IDS not in values


def test_identity_evidence_records_every_pinned_identity(registered: _Fixture) -> None:
    result = _run(registered, FakeApi())
    identities = result.identities
    manifest = registered.manifest
    assert identities.model_repository == manifest.model_repository
    assert identities.model_revision == manifest.model_revision
    assert identities.colibri_commit == manifest.colibri_commit
    assert identities.engine_sha256 == manifest.engine_sha256
    assert identities.converter_kind == "bounded"
    assert identities.converter_sha256 == manifest.converter_source_sha256
    assert identities.config_sha256 == manifest.config_sha256
    assert identities.shard_sha256 == tuple(manifest.shard_sha256)
    assert identities.reference_sha256 == manifest.ref_sha256
    assert identities.cap_argument == "8"
    assert identities.bits_argument == "8"


def test_identity_evidence_is_frozen(registered: _Fixture) -> None:
    import dataclasses

    result = _run(registered, FakeApi())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.identities.engine_sha256 = "x" * 64  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Token oracle is closed and the reference is bound to it
# ---------------------------------------------------------------------------


def test_reference_contract_mismatch_blocks_before_process_creation(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the in-process reference derivation ever stopped agreeing with the
    # reviewed registry entry's token contract, the run must be abandoned
    # before a process exists -- not "adapted" to whichever side changed.
    monkeypatch.setattr(
        runner, "reference_object", lambda: {"prompt_ids": [1, 2, 3, 4, 5], "full_ids": [1, 2, 3, 4, 5, 6]}
    )
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="token_identity_mismatch"):
        _run(registered, api)
    assert api.calls == []


def test_expected_token_mismatch_between_reference_and_manifest_is_rejected(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "reference_object",
        lambda: {
            "prompt_ids": list(common.PROMPT_TOKEN_IDS),
            "full_ids": list(common.PROMPT_TOKEN_IDS) + [7786],
        },
    )
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="token_identity_mismatch"):
        _run(registered, api)
    assert api.calls == []


def test_reference_digest_drift_is_rejected(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "canonical_reference_sha256", lambda: "a" * 64)
    api = FakeApi()
    with pytest.raises(runner.ColibriStage2Failure, match="reference_hash_mismatch"):
        _run(registered, api)
    assert api.calls == []


def test_wrong_generated_token_is_retained_as_observed_evidence(
    registered: _Fixture,
) -> None:
    # The observation is the point of having run it: a wrong token must be
    # reported as the actual integer the engine produced, never nulled out and
    # never replaced by the expected value.
    failed = _attempt(
        registered, FakeApi(stdout=engine_output(generated_ids="4242 ", matched="0"))
    )
    assert failed.ok is False
    assert failed.category == "token_identity_mismatch"
    assert failed.generated_token_id == 4242
    assert failed.expected_token_id == 7785
    assert failed.evidence_sha256 is None


def test_generated_token_id_is_never_taken_from_the_expected_value(
    registered: _Fixture,
) -> None:
    result = _run(registered, FakeApi(stdout=GOOD_OUTPUT))
    # Equal here, but read from the engine's own line rather than copied.
    assert result.generated_token_id == result.expected_token_id == 7785


def test_failed_run_retains_engine_reported_counts(registered: _Fixture) -> None:
    # An internally inconsistent run still reports what the engine said, with
    # the contract's own expected count kept separate from the engine's.
    failed = _attempt(registered, FakeApi(stdout=engine_output(matched="1", expected="3")))
    assert failed.ok is False
    assert failed.category == "output_internally_inconsistent"
    assert failed.matched_count == 1
    assert failed.engine_reported_expected_count == 3
    assert failed.contract_expected_count == 1
    assert failed.generated_token_id == 7785
    assert failed.evidence_sha256 is None


def test_no_generated_token_is_reported_when_none_was_safely_parsed(
    registered: _Fixture,
) -> None:
    # Two generated tokens: there is no single integer to report, so the field
    # stays null rather than guessing which one to keep.
    failed = _attempt(registered, FakeApi(stdout=engine_output(generated_ids="7785 42 ")))
    assert failed.ok is False
    assert failed.category == "generated_token_count_unexpected"
    assert failed.generated_token_id is None


def test_evidence_digest_depends_on_the_parsed_generated_token(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two runs identical in every reviewed identity but differing in the token
    # the engine actually produced must not share an evidence digest. Since a
    # wrong token is rejected outright, this is asserted on the digest helper
    # with the real manifest.
    manifest = registered.manifest
    first = runner._evidence_digest(manifest, 7785, 1)
    second = runner._evidence_digest(manifest, 7786, 1)
    assert first != second
    result = _run(registered, FakeApi(stdout=GOOD_OUTPUT))
    assert result.evidence_sha256 == first


# ---------------------------------------------------------------------------
# Exit category, latency, and whole-tree memory evidence
# ---------------------------------------------------------------------------


def test_classify_process_exit_covers_the_closed_vocabulary() -> None:
    assert runner.classify_process_exit(exit_code=0, timed_out=False) == "clean_exit"
    assert runner.classify_process_exit(exit_code=3, timed_out=False) == "nonzero_exit"
    assert runner.classify_process_exit(exit_code=None, timed_out=False) == "not_observed"
    assert runner.classify_process_exit(exit_code=None, timed_out=True) == "timed_out"
    assert runner.classify_process_exit(exit_code=0, timed_out=True) == "timed_out"
    for category in ("clean_exit", "nonzero_exit", "not_observed", "timed_out"):
        assert category in common.EXIT_CATEGORIES


def test_engine_reported_latencies_come_from_the_engine_output(registered: _Fixture) -> None:
    api = FakeApi(stdout=engine_output(model_load_seconds="12.500", generation_seconds="0.540"))
    result = _run(registered, api)
    latency = result.latency
    # Exactly the values the engine printed, in milliseconds -- not a
    # wall-clock approximation of them.
    assert latency.model_load_latency_state == "measured"
    assert latency.model_load_latency_ms == 12500
    assert latency.generation_latency_state == "measured"
    assert latency.generation_latency_ms == 540


def test_independently_measured_latencies_are_recorded_separately(registered: _Fixture) -> None:
    result = _run(registered, FakeApi())
    latency = result.latency
    assert latency.end_to_end_latency_state == "measured"
    assert isinstance(latency.end_to_end_latency_ms, int) and latency.end_to_end_latency_ms >= 0
    assert latency.first_output_latency_state == "measured"
    assert isinstance(latency.first_output_latency_ms, int) and latency.first_output_latency_ms >= 0


def test_first_output_latency_is_not_presented_as_model_load(registered: _Fixture) -> None:
    # The pinned engine prints its banner before model_init, so first-output
    # must never stand in for model load. The two are independent fields, and
    # the engine-reported load time is not derived from the clock at all.
    api = FakeApi(stdout=engine_output(model_load_seconds="99.000"))
    result = _run(registered, api)
    assert result.latency.model_load_latency_ms == 99000
    assert result.latency.first_output_latency_ms != result.latency.model_load_latency_ms
    field_names = {field.name for field in dataclasses.fields(result.latency)}
    assert "startup_latency_ms" not in field_names
    assert "one_token_latency_ms" not in field_names


def test_latency_evidence_reports_unavailable_rather_than_zero() -> None:
    # A missing endpoint yields None plus an explicit state, so an unmeasured
    # latency can never be misread as "0 ms".
    evidence = runner._latency_evidence(
        resumed_at=None, first_output_at=None, exit_observed_at=None, parsed=None
    )
    assert evidence.end_to_end_latency_ms is None
    assert evidence.end_to_end_latency_state == "unavailable"
    assert evidence.first_output_latency_ms is None
    assert evidence.first_output_latency_state == "unavailable"
    assert evidence.model_load_latency_ms is None
    assert evidence.model_load_latency_state == "unavailable"
    assert evidence.generation_latency_ms is None
    assert evidence.generation_latency_state == "unavailable"

    partial = runner._latency_evidence(
        resumed_at=10.0, first_output_at=None, exit_observed_at=10.5, parsed=None
    )
    assert partial.first_output_latency_state == "unavailable"
    assert partial.first_output_latency_ms is None
    assert partial.end_to_end_latency_state == "measured"
    assert partial.end_to_end_latency_ms == 500


def test_engine_latencies_are_unavailable_when_output_was_never_parsed(
    registered: _Fixture,
) -> None:
    # A run that failed before parsing must not report engine timings at all.
    result = _attempt(registered, FakeApi(stdout=b"nothing useful here\n"))
    assert result.ok is False
    assert result.latency.model_load_latency_state == "unavailable"
    assert result.latency.model_load_latency_ms is None
    assert result.latency.generation_latency_state == "unavailable"
    assert result.latency.generation_latency_ms is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_load_seconds": "-1.0"},
        {"model_load_seconds": "nan"},
        {"model_load_seconds": "inf"},
        {"model_load_seconds": "999999999"},
        {"generation_seconds": "nan"},
        {"generation_seconds": "-0.5"},
        {"rate": "nan"},
        {"rate": "-3"},
        {"generated_count": "2"},
    ],
)
def test_out_of_bounds_engine_timings_are_rejected(
    registered: _Fixture, overrides: dict[str, str]
) -> None:
    api = FakeApi(stdout=engine_output(**overrides))
    with pytest.raises(runner.ColibriStage2Failure, match="timing_evidence_invalid"):
        _run(registered, api)


def test_missing_or_duplicated_timing_lines_are_rejected(registered: _Fixture) -> None:
    without_load = b"\n".join(
        line for line in GOOD_OUTPUT.split(b"\n") if not line.startswith(b"resident weights")
    )
    with pytest.raises(runner.ColibriStage2Failure, match="timing_evidence_invalid"):
        _run(registered, FakeApi(stdout=without_load))

    without_speed = b"\n".join(
        line for line in GOOD_OUTPUT.split(b"\n") if not line.startswith(b"Speed:")
    )
    with pytest.raises(runner.ColibriStage2Failure, match="timing_evidence_invalid"):
        _run(registered, FakeApi(stdout=without_speed))

    duplicate_load = b"resident weights loaded in 1.0s | RSS after load: 1.00 GB\n"
    with pytest.raises(runner.ColibriStage2Failure, match="timing_evidence_invalid"):
        _run(registered, FakeApi(stdout=GOOD_OUTPUT + duplicate_load))


def test_shortened_model_load_line_is_rejected_end_to_end(registered: _Fixture) -> None:
    # Regression guard at the runner level: the shortened line the tests used
    # before this correction is not what the engine prints, and must fail.
    shortened = engine_output(model_load_line="resident weights loaded in 12.5s")
    with pytest.raises(runner.ColibriStage2Failure, match="timing_evidence_invalid"):
        _run(registered, FakeApi(stdout=shortened))


def test_peak_tree_memory_is_recorded_when_positively_measured(registered: _Fixture) -> None:
    probe_calls: list[Any] = []

    def probe(job: Any) -> tuple[int | None, str]:
        probe_calls.append(job)
        return 205_520_896, "measured"

    result = _run(registered, FakeApi(), tree_memory_probe=probe)
    assert probe_calls == ["job-1"]
    assert result.peak_tree_memory_bytes == 205_520_896
    assert result.peak_tree_memory_state == "measured"


def test_peak_tree_memory_is_unavailable_rather_than_zero(registered: _Fixture) -> None:
    result = _run(registered, FakeApi(), tree_memory_probe=lambda job: (None, "unavailable"))
    assert result.peak_tree_memory_bytes is None
    assert result.peak_tree_memory_state == "unavailable"


def test_unmeasured_peak_tree_memory_never_keeps_a_stale_number(registered: _Fixture) -> None:
    # A probe that hands back a number while admitting it is not a
    # measurement must not have that number recorded.
    result = _run(registered, FakeApi(), tree_memory_probe=lambda job: (999, "unavailable"))
    assert result.peak_tree_memory_bytes is None
    assert result.peak_tree_memory_state == "unavailable"


def test_raising_tree_memory_probe_never_fails_the_run(registered: _Fixture) -> None:
    def probe(job: Any) -> tuple[int | None, str]:
        raise RuntimeError("probe exploded")

    result = _run(registered, FakeApi(), tree_memory_probe=probe)
    assert result.ok is True
    assert result.peak_tree_memory_state == "unavailable"


def test_tree_memory_probe_runs_before_handles_are_closed(registered: _Fixture) -> None:
    api = FakeApi()
    closed_at_probe_time: list[int] = []

    def probe(job: Any) -> tuple[int | None, str]:
        closed_at_probe_time.append(len(api.closed_handles))
        return 1, "measured"

    _run(registered, api, tree_memory_probe=probe)
    assert closed_at_probe_time == [0]


def test_default_tree_memory_probe_is_unavailable_without_a_job() -> None:
    assert runner.default_tree_memory_probe(None) == (None, "unavailable")


def test_default_tree_memory_probe_never_guesses_for_a_bogus_job() -> None:
    peak, state = runner.default_tree_memory_probe("not-a-handle")
    assert peak is None
    assert state == "unavailable"


# ---------------------------------------------------------------------------
# Timeout owns the whole native tree
# ---------------------------------------------------------------------------


def test_timeout_terminates_the_job_and_proves_no_orphans(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_TOTAL_RUN_DEADLINE_SECONDS", 0.2)
    clock = FakeClock()
    api = NeverExitsApi(clock=clock)
    with pytest.raises(runner.ColibriStage2Failure, match="timeout"):
        _run(registered, api, clock=clock.time)
    # The whole tree is owned: the kill-on-close Job Object is terminated,
    # the child is waited for, and descendants are enumerated.
    assert api.terminated == ["job-1"]
    assert "wait_process" in api.calls
    assert "descendant_process_ids" in api.calls


def test_timeout_with_a_surviving_descendant_reports_the_orphan(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_TOTAL_RUN_DEADLINE_SECONDS", 0.2)
    clock = FakeClock()
    api = NeverExitsApi(clock=clock, descendants={4242})
    with pytest.raises(runner.ColibriStage2Failure, match="orphan_detected"):
        _run(registered, api, clock=clock.time)
    assert api.terminated == ["job-1"]


def test_unavailable_job_member_count_fails_closed(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unobtainable member count is never read as zero: it is cleanup
    # uncertainty, and the run fails closed.
    monkeypatch.setattr(runner, "_CLEANUP_DEADLINE_SECONDS", 0.2)
    clock = FakeClock()
    api = FakeApi(clock=clock)
    with pytest.raises(runner.ColibriStage2Failure, match="cleanup_failed"):
        _run(
            registered,
            api,
            clock=clock.time,
            sleep=lambda seconds: clock.advance(seconds),
            job_member_probe=lambda job: None,
        )


def test_job_that_never_empties_fails_closed(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_CLEANUP_DEADLINE_SECONDS", 0.2)
    clock = FakeClock()
    api = FakeApi(clock=clock)
    with pytest.raises(runner.ColibriStage2Failure, match="cleanup_failed"):
        _run(
            registered,
            api,
            clock=clock.time,
            sleep=lambda seconds: clock.advance(seconds),
            job_member_probe=lambda job: 2,
        )


def test_job_emptying_after_a_few_polls_still_succeeds(registered: _Fixture) -> None:
    # Termination is asynchronous, so the proof polls rather than sampling
    # once. A job that reports members briefly and then empties is a pass.
    clock = FakeClock()
    api = FakeApi(clock=clock)
    counts = iter([3, 1, 0])

    result = _run(
        registered,
        api,
        clock=clock.time,
        sleep=lambda seconds: clock.advance(seconds),
        job_member_probe=lambda job: next(counts, 0),
    )
    assert result.ok is True
    assert result.job_empty_proven is True
    assert result.job_member_count == 0


def test_orphan_free_is_not_asserted_without_the_zero_member_proof(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A descendant snapshot reporting "none" is not, on its own, proof of an
    # empty tree -- it is an absence-of-evidence argument over a parentage link
    # that goes stale once the root exits. orphan_free must stay False when the
    # Job Object never positively reported zero members.
    monkeypatch.setattr(runner, "_CLEANUP_DEADLINE_SECONDS", 0.2)
    clock = FakeClock()
    api = FakeApi(clock=clock, descendants=set())
    result = _attempt(
        registered,
        api,
        clock=clock.time,
        sleep=lambda seconds: clock.advance(seconds),
        job_member_probe=lambda job: None,
    )
    assert result.ok is False
    assert result.category == "cleanup_failed"
    assert result.job_empty_proven is False
    assert result.orphan_free is False
    assert result.descendant_count == 0


def test_job_handle_is_closed_only_after_the_zero_member_proof(registered: _Fixture) -> None:
    api = FakeApi()
    closed_at_probe_time: list[int] = []

    def probe(job: Any) -> int:
        closed_at_probe_time.append(len(api.closed_handles))
        return 0

    result = _run(registered, api, job_member_probe=probe)
    assert result.ok is True
    # No handle had been closed when the membership query ran.
    assert closed_at_probe_time == [0]
    # And the job handle was closed by the end of the run.
    assert "job-1" in api.closed_handles


def test_complete_job_is_terminated_on_the_success_path_too(registered: _Fixture) -> None:
    result = _run(registered, FakeApi())
    assert result.ok is True
    assert result.job_empty_proven is True


def test_timeout_still_removes_the_private_reference(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_TOTAL_RUN_DEADLINE_SECONDS", 0.2)
    session_parent = registered.root / "timeout-sessions"
    session_parent.mkdir()
    clock = FakeClock()
    api = NeverExitsApi(clock=clock)
    with pytest.raises(runner.ColibriStage2Failure, match="timeout"):
        _run(registered, api, clock=clock.time, reference_session_parent=session_parent)
    assert list(session_parent.iterdir()) == []


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
    fixture_a = _build_fixture(root_a, monkeypatch)

    fixture_b = _Fixture(root_b)
    fixture_b.exe.write_bytes(b"a completely different fake engine")
    _patch_reviewed_engine_identity(monkeypatch, fixture_b.exe.read_bytes())
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
    fixture_a = _build_fixture(root_a, monkeypatch)

    fixture_b = _Fixture(root_b)
    tampered_shard_bytes = (b"different-shard-0", fixture_b.shard_bytes[1], fixture_b.shard_bytes[2])
    for name, data in zip(common.EXPECTED_SHARD_BASENAMES, tampered_shard_bytes):
        (fixture_b.model_dir / name).write_bytes(data)
    _patch_reviewed_engine_identity(monkeypatch, fixture_b.exe.read_bytes())
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
