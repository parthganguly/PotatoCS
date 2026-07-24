"""Derived one-token reference for Colibrì Stage 2A (OLMoE).

The reviewed upstream prompt/full token arrays are embedded in code (never
read from an external tokenizer or caller-supplied path). This module turns
them into one canonical, deterministic JSON file inside a private session
temporary directory, and tears that file down again with verified deletion.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from odysseus_desktop_backend.services.colibri_stage2_common import (
    EXPECTED_REF_BASENAME,
    FULL_TOKEN_IDS,
    PROMPT_TOKEN_IDS,
    ColibriStage2Failure,
)

_SESSION_PREFIX = "odysseus-colibri-stage2-ref-"


def reference_object() -> dict[str, list[int]]:
    """The exact reviewed reference payload, key order fixed."""

    return {"prompt_ids": list(PROMPT_TOKEN_IDS), "full_ids": list(FULL_TOKEN_IDS)}


def canonical_reference_bytes() -> bytes:
    """Deterministic, minified, fixed-key-order JSON bytes for the reference."""

    return json.dumps(reference_object(), separators=(",", ":"), sort_keys=False).encode("utf-8")


def canonical_reference_sha256() -> str:
    return hashlib.sha256(canonical_reference_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReferenceArtifact:
    path: Path
    basename: str
    size_bytes: int
    sha256: str


def create_private_reference_session(parent: Path | None = None) -> Path:
    """A private, memory-only-tracked temporary directory for the ref file."""

    try:
        return Path(tempfile.mkdtemp(prefix=_SESSION_PREFIX, dir=parent))
    except OSError as exc:
        raise ColibriStage2Failure("reference_write_failed") from exc


def write_private_reference(session_dir: Path) -> ReferenceArtifact:
    """Write the canonical one-token reference into ``session_dir``.

    Takes no external ref path and no tokenizer; the only inputs are the
    embedded, reviewed token arrays. Never overwrites an existing file.
    """

    target = session_dir / EXPECTED_REF_BASENAME
    if target.exists():
        raise ColibriStage2Failure("reference_write_failed")
    payload = canonical_reference_bytes()
    try:
        with target.open("xb") as handle:
            handle.write(payload)
    except OSError as exc:
        raise ColibriStage2Failure("reference_write_failed") from exc
    digest = hashlib.sha256(payload).hexdigest()
    return ReferenceArtifact(path=target, basename=target.name, size_bytes=len(payload), sha256=digest)


def delete_private_reference(artifact: ReferenceArtifact) -> None:
    """Delete the reference file and verify it is actually gone."""

    try:
        artifact.path.unlink(missing_ok=True)
    except OSError as exc:
        raise ColibriStage2Failure("reference_cleanup_failed") from exc
    if artifact.path.exists():
        raise ColibriStage2Failure("reference_cleanup_failed")


def teardown_private_reference_session(session_dir: Path) -> None:
    """Remove the private session directory and verify it is gone."""

    import shutil

    try:
        shutil.rmtree(session_dir, ignore_errors=False)
    except OSError as exc:
        raise ColibriStage2Failure("reference_cleanup_failed") from exc
    if session_dir.exists():
        raise ColibriStage2Failure("reference_cleanup_failed")
