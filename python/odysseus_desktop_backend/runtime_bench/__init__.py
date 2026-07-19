"""Repeatable local-runtime benchmark harness (development tool).

The product never imports or launches this package; it exists so that
runtime acceleration claims are measured, machine-readable, and
reproducible. See LOCAL_RUNTIME_ACCELERATION_RFC.md section 5.
"""

from odysseus_desktop_backend.runtime_bench.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    PAIRED_ARTIFACT_SCHEMA_VERSION,
    redaction_violations,
    validate_artifact,
    write_artifact,
)
from odysseus_desktop_backend.runtime_bench.shapes import BENCHMARK_SHAPES, quality_check
from odysseus_desktop_backend.runtime_bench.capabilities import (
    capability,
    runtime_capability_matrix,
)
from odysseus_desktop_backend.runtime_bench.comparison import compare_artifacts

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "PAIRED_ARTIFACT_SCHEMA_VERSION",
    "BENCHMARK_SHAPES",
    "capability",
    "compare_artifacts",
    "quality_check",
    "redaction_violations",
    "runtime_capability_matrix",
    "validate_artifact",
    "write_artifact",
]
