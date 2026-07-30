"""Shared closed contract for the Colibrì Stage 2A OLMoE scaffold.

Every Stage 2 module (manifest, conversion capture, derived reference, real
token runner) imports its pinned identifiers and failure vocabulary from
here so the five pieces can never silently disagree about which model,
commit, or shard set is in scope. Nothing in this module downloads,
converts, or executes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

# --- Pinned upstream contract (Stage 2A) -----------------------------------

PINNED_COLIBRI_COMMIT = "72d3d37231e922a6fa9afca16e08fa45842d5eb4"
PINNED_MODEL_REPOSITORY = "allenai/OLMoE-1B-7B-0125-Instruct"
PINNED_MODEL_REVISION = "b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e"
PINNED_LICENSE_IDENTIFIER = "Apache-2.0"

EXPECTED_ENGINE_BASENAME = "olmoe.exe"
EXPECTED_CONFIG_BASENAME = "config.json"
EXPECTED_SHARD_BASENAMES = (
    "model-00001-of-00003.safetensors",
    "model-00002-of-00003.safetensors",
    "model-00003-of-00003.safetensors",
)
EXPECTED_REF_BASENAME = "olmoe-stage2-one-token-ref.json"

PROMPT_TOKEN_IDS = (510, 5347, 273, 6181, 310)
EXPECTED_GENERATED_TOKEN_ID = 7785
FULL_TOKEN_IDS = PROMPT_TOKEN_IDS + (EXPECTED_GENERATED_TOKEN_ID,)

CAP_ARGUMENT = "8"
BITS_ARGUMENT = "8"

APPROX_DOWNLOAD_BYTES = 13_840_000_000
REQUIRED_FREE_SPACE_BYTES = 18 * 1024 * 1024 * 1024

# v2 binds each registry entry to the *executed* converter (kind plus its
# full reviewed identity, not just the upstream script's digest) and to the
# token contract itself (cap, bits, the prompt token ids, and the single
# expected generated token id), so one reviewed record now covers every
# input the real one-token command depends on.
MANIFEST_EVIDENCE_SCHEMA_VERSION = "colibri-stage2-olmoe-manifest-v2"
# v2 added the per-shard resume booleans (``source_reused`` /
# ``converted_reused``) and the bounded per-shard conversion peak-memory
# evidence. v3 replaces the single top-level ``converter_basename`` /
# ``converter_size_bytes`` / ``converter_sha256`` triple -- which always
# named the upstream script even when the bounded converter had actually
# run -- with a ``converters`` list plus a per-shard ``converter_kind``
# and identity, both derived from the adapter that really executed.
CONVERSION_CAPTURE_SCHEMA_VERSION = "colibri-stage2-olmoe-conversion-capture-v3"
CONVERSION_CAPTURE_STATE = "unreviewed_conversion_capture"

ALLOWED_CONVERSION_DEPENDENCY_NAMES = frozenset({"python", "torch", "safetensors"})

EXPECTED_CONVERTER_SCRIPT_BASENAME = "convert_olmoe.py"
EXPECTED_BOUNDED_CONVERTER_BASENAME = "colibri_stage2_bounded_convert.py"
DEVIATION_STATEMENT = (
    "Selecting allenai/OLMoE-1B-7B-0125-Instruct instead of the 0924 release "
    "is deliberate: 0125-Instruct is the reviewed, instruction-tuned revision "
    "pinned for this proof."
)
APPROVAL_STATEMENT = (
    "No download occurs without explicit approval: this is a dry run only."
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SIMPLE_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,31}$")

MAX_METADATA_NUMBER = 2**63 - 1

STAGE2_FAILURE_CATEGORIES = frozenset(
    {
        # Manifest gate (Part 2)
        "reviewed_model_manifest_unavailable",
        "manifest_pin_mismatch",
        "malformed_registry",
        # Download/conversion plan (Part 3)
        "source_model_manifest_unreviewed",
        "noninteractive_approval_rejected",
        "destination_not_empty",
        "insufficient_disk_space",
        "python_environment_unavailable",
        "dependency_unavailable",
        "unsafe_basename_rejected",
        "unsafe_directory_rejected",
        "partial_already_exists",
        "shard_download_failed",
        "shard_verification_failed",
        "conversion_failed",
        # Distinguished conversion-process outcomes: a converter that ran
        # out of its deadline, one that exited nonzero under its own
        # control, and one the OS killed with a native exception (on
        # Windows, an NTSTATUS such as 0xc0000005) are three different
        # facts and must never collapse into one category.
        "conversion_timeout",
        "conversion_nonzero_exit",
        "conversion_process_crashed",
        "conversion_output_unexpected",
        "converted_shard_missing",
        "converted_shard_already_exists",
        # Resume gates
        "stale_source_file_rejected",
        "resume_state_invalid",
        "source_shard_deletion_failed",
        "source_shard_deletion_unverified",
        "temporary_output_cleanup_failed",
        # Derived reference (Part 4)
        "reference_write_failed",
        "reference_cleanup_failed",
        "reference_hash_mismatch",
        # Real token runner (Part 5)
        "executable_not_found",
        "executable_identity_unavailable",
        "runtime_identity_mismatch",
        "missing_converted_shard",
        "unknown_converted_shard",
        "reparse_point_rejected",
        "process_create_failed",
        "job_create_failed",
        "job_assignment_failed",
        "process_resume_failed",
        "output_overflow",
        "timeout",
        "nonzero_exit",
        "stderr_present",
        "match_count_mismatch",
        "duplicate_match_line",
        "malformed_output",
        "token_identity_mismatch",
        # Distinguished engine-output defects. Collapsing these into
        # `malformed_output` would hide *which* part of the reviewed dialect
        # the engine violated, and the difference between "the engine printed
        # the wrong reference" and "the engine contradicted itself" matters
        # for triage.
        "duplicate_output_line",
        "reference_line_mismatch",
        "generated_token_count_unexpected",
        "output_internally_inconsistent",
        "timing_evidence_invalid",
        "output_decode_failed",
        "cleanup_failed",
        "orphan_detected",
        "platform_unsupported",
    }
)

FAILURE_NUMERIC_METADATA_KEYS = frozenset(
    {
        "exit_code",
        "timeout_ms",
        "win32_code",
        "bytes_observed",
        "matched_count",
        "expected_count",
        "elapsed_ms",
        # Bounded resource evidence for a converter child process. Both are
        # plain byte counts read from the OS; neither can carry a path, an
        # environment value, or any model output.
        "peak_memory_bytes",
        "peak_commit_bytes",
    }
)

RESUME_LEDGER_BASENAME = "colibri-stage2-resume.json"
RESUME_LEDGER_SCHEMA_VERSION = "colibri-stage2-olmoe-resume-v1"


# --- Closed evidence vocabulary for the real one-token run ------------------

# v2 replaces the resume-to-first-byte "startup latency" with the engine's
# own reported model-load and one-token generation timings, keeps
# resume-to-exit as the independently measured end-to-end figure, records the
# *parsed* generated token id rather than the expected one, and adds the Job
# Object zero-member proof.
TOKEN_RUN_EVIDENCE_SCHEMA_VERSION = "colibri-stage2-olmoe-token-evidence-v2"

# Bounds every engine-reported timing must satisfy to be recorded at all.
# A value that is negative, non-finite, or absurd is rejected outright
# rather than clamped: a clamped number would look like a measurement.
MAX_ENGINE_REPORTED_SECONDS = 86_400.0
MAX_ENGINE_REPORTED_RATE = 1_000_000_000.0

# Every optional measurement in the run evidence carries its own state, so a
# missing number is always distinguishable from a measured zero and can never
# be silently read as "0 ms" or "0 bytes".
EVIDENCE_STATE_MEASURED = "measured"
EVIDENCE_STATE_UNAVAILABLE = "unavailable"
EVIDENCE_STATES = frozenset({EVIDENCE_STATE_MEASURED, EVIDENCE_STATE_UNAVAILABLE})

# The closed process-exit vocabulary. This is a *category*, never a raw
# status string, message, or captured stream: `clean_exit` means the process
# was observed exiting with code 0, `nonzero_exit` means it was observed
# exiting with any other code, `timed_out` means no exit was observed before
# the absolute deadline, and `not_observed` means the run failed before an
# exit could be sampled at all (e.g. process creation failed).
EXIT_CATEGORY_CLEAN = "clean_exit"
EXIT_CATEGORY_NONZERO = "nonzero_exit"
EXIT_CATEGORY_TIMED_OUT = "timed_out"
EXIT_CATEGORY_NOT_OBSERVED = "not_observed"
EXIT_CATEGORIES = frozenset(
    {
        EXIT_CATEGORY_CLEAN,
        EXIT_CATEGORY_NONZERO,
        EXIT_CATEGORY_TIMED_OUT,
        EXIT_CATEGORY_NOT_OBSERVED,
    }
)


class ColibriStage2Failure(RuntimeError):
    """A closed failure category with optional bounded numeric evidence.

    No raw path, prompt text, environment value, or subprocess output may
    ever be attached here; only the fixed category and small non-negative
    integers are carried.
    """

    def __init__(self, category: str, **numeric_metadata: int) -> None:
        if category not in STAGE2_FAILURE_CATEGORIES:
            raise ValueError(f"unknown Stage 2 failure category: {category}")
        clean: dict[str, int] = {}
        for key, value in numeric_metadata.items():
            if key not in FAILURE_NUMERIC_METADATA_KEYS:
                raise ValueError(f"unknown failure metadata key: {key}")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_METADATA_NUMBER:
                raise ValueError(f"failure metadata {key} is out of bounds")
            clean[key] = value
        super().__init__(category)
        self.category = category
        self.numeric_metadata = clean

    def as_record(self) -> dict[str, Any]:
        return {"category": self.category, "numeric_metadata": dict(self.numeric_metadata)}


def is_hex40(value: str) -> bool:
    return isinstance(value, str) and bool(_HEX40.fullmatch(value))


def is_hex64(value: str) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def is_safe_basename(value: str) -> bool:
    return isinstance(value, str) and bool(_SAFE_BASENAME.fullmatch(value))


def is_simple_version(value: str) -> bool:
    return isinstance(value, str) and bool(_SIMPLE_VERSION.fullmatch(value))


# --- Reviewed real build evidence ------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewedEngineIdentity:
    """The one immutable, human-reviewed identity of the real,
    deterministically-built ``olmoe.exe`` -- proven byte-identical across
    two independent clean builds of the pinned Colibrì commit under a
    fixed ``SOURCE_DATE_EPOCH``.

    This proves reproducible compilation only. It is not a claim about
    model loading or token generation.
    """

    colibri_commit: str
    basename: str
    size_bytes: int
    sha256: str
    source_date_epoch: int
    deterministic_build_count: int

    def __post_init__(self) -> None:
        if not is_hex40(self.colibri_commit) or self.colibri_commit != PINNED_COLIBRI_COMMIT:
            raise ValueError("reviewed engine identity colibri_commit does not match the pinned commit")
        if self.basename != EXPECTED_ENGINE_BASENAME:
            raise ValueError("reviewed engine identity basename does not match the expected engine")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes <= 0:
            raise ValueError("reviewed engine identity size_bytes is out of bounds")
        if not is_hex64(self.sha256):
            raise ValueError("reviewed engine identity sha256 is not a SHA-256")
        if (
            isinstance(self.source_date_epoch, bool)
            or not isinstance(self.source_date_epoch, int)
            or self.source_date_epoch <= 0
        ):
            raise ValueError("reviewed engine identity source_date_epoch is out of bounds")
        if self.deterministic_build_count != 2:
            raise ValueError("reviewed engine identity must record exactly two deterministic builds")


# Populated from the real, reviewed two-build verifier result: pinned
# Colibrì commit `72d3d37231e922a6fa9afca16e08fa45842d5eb4`,
# SOURCE_DATE_EPOCH `1784223580`, clean-build A and B SHA-256 both
# `d7beaf6fe35de265cfaeb1d07914deeea6ceb8b3650e79b76e9c6d77176b528d`
# (byte-identical). Never a caller-supplied override -- OlmoeModelManifest
# requires its engine fields to equal this identity exactly.
REVIEWED_ENGINE_IDENTITY = ReviewedEngineIdentity(
    colibri_commit=PINNED_COLIBRI_COMMIT,
    basename=EXPECTED_ENGINE_BASENAME,
    size_bytes=704275,
    sha256="d7beaf6fe35de265cfaeb1d07914deeea6ceb8b3650e79b76e9c6d77176b528d",
    source_date_epoch=1784223580,
    deterministic_build_count=2,
)


@dataclass(frozen=True, slots=True)
class ReviewedConverterIdentity:
    """The one immutable, human-reviewed identity of the pinned
    ``convert_olmoe.py`` from the verified local Colibrì checkout at the
    pinned commit. Never a caller-supplied override. Carries its own
    ``colibri_commit`` so a manifest can bind its converter identity to
    the exact same pinned commit this script was verified against, not
    merely to "some" pinned-looking 40-character hex string."""

    basename: str
    size_bytes: int
    sha256: str
    colibri_commit: str

    def __post_init__(self) -> None:
        if self.basename != EXPECTED_CONVERTER_SCRIPT_BASENAME:
            raise ValueError("reviewed converter identity basename does not match the expected converter")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes <= 0:
            raise ValueError("reviewed converter identity size_bytes is out of bounds")
        if not is_hex64(self.sha256):
            raise ValueError("reviewed converter identity sha256 is not a SHA-256")
        if not is_hex40(self.colibri_commit) or self.colibri_commit != PINNED_COLIBRI_COMMIT:
            raise ValueError("reviewed converter identity colibri_commit does not match the pinned commit")


# Computed directly from the verified local pinned checkout
# (`c/tools/convert_olmoe.py` at commit `72d3d37231e922a6fa9afca16e08fa45842d5eb4`).
REVIEWED_CONVERTER_IDENTITY = ReviewedConverterIdentity(
    basename=EXPECTED_CONVERTER_SCRIPT_BASENAME,
    size_bytes=4469,
    sha256="43f3ed1bad0cd89656c1a2ee17843d86ff33f670ff12c51a803f2b6361a5e168",
    colibri_commit=PINNED_COLIBRI_COMMIT,
)


@dataclass(frozen=True, slots=True)
class ReviewedBoundedConverterIdentity:
    """The one immutable, human-reviewed identity of the in-repo
    memory-bounded converter ``colibri_stage2_bounded_convert.py``.

    The bounded converter is launched as a subprocess exactly like the
    pinned upstream script, so it needs exactly the same strength of
    proof. Being in-repo is not itself a guarantee: a working tree can be
    edited, a file can be patched after review, and a path-safety check
    proves only *where* a file is, never *what it contains*. This identity
    is what makes "the reviewed bounded converter" a checkable claim.

    Never a caller-supplied override -- there is no parameter anywhere
    that could substitute a different basename, size, or digest.
    """

    basename: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if self.basename != EXPECTED_BOUNDED_CONVERTER_BASENAME:
            raise ValueError(
                "reviewed bounded converter identity basename does not match the expected converter"
            )
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes <= 0:
            raise ValueError("reviewed bounded converter identity size_bytes is out of bounds")
        if not is_hex64(self.sha256):
            raise ValueError("reviewed bounded converter identity sha256 is not a SHA-256")


# Computed directly from the reviewed file in this repository. The digest
# is over the file's exact checked-out bytes, which a `.gitattributes`
# rule pins to LF on every platform -- without that rule this repository's
# `core.autocrlf=true` would hand a fresh Windows clone 24,670 CRLF bytes
# while a Linux checkout got 24,033 LF bytes, and no single pinned digest
# could ever match both.
#
# A `test_reviewed_bounded_converter_identity_matches_the_file_on_disk`
# test recomputes this from the real file, so editing the converter
# without updating this identity fails the suite rather than silently
# weakening the gate.
REVIEWED_BOUNDED_CONVERTER_IDENTITY = ReviewedBoundedConverterIdentity(
    basename=EXPECTED_BOUNDED_CONVERTER_BASENAME,
    size_bytes=24033,
    sha256="6f8145fc71f060c75d7d04a34c96cfd58d00daa3d51f2406a6de25e167d2266b",
)


# --- Which converter actually ran -------------------------------------------

# Stage 2A has exactly two reviewed converters, and a capture must record
# the identity of the one that *actually executed* -- never a default.
# These two constants are the only values that can select between them.
CONVERTER_KIND_BOUNDED = "bounded"
CONVERTER_KIND_PINNED_SCRIPT = "pinned_script"

# The closed kind -> reviewed identity binding. This mapping is the single
# place either identity can enter a capture: a caller supplies at most a
# *kind*, never a basename, size, hash, or identity object, so no capture
# can ever claim an identity that was not reviewed, and the bounded
# converter can never be recorded as the upstream script or vice versa.
REVIEWED_CONVERTER_IDENTITY_BY_KIND: Mapping[str, Any] = MappingProxyType(
    {
        CONVERTER_KIND_BOUNDED: REVIEWED_BOUNDED_CONVERTER_IDENTITY,
        CONVERTER_KIND_PINNED_SCRIPT: REVIEWED_CONVERTER_IDENTITY,
    }
)

CONVERTER_KINDS = frozenset(REVIEWED_CONVERTER_IDENTITY_BY_KIND)


def reviewed_identity_for_converter_kind(kind: str) -> Any:
    """Return the one reviewed identity for ``kind``, or fail closed.

    The only way to obtain a converter identity for a capture. There is
    deliberately no overload, parameter, or fallback that could return an
    identity for an unknown kind.
    """

    identity = REVIEWED_CONVERTER_IDENTITY_BY_KIND.get(kind)
    if identity is None:
        raise ValueError(f"unknown converter kind: {kind!r}")
    return identity
