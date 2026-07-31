"""Tests for the closed developer CLI around the Stage 2 one-token proof.

No engine is launched: ``main`` is driven with an injected attempt function.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from odysseus_desktop_backend.services import colibri_stage2_token_cli as cli
from odysseus_desktop_backend.services.colibri_stage2_common import ColibriStage2Failure
from odysseus_desktop_backend.services.colibri_stage2_manifest import (
    REVIEWED_OLMOE_CONVERTED_MODEL as ENTRY,
)
from odysseus_desktop_backend.services.colibri_stage2_runner import (
    LatencyEvidence,
    OneTokenRunResult,
    ResourceEvidence,
    RunIdentityEvidence,
    _identity_evidence,
)

# Deliberately synthetic absolute paths on a drive letter nothing here uses.
# They are only ever checked for absoluteness and handed to an injected fake
# attempt function -- no filesystem access happens, and no test names the real
# converted-artifact directory.
ENGINE = r"Q:\synthetic\engine\olmoe.exe"
MODEL_DIR = r"Q:\synthetic\converted"
# argparse would put the program name into any usage/error text it printed.
_PROGRAM_HINT = "colibri-stage2-token-proof"
ARGV = ["--engine", ENGINE, "--converted-model-dir", MODEL_DIR, "--approve"]


def _latency(**overrides: Any) -> LatencyEvidence:
    values: dict[str, Any] = dict(
        model_load_latency_ms=12500,
        model_load_latency_state="measured",
        generation_latency_ms=540,
        generation_latency_state="measured",
        end_to_end_latency_ms=13800,
        end_to_end_latency_state="measured",
        first_output_latency_ms=13100,
        first_output_latency_state="measured",
    )
    values.update(overrides)
    return LatencyEvidence(**values)


def _result(**overrides: Any) -> OneTokenRunResult:
    values: dict[str, Any] = dict(
        category="passed",
        ok=True,
        evidence_schema_version="colibri-stage2-olmoe-token-evidence-v3",
        identities=_identity_evidence(ENTRY),
        matched_count=1,
        contract_expected_count=1,
        engine_reported_expected_count=1,
        expected_token_id=7785,
        generated_token_id=7785,
        exit_category="clean_exit",
        evidence_sha256="a" * 64,
        elapsed_ms=13900,
        exit_code=0,
        latency=_latency(),
        peak_tree_memory_bytes=205_520_896,
        peak_tree_memory_state="measured",
        cleanup_complete=True,
        session_created=True,
        reference_session_removed=True,
        job_empty_proven=True,
        job_member_count=0,
        root_exit_confirmed=True,
        descendant_count=0,
        orphan_free=True,
        reference_removed=True,
        resources=ResourceEvidence(
            cpu_time_ms=None,
            cpu_time_state="unavailable",
            process_memory_bytes=None,
            process_memory_state="unavailable",
            disk_read_bytes=None,
            disk_read_state="unavailable",
        ),
    )
    values.update(overrides)
    return OneTokenRunResult(**values)


def _invoke(argv: list[str], **kwargs: Any) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    defaults: dict[str, Any] = dict(
        interactive_check=lambda: True,
        attempt=lambda **_: _result(),
        api_factory=lambda: object(),
        stdout=stdout,
    )
    defaults.update(kwargs)
    status = cli.main(argv, **defaults)
    raw = stdout.getvalue()
    return status, json.loads(raw), raw


def _invoke_capturing_real_streams(
    argv: list[str], capsys: pytest.CaptureFixture[str], **kwargs: Any
) -> tuple[int, dict[str, Any], str, str]:
    """Drive ``main`` writing to the *real* stdout, and capture both streams.

    This is what proves argparse is not printing behind the tool's back: with
    no injected stdout, anything argparse emits lands in the captured streams
    alongside (or instead of) the closed document.
    """

    defaults: dict[str, Any] = dict(
        interactive_check=lambda: True,
        attempt=lambda **_: _result(),
        api_factory=lambda: object(),
    )
    defaults.update(kwargs)
    status = cli.main(argv, **defaults)
    captured = capsys.readouterr()
    return status, json.loads(captured.out), captured.out, captured.err


# ---------------------------------------------------------------------------
# Verified run
# ---------------------------------------------------------------------------


def test_verified_run_exits_zero_with_one_closed_document() -> None:
    status, document, raw = _invoke(ARGV)
    assert status == 0
    assert document["state"] == "verified"
    assert document["category"] == "passed"
    # Exactly one JSON document, nothing else on the stream.
    assert raw.count('"schema_version"') == 1


def test_verified_document_records_every_required_field() -> None:
    _, document, _ = _invoke(ARGV)
    assert document["identities"]["model_revision"] == ENTRY.model_revision
    assert document["identities"]["engine_sha256"] == ENTRY.engine_sha256
    assert document["identities"]["converter_kind"] == "bounded"
    assert len(document["identities"]["shard_sha256"]) == 3
    assert document["token"]["expected_token_id"] == 7785
    assert document["token"]["generated_token_id"] == 7785
    assert document["token"]["matched_count"] == 1
    assert document["process"]["exit_category"] == "clean_exit"
    assert document["process"]["exit_code"] == 0
    assert document["latency"]["model_load_latency_ms"] == 12500
    assert document["latency"]["generation_latency_ms"] == 540
    assert document["latency"]["end_to_end_latency_ms"] == 13800
    assert document["memory"]["peak_tree_memory_bytes"] == 205_520_896
    assert document["cleanup"]["job_empty_proven"] is True
    assert document["cleanup"]["session_created"] is True
    assert document["cleanup"]["reference_session_removed"] is True
    assert document["cleanup"]["job_member_count"] == 0
    assert document["cleanup"]["orphan_free"] is True
    assert document["cleanup"]["reference_removed"] is True
    assert document["evidence_sha256"] == "a" * 64


def test_document_is_json_serialisable_and_stable() -> None:
    _, document, raw = _invoke(ARGV)
    # Round-trips, and keys are sorted so two runs are diffable.
    assert json.loads(json.dumps(document)) == document
    assert raw.index('"category"') < raw.index('"token"')


# ---------------------------------------------------------------------------
# Failed attempt: still one document, still the measurements
# ---------------------------------------------------------------------------


def test_failed_attempt_exits_nonzero_but_keeps_the_measurements() -> None:
    failed = _result(
        category="token_identity_mismatch",
        ok=False,
        generated_token_id=None,
        matched_count=0,
        evidence_sha256=None,
    )
    status, document, raw = _invoke(ARGV, attempt=lambda **_: failed)
    assert status == 1
    assert document["state"] == "failed"
    assert document["category"] == "token_identity_mismatch"
    assert document["token"]["generated_token_id"] is None
    assert document["evidence_sha256"] is None
    # The cleanup proof and the latencies survive the failure.
    assert document["cleanup"]["job_empty_proven"] is True
    assert document["latency"]["model_load_latency_ms"] == 12500
    assert "Traceback" not in raw


def test_failed_attempt_reports_unavailable_measurements_honestly() -> None:
    failed = _result(
        category="timeout",
        ok=False,
        generated_token_id=None,
        matched_count=None,
        evidence_sha256=None,
        exit_category="timed_out",
        exit_code=None,
        latency=_latency(
            model_load_latency_ms=None,
            model_load_latency_state="unavailable",
            generation_latency_ms=None,
            generation_latency_state="unavailable",
        ),
        peak_tree_memory_bytes=None,
        peak_tree_memory_state="unavailable",
    )
    _, document, _ = _invoke(ARGV, attempt=lambda **_: failed)
    assert document["latency"]["model_load_latency_ms"] is None
    assert document["latency"]["model_load_latency_state"] == "unavailable"
    assert document["memory"]["peak_tree_memory_bytes"] is None
    assert document["memory"]["peak_tree_memory_state"] == "unavailable"
    assert document["process"]["exit_category"] == "timed_out"


def test_failure_metadata_is_numeric_only() -> None:
    from types import MappingProxyType

    failed = _result(
        category="nonzero_exit",
        ok=False,
        generated_token_id=None,
        evidence_sha256=None,
        failure_metadata=MappingProxyType({"exit_code": 3}),
    )
    _, document, _ = _invoke(ARGV, attempt=lambda **_: failed)
    assert document["failure_metadata"] == {"exit_code": 3}
    for value in document["failure_metadata"].values():
        assert isinstance(value, int)


# ---------------------------------------------------------------------------
# Rejected attempts: never a traceback
# ---------------------------------------------------------------------------


def test_missing_approve_flag_is_rejected() -> None:
    status, document, raw = _invoke(["--engine", ENGINE, "--converted-model-dir", MODEL_DIR])
    assert status == 2
    assert document["state"] == "rejected"
    assert document["category"] == "noninteractive_approval_rejected"
    assert "Traceback" not in raw


def test_noninteractive_session_is_rejected() -> None:
    status, document, _ = _invoke(ARGV, interactive_check=lambda: False)
    assert status == 2
    assert document["category"] == "noninteractive_approval_rejected"


def test_relative_paths_are_rejected() -> None:
    status, document, _ = _invoke(
        ["--engine", "olmoe.exe", "--converted-model-dir", MODEL_DIR, "--approve"]
    )
    assert status == 2
    assert document["category"] == "unsafe_directory_rejected"


def test_closed_precondition_failure_becomes_a_rejected_document() -> None:
    def raising(**_: Any) -> OneTokenRunResult:
        raise ColibriStage2Failure("reviewed_model_manifest_unavailable")

    status, document, raw = _invoke(ARGV, attempt=raising)
    assert status == 2
    assert document["state"] == "rejected"
    assert document["category"] == "reviewed_model_manifest_unavailable"
    assert "Traceback" not in raw


def test_unexpected_exception_never_leaks_a_traceback_or_its_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def exploding(**_: Any) -> OneTokenRunResult:
        raise RuntimeError(r"boom in C:\Users\someone\secret\path with TOKEN=abc123")

    status, document, out, err = _invoke_capturing_real_streams(
        ARGV, capsys, attempt=exploding
    )
    assert status == 3
    assert document["state"] == "rejected"
    assert err == ""
    for stream in (out, err):
        assert "Traceback" not in stream
        assert "RuntimeError" not in stream
        assert "secret" not in stream
        assert "TOKEN=abc123" not in stream
        assert "someone" not in stream


def test_unexpected_exception_is_not_classified_as_malformed_output() -> None:
    def exploding(**_: Any) -> OneTokenRunResult:
        raise RuntimeError("internal defect")

    _, document, _ = _invoke(ARGV, attempt=exploding)
    # Blaming the engine's output for our own bug would be false, and would
    # imply output was parsed when it may never have been reached.
    assert document["category"] == "unexpected_internal_failure"
    assert document["category"] != "malformed_output"


def test_unexpected_exception_claims_no_cleanup_or_prelaunch_facts() -> None:
    def exploding(**_: Any) -> OneTokenRunResult:
        raise RuntimeError("internal defect")

    _, document, _ = _invoke(ARGV, attempt=exploding)
    # The failure could have happened anywhere, including after a launch, so
    # nothing about cleanup or the job may be asserted.
    assert document["cleanup"]["cleanup_complete"] is None
    assert document["pre_launch_rejection"] is None
    assert document["cleanup"]["job_empty_proven"] is False
    assert document["cleanup"]["job_member_count"] is None
    assert document["cleanup"]["orphan_free"] is False
    assert document["cleanup"]["root_exit_confirmed"] is False


def test_cleanup_fields_report_a_created_and_removed_session() -> None:
    # The shape of the first real invocation, corrected: pre-launch failure,
    # no process, but a session directory was created and then removed.
    failed = _result(
        category="reference_write_failed",
        ok=False,
        generated_token_id=None,
        matched_count=None,
        engine_reported_expected_count=None,
        evidence_sha256=None,
        exit_category="not_observed",
        exit_code=None,
        session_created=True,
        reference_session_removed=True,
        reference_removed=False,
        cleanup_complete=True,
        job_empty_proven=False,
        job_member_count=None,
        root_exit_confirmed=False,
        descendant_count=None,
        orphan_free=False,
    )
    status, document, _ = _invoke(ARGV, attempt=lambda **_: failed)
    assert status == 1
    assert document["state"] == "failed"
    assert document["cleanup"]["session_created"] is True
    assert document["cleanup"]["reference_session_removed"] is True
    # No reference file existed, so none is claimed removed.
    assert document["cleanup"]["reference_removed"] is False
    # And no process claim is manufactured.
    assert document["cleanup"]["job_empty_proven"] is False
    assert document["cleanup"]["job_member_count"] is None
    assert document["cleanup"]["orphan_free"] is False
    assert document["process"]["exit_category"] == "not_observed"


def test_failed_session_removal_is_reported_as_incomplete_cleanup() -> None:
    failed = _result(
        category="cleanup_failed",
        ok=False,
        generated_token_id=None,
        evidence_sha256=None,
        session_created=True,
        reference_session_removed=False,
        cleanup_complete=False,
    )
    _, document, _ = _invoke(ARGV, attempt=lambda **_: failed)
    # cleanup_complete must never be manufactured as true.
    assert document["cleanup"]["cleanup_complete"] is False
    assert document["cleanup"]["session_created"] is True
    assert document["cleanup"]["reference_session_removed"] is False


def test_closed_prelaunch_rejection_may_state_the_facts_it_established() -> None:
    def raising(**_: Any) -> OneTokenRunResult:
        raise ColibriStage2Failure("reviewed_model_manifest_unavailable")

    _, document, _ = _invoke(ARGV, attempt=raising)
    # A closed failure out of the attempt can only come from a precondition
    # checked before the cleanup-owned block, so "nothing was created" is
    # known and may be stated.
    assert document["pre_launch_rejection"] is True
    assert document["cleanup"]["cleanup_complete"] is True
    assert document["cleanup"]["session_created"] is False
    assert document["cleanup"]["reference_session_removed"] is False
    assert document["cleanup"]["job_empty_proven"] is False


def test_unexpected_failure_claims_nothing_about_a_session() -> None:
    def exploding(**_: Any) -> OneTokenRunResult:
        raise RuntimeError("internal defect")

    _, document, _ = _invoke(ARGV, attempt=exploding)
    # Could have happened anywhere, so session creation is unknown, not False.
    assert document["cleanup"]["session_created"] is None
    assert document["cleanup"]["cleanup_complete"] is None


def test_argparse_failure_still_emits_one_closed_document() -> None:
    status, document, raw = _invoke(["--engine", ENGINE])
    assert status == 2
    assert document["state"] == "rejected"
    assert document["category"] == "cli_arguments_rejected"
    assert "Traceback" not in raw


# ---------------------------------------------------------------------------
# Argparse must never write to the real streams
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--engine", ENGINE],                                  # missing required option
        ["--converted-model-dir", MODEL_DIR, "--approve"],     # missing required option
        ["--engine", ENGINE, "--converted-model-dir", MODEL_DIR, "--nonsense"],
        ["--engine"],                                          # option expects a value
        [],                                                    # nothing at all
    ],
)
def test_argument_rejection_writes_nothing_but_the_document(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    status, document, out, err = _invoke_capturing_real_streams(argv, capsys)
    assert status == 2
    assert document["category"] == "cli_arguments_rejected"
    # stderr is completely empty -- no usage line, no error text.
    assert err == ""
    # stdout is the document and nothing else.
    assert out.strip().startswith("{")
    assert out.strip().endswith("}")
    assert json.loads(out) == document
    for leaked in ("usage:", "error:", _PROGRAM_HINT, ENGINE, MODEL_DIR, "--nonsense"):
        assert leaked not in out
        assert leaked not in err


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_is_unsupported_and_emits_one_closed_document(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Help text is not the closed document, so it is deliberately unsupported.
    status, document, out, err = _invoke_capturing_real_streams([flag], capsys)
    assert status == 2
    assert document["state"] == "rejected"
    assert document["category"] == "cli_arguments_rejected"
    assert err == ""
    assert json.loads(out) == document
    assert "usage:" not in out
    assert "show this help message" not in out


def test_a_successful_run_writes_nothing_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    status, document, out, err = _invoke_capturing_real_streams(ARGV, capsys)
    assert status == 0
    assert err == ""
    assert json.loads(out) == document


def test_parser_never_formats_usage_or_help_text() -> None:
    parser = cli._build_parser()
    assert parser.format_usage() == ""
    assert parser.format_help() == ""
    # And its error/exit paths raise rather than printing and exiting.
    with pytest.raises(cli._ArgumentsRejected):
        parser.error("boom in C:\\Users\\someone\\path")
    with pytest.raises(cli._ArgumentsRejected):
        parser.exit(2, "message")


# ---------------------------------------------------------------------------
# Privacy: nothing local ever reaches the document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [ARGV])
def test_document_never_carries_paths_environment_or_usernames(argv: list[str]) -> None:
    import getpass

    _, _, raw = _invoke(argv)
    assert ENGINE not in raw
    assert MODEL_DIR not in raw
    assert "olmoe.exe" not in raw
    assert "converted" not in raw
    assert "SNAP" not in raw
    assert "OMP_NUM_THREADS" not in raw
    assert "TEMP" not in raw
    try:
        username = getpass.getuser()
    except Exception:  # noqa: BLE001
        username = ""
    if username:
        assert username not in raw


def test_document_never_carries_the_prompt_or_raw_streams() -> None:
    _, _, raw = _invoke(ARGV)
    assert "Matching tokens" not in raw
    assert "C engine" not in raw
    assert "resident weights" not in raw
    assert "prompt" not in raw.lower()


def test_rejected_document_also_carries_no_paths() -> None:
    _, _, raw = _invoke(ARGV, interactive_check=lambda: False)
    assert ENGINE not in raw
    assert MODEL_DIR not in raw


# ---------------------------------------------------------------------------
# The CLI supplies only paths -- never an identity
# ---------------------------------------------------------------------------


def test_cli_accepts_only_the_two_paths_and_approve() -> None:
    parser = cli._build_parser()
    options = {action.dest for action in parser._actions}
    # No `help` action: auto-help would print non-JSON to stdout.
    assert options == {"engine", "converted_model_dir", "approve"}


def test_attempt_is_called_with_no_identity_arguments() -> None:
    captured: dict[str, Any] = {}

    def recording(**kwargs: Any) -> OneTokenRunResult:
        captured.update(kwargs)
        return _result()

    _invoke(ARGV, attempt=recording)
    forbidden = {
        "expected_sha256",
        "expected_token_id",
        "expected_generated_token_id",
        "manifest",
        "registry",
        "model_revision",
        "engine_sha256",
        "converter_kind",
        "cap_argument",
        "bits_argument",
        "prompt_token_ids",
        "tokenizer",
        "ref_path",
    }
    assert not (set(captured) & forbidden)
    assert captured["approved"] is True


def test_identity_evidence_in_the_document_comes_from_the_registry() -> None:
    # The document's identities must equal the reviewed registry entry's, not
    # anything derived from the CLI arguments.
    _, document, _ = _invoke(ARGV)
    identities: RunIdentityEvidence = _identity_evidence(ENTRY)
    assert document["identities"]["engine_sha256"] == identities.engine_sha256
    assert document["identities"]["config_sha256"] == identities.config_sha256
    assert document["identities"]["reference_sha256"] == identities.reference_sha256
    assert document["identities"]["cap_argument"] == "8"
    assert document["identities"]["bits_argument"] == "8"
