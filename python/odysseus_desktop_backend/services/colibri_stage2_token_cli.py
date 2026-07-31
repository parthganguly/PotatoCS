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
            # single generated token was parsed. Never the expected value --
            # on a wrong-token run this is the actual wrong integer.
            "generated_token_id": result.generated_token_id,
            "matched_count": result.matched_count,
            "contract_expected_count": result.contract_expected_count,
            "engine_reported_expected_count": result.engine_reported_expected_count,
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
            "session_created": result.session_created,
            "reference_session_removed": result.reference_session_removed,
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


def build_rejected_document(
    category: str, metadata: Mapping[str, int], *, pre_launch_established: bool
) -> dict[str, Any]:
    """The closed record for an attempt that produced no result object.

    ``pre_launch_established`` says whether it is a *proven* fact that no
    process was ever created. It is true only for rejections that happen
    before ``attempt_one_token_proof`` can launch anything -- argument
    rejection, missing approval, a non-absolute path, or a closed
    ``ColibriStage2Failure`` raised out of the attempt (which by construction
    only escapes from preconditions checked before process creation).

    It is false for an unexpected internal failure, which could have occurred
    anywhere, including after a launch. In that case nothing is claimed:
    ``cleanup_complete`` and ``pre_launch_rejection`` are ``null`` rather than
    ``true``, and no proof is asserted. A document must never report a
    cleanup or job-empty fact that was not established.
    """

    return {
        "schema_version": TOKEN_RUN_EVIDENCE_SCHEMA_VERSION,
        "state": STATE_REJECTED,
        "category": category,
        "pre_launch_rejection": True if pre_launch_established else None,
        "pins": {
            "model_revision": PINNED_MODEL_REVISION,
            "colibri_commit": PINNED_COLIBRI_COMMIT,
        },
        "token": {
            "expected_token_id": None,
            "generated_token_id": None,
            "matched_count": None,
            "contract_expected_count": None,
            "engine_reported_expected_count": None,
        },
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
            # True only where "nothing was created, so nothing was owed" is an
            # established fact; null where it is simply not known.
            #
            # A raised `ColibriStage2Failure` reaching here is side-effect
            # free by construction: `attempt_one_token_proof` only raises
            # before its cleanup-owned block, and every failure from the first
            # side effect onward is reported as a result instead. A failure
            # that created a private session directory therefore never lands
            # in this document -- it lands in `build_attempt_document` with
            # the real `session_created` / `reference_session_removed` values.
            "cleanup_complete": True if pre_launch_established else None,
            "session_created": False if pre_launch_established else None,
            "reference_session_removed": False,
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


class _ArgumentsRejected(Exception):
    """Raised instead of argparse printing anything and calling ``exit``."""


class _SilentArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that never writes to stdout or stderr.

    Stock argparse writes usage, error text, and help directly to the real
    streams and then calls ``sys.exit``. Any of those would put the offending
    argument -- which is a local path -- onto a stream this tool promises
    carries nothing but one closed JSON document. Every printing and exiting
    path is therefore overridden to raise instead.

    ``--help`` is deliberately unsupported for the same reason: emitting help
    text would mean emitting something that is not the closed document. It is
    rejected like any other unrecognised argument.
    """

    def _print_message(self, message: str, file: Any = None) -> None:  # noqa: D102
        return

    def print_usage(self, file: Any = None) -> None:  # noqa: D102
        return

    def print_help(self, file: Any = None) -> None:  # noqa: D102
        return

    def format_usage(self) -> str:  # noqa: D102
        return ""

    def format_help(self) -> str:  # noqa: D102
        return ""

    def error(self, message: str) -> Any:  # noqa: D102
        # `message` names the offending argument and is discarded unread.
        raise _ArgumentsRejected()

    def exit(self, status: int = 0, message: str | None = None) -> Any:  # noqa: D102
        raise _ArgumentsRejected()


def _build_parser() -> argparse.ArgumentParser:
    parser = _SilentArgumentParser(
        prog=_PROGRAM,
        description="Run the reviewed Colibri Stage 2 one-token proof exactly once.",
        # No auto-help: `-h`/`--help` would print non-JSON to stdout.
        add_help=False,
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

    def emit(document: dict[str, Any], status: int) -> int:
        json.dump(document, out, indent=2, sort_keys=True)
        out.write("\n")
        return status

    parser = _build_parser()
    try:
        arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except (_ArgumentsRejected, SystemExit):
        # The silent parser wrote nothing anywhere; the offending argument
        # (a local path) is discarded unread. `SystemExit` is caught too, so
        # no argparse path can bypass the closed document.
        return emit(
            build_rejected_document("cli_arguments_rejected", {}, pre_launch_established=True),
            EXIT_REJECTED,
        )

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
        # A closed failure escaping `attempt_one_token_proof` can only come
        # from a precondition checked before process creation, so "nothing was
        # launched" is an established fact here.
        return emit(
            build_rejected_document(exc.category, exc.numeric_metadata, pre_launch_established=True),
            EXIT_REJECTED,
        )
    except Exception:  # noqa: BLE001 - a traceback must never reach stdout/stderr
        # A defect in this code, not an engine outcome: classified as
        # `unexpected_internal_failure`, never as `malformed_output`. The
        # exception object is deliberately swallowed unread -- its message
        # could carry a local path or an environment value -- and nothing is
        # claimed about cleanup, because this could have happened at any point.
        return emit(
            build_rejected_document(
                "unexpected_internal_failure", {}, pre_launch_established=False
            ),
            EXIT_UNEXPECTED,
        )

    return emit(build_attempt_document(result), EXIT_VERIFIED if result.ok else EXIT_FAILED)


if __name__ == "__main__":  # pragma: no cover - exercised via main(argv=...)
    raise SystemExit(main())
