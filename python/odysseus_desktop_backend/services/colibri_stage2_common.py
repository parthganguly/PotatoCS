"""Shared closed contract for the Colibrì Stage 2A OLMoE scaffold.

Every Stage 2 module (manifest, conversion capture, derived reference, real
token runner) imports its pinned identifiers and failure vocabulary from
here so the five pieces can never silently disagree about which model,
commit, or shard set is in scope. Nothing in this module downloads,
converts, or executes anything.
"""

from __future__ import annotations

import re
from typing import Any

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

MANIFEST_EVIDENCE_SCHEMA_VERSION = "colibri-stage2-olmoe-manifest-v1"
CONVERSION_CAPTURE_SCHEMA_VERSION = "colibri-stage2-olmoe-conversion-capture-v1"
CONVERSION_CAPTURE_STATE = "unreviewed_conversion_capture"

ALLOWED_CONVERSION_DEPENDENCY_NAMES = frozenset({"python", "torch", "transformers", "safetensors", "numpy"})

EXPECTED_CONVERTER_SCRIPT_BASENAME = "convert_olmoe.py"
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
        "shard_download_failed",
        "shard_verification_failed",
        "conversion_failed",
        "converted_shard_missing",
        "converted_shard_already_exists",
        "source_shard_deletion_failed",
        "source_shard_deletion_unverified",
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
