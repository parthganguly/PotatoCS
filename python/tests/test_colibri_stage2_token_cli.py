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
        evidence_schema_version="colibri-stage2-olmoe-token-evidence-v2",
        identities=_identity_evidence(ENTRY),
        matched_count=1,
        expected_count=1,
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


def test_unexpected_exception_never_leaks_a_traceback_or_its_message() -> None:
    def exploding(**_: Any) -> OneTokenRunResult:
        raise RuntimeError(r"boom in C:\Users\someone\secret\path with TOKEN=abc123")

    status, document, raw = _invoke(ARGV, attempt=exploding)
    assert status == 3
    assert document["state"] == "rejected"
    assert "Traceback" not in raw
    assert "secret" not in raw
    assert "TOKEN=abc123" not in raw
    assert "someone" not in raw


def test_argparse_failure_still_emits_one_closed_document() -> None:
    status, document, raw = _invoke(["--engine", ENGINE])
    assert status == 2
    assert document["state"] == "rejected"
    assert "Traceback" not in raw


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
    assert options == {"help", "engine", "converted_model_dir", "approve"}


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
