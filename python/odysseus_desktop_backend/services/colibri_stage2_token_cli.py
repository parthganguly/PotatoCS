"""Developer-only CLI for the Colibrì Stage 2 real one-token proof.

Emits exactly one closed JSON document on stdout -- on success *and* on
failure -- and never a Python traceback. Exit status is ``0`` only when a
generated token was parsed from the engine's own output and independently
verified against the reviewed registry entry; every other outcome exits
nonzero while still reporting whatever was measured.

Usage (interactive terminal only)::

    python -m odysseus_desktop_backend.services.colibri_stage2_token_cli \
        --engine <absolute-path-to-olmoe.exe> \
        --converted-model-dir <absolute-path-to-converted-model-dir> \
        --approve

The two paths are the only inputs. Every expected identity -- model
repository and revision, Colibrì commit, engine digest, converter kind and
digest, config and shard digests, the reference digest, cap, bits, the prompt
token ids, and the expected generated token -- comes solely from
``REVIEWED_OLMOE_MODEL_REGISTRY``. There is no flag that can supply, relax,
or override any of them.

The emitted document is closed by construction: it is assembled field by
field from categories, pinned identities, small integers, and measurement
states. Raw engine streams, local paths, environment values, usernames, and
prompt text are never placed in it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from odysseus_desktop_backend.services.colibri_stage2_common import (
    EVIDENCE_STATE_UNAVAILABLE,
    PINNED_COLIBRI_COMMIT,
    PINNED_MODEL_REVISION,
    TOKEN_RUN_EVIDENCE_SCHEMA_VERSION,
    ColibriStage2Failure,
)
from odysseus_desktop_backend.services.colibri_stage2_runner import (
    OneTokenRunResult,
    attempt_one_token_proof,
)

_PROGRAM = "colibri-stage2-token-proof"

STATE_VERIFIED = "verified"
STATE_FAILED = "failed"
STATE_REJECTED = "rejected"

EXIT_VERIFIED = 0
EXIT_FAILED = 1
EXIT_REJECTED = 2
EXIT_UNEXPECTED = 3


def _interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _identity_document(result: OneTokenRunResult) -> dict[str, Any]:
    identities = result.identities
    return {
        "model_repository": identities.model_repository,
        "model_revision": identities.model_revision,
        "colibri_commit": identities.colibri_commit,
        "engine_sha256": identities.engine_sha256,
        "converter_kind": identities.converter_kind,
        "converter_sha256": identities.converter_sha256,
        "config_sha256": identities.config_sha256,
        "shard_sha256": list(identities.shard_sha256),
        "reference_sha256": identities.reference_sha256,
        "cap_argument": identities.cap_argument,
        "bits_argument": identities.bits_argument,
    }


def _latency_document(result: OneTokenRunResult) -> dict[str, Any]:
    latency = result.latency
    return {
        # Engine-reported, parsed from the pinned engine's own output lines.
        "model_load_latency_ms": latency.model_load_latency_ms,
        "model_load_latency_state": latency.model_load_latency_state,
        "generation_latency_ms": latency.generation_latency_ms,
        "generation_latency_state": latency.generation_latency_state,
        # Independently measured by this process from a monotonic clock.
        "end_to_end_latency_ms": latency.end_to_end_latency_ms,
        "end_to_end_latency_state": latency.end_to_end_latency_state,
        "first_output_latency_ms": latency.first_output_latency_ms,
        "first_output_latency_state": latency.first_output_latency_state,
    }


def build_attempt_document(result: OneTokenRunResult) -> dict[str, Any]:
    """The closed record for a completed attempt (verified or failed)."""

    return {
        "schema_version": result.evidence_schema_version,
        "state": STATE_VERIFIED if result.ok else STATE_FAILED,
        "category": result.category,
        "identities": _identity_document(result),
        "token": {
            "expected_token_id": result.expected_token_id,
            # Read from the engine's own ``C engine :`` line, or null when no
            # generated token was ever parsed. Never the expected value.
            "generated_token_id": result.generated_token_id,
            "matched_count": result.matched_count,
            "expected_count": result.expected_count,
        },
        "process": {
            "exit_category": result.exit_category,
            "exit_code": result.exit_code,
        },
        "latency": _latency_document(result),
        "memory": {
            "peak_tree_memory_bytes": result.peak_tree_memory_bytes,
            "peak_tree_memory_state": result.peak_tree_memory_state,
        },
        "cleanup": {
            "cleanup_complete": result.cleanup_complete,
            "job_empty_proven": result.job_empty_proven,
            "job_member_count": result.job_member_count,
            "root_exit_confirmed": result.root_exit_confirmed,
            "descendant_count": result.descendant_count,
            "orphan_free": result.orphan_free,
            "reference_removed": result.reference_removed,
        },
        "evidence_sha256": result.evidence_sha256,
        "failure_metadata": dict(result.failure_metadata),
    }


def build_rejected_document(category: str, metadata: Mapping[str, int]) -> dict[str, Any]:
    """The closed record for an attempt that never started.

    A precondition rejection (manifest gate, approval, path safety, identity
    mismatch) has no process, no measurement, and no cleanup to report, so
    those sections are reported as explicitly unavailable rather than as
    zeroes that would read like observations.
    """

    return {
        "schema_version": TOKEN_RUN_EVIDENCE_SCHEMA_VERSION,
        "state": STATE_REJECTED,
        "category": category,
        "pins": {
            "model_revision": PINNED_MODEL_REVISION,
            "colibri_commit": PINNED_COLIBRI_COMMIT,
        },
        "token": {"expected_token_id": None, "generated_token_id": None},
        "process": {"exit_category": "not_observed", "exit_code": None},
        "latency": {
            "model_load_latency_ms": None,
            "model_load_latency_state": EVIDENCE_STATE_UNAVAILABLE,
            "generation_latency_ms": None,
            "generation_latency_state": EVIDENCE_STATE_UNAVAILABLE,
            "end_to_end_latency_ms": None,
            "end_to_end_latency_state": EVIDENCE_STATE_UNAVAILABLE,
            "first_output_latency_ms": None,
            "first_output_latency_state": EVIDENCE_STATE_UNAVAILABLE,
        },
        "memory": {
            "peak_tree_memory_bytes": None,
            "peak_tree_memory_state": EVIDENCE_STATE_UNAVAILABLE,
        },
        "cleanup": {
            "cleanup_complete": True,
            "job_empty_proven": False,
            "job_member_count": None,
            "root_exit_confirmed": False,
            "descendant_count": None,
            "orphan_free": False,
            "reference_removed": False,
        },
        "evidence_sha256": None,
        "failure_metadata": dict(metadata),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROGRAM,
        description="Run the reviewed Colibri Stage 2 one-token proof exactly once.",
        add_help=True,
    )
    parser.add_argument(
        "--engine",
        required=True,
        help="Absolute path to the reviewed olmoe.exe. Its identity is still verified against the registry.",
    )
    parser.add_argument(
        "--converted-model-dir",
        required=True,
        help="Absolute path to the converted model directory. Its contents are still verified against the registry.",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicit approval to launch the real engine. Required.",
    )
    return parser


def _require_absolute(raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ColibriStage2Failure("unsafe_directory_rejected")
    return candidate


def main(
    argv: Sequence[str] | None = None,
    *,
    interactive_check: Callable[[], bool] = _interactive,
    attempt: Callable[..., OneTokenRunResult] = attempt_one_token_proof,
    api_factory: Callable[[], Any] | None = None,
    stdout: Any = None,
) -> int:
    """Emit exactly one closed JSON document and return the exit status."""

    out = sys.stdout if stdout is None else stdout
    parser = _build_parser()
    try:
        arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:
        # argparse already wrote its own usage text to stderr; emit the closed
        # document on stdout so a caller always gets exactly one record.
        document = build_rejected_document("unsafe_basename_rejected", {})
        json.dump(document, out, indent=2, sort_keys=True)
        out.write("\n")
        return EXIT_REJECTED

    try:
        if not arguments.approve:
            raise ColibriStage2Failure("noninteractive_approval_rejected")
        if not interactive_check():
            raise ColibriStage2Failure("noninteractive_approval_rejected")
        engine = _require_absolute(arguments.engine)
        converted_model_dir = _require_absolute(arguments.converted_model_dir)

        if api_factory is None:
            if sys.platform != "win32":
                raise ColibriStage2Failure("platform_unsupported")
            from odysseus_desktop_backend.runtime_bench.isolated_server import WindowsLifecycleApi

            api: Any = WindowsLifecycleApi()
        else:
            api = api_factory()

        result = attempt(
            olmoe_exe=engine,
            converted_model_dir=converted_model_dir,
            api=api,
            approved=True,
            interactive_check=interactive_check,
        )
    except ColibriStage2Failure as exc:
        document = build_rejected_document(exc.category, exc.numeric_metadata)
        json.dump(document, out, indent=2, sort_keys=True)
        out.write("\n")
        return EXIT_REJECTED
    except Exception:  # noqa: BLE001 - a traceback must never reach stdout/stderr
        # Deliberately swallows the exception object: its message could carry
        # a local path or an environment value. The closed category says all a
        # caller may learn.
        document = build_rejected_document("malformed_output", {})
        json.dump(document, out, indent=2, sort_keys=True)
        out.write("\n")
        return EXIT_UNEXPECTED

    document = build_attempt_document(result)
    json.dump(document, out, indent=2, sort_keys=True)
    out.write("\n")
    return EXIT_VERIFIED if result.ok else EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - exercised via main(argv=...)
    raise SystemExit(main())
