"""Tests for the Colibrì Stage 2A derived one-token reference (Part 4)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from odysseus_desktop_backend.services import colibri_stage2_reference as ref

# Independently computed (not via the production function under test) from
# the exact reviewed reference payload, so this is a real regression check
# on the canonical byte format, not a tautology.
_EXPECTED_CANONICAL_BYTES = b'{"prompt_ids":[510,5347,273,6181,310],"full_ids":[510,5347,273,6181,310,7785]}'
_EXPECTED_SHA256 = hashlib.sha256(_EXPECTED_CANONICAL_BYTES).hexdigest()


def test_canonical_reference_bytes_are_fixed_and_deterministic() -> None:
    assert ref.canonical_reference_bytes() == _EXPECTED_CANONICAL_BYTES
    assert ref.canonical_reference_bytes() == ref.canonical_reference_bytes()


def test_canonical_reference_sha256_matches_independent_hash() -> None:
    assert ref.canonical_reference_sha256() == _EXPECTED_SHA256


def test_reference_object_key_order_and_values() -> None:
    obj = ref.reference_object()
    assert list(obj.keys()) == ["prompt_ids", "full_ids"]
    assert obj["prompt_ids"] == [510, 5347, 273, 6181, 310]
    assert obj["full_ids"] == [510, 5347, 273, 6181, 310, 7785]


def test_write_private_reference_uses_no_external_path_or_tokenizer(tmp_path: Path) -> None:
    session = ref.create_private_reference_session(tmp_path)
    try:
        artifact = ref.write_private_reference(session)
        assert artifact.basename == "olmoe-stage2-one-token-ref.json"
        assert artifact.path.parent == session
        assert artifact.path.read_bytes() == _EXPECTED_CANONICAL_BYTES
        assert artifact.sha256 == _EXPECTED_SHA256
        assert artifact.size_bytes == len(_EXPECTED_CANONICAL_BYTES)
    finally:
        ref.teardown_private_reference_session(session)


def test_write_private_reference_never_overwrites(tmp_path: Path) -> None:
    session = ref.create_private_reference_session(tmp_path)
    try:
        ref.write_private_reference(session)
        with pytest.raises(ref.ColibriStage2Failure, match="reference_write_failed"):
            ref.write_private_reference(session)
    finally:
        ref.teardown_private_reference_session(session)


def test_delete_private_reference_verifies_deletion(tmp_path: Path) -> None:
    session = ref.create_private_reference_session(tmp_path)
    try:
        artifact = ref.write_private_reference(session)
        assert artifact.path.exists()
        ref.delete_private_reference(artifact)
        assert not artifact.path.exists()
    finally:
        ref.teardown_private_reference_session(session)


def test_delete_private_reference_is_idempotent_when_already_gone(tmp_path: Path) -> None:
    session = ref.create_private_reference_session(tmp_path)
    try:
        artifact = ref.write_private_reference(session)
        artifact.path.unlink()
        # missing_ok=True: deleting an already-gone reference must not raise.
        ref.delete_private_reference(artifact)
    finally:
        ref.teardown_private_reference_session(session)


def test_teardown_private_reference_session_removes_and_verifies(tmp_path: Path) -> None:
    session = ref.create_private_reference_session(tmp_path)
    ref.write_private_reference(session)
    ref.teardown_private_reference_session(session)
    assert not session.exists()


def test_private_reference_session_is_isolated_per_call(tmp_path: Path) -> None:
    first = ref.create_private_reference_session(tmp_path)
    second = ref.create_private_reference_session(tmp_path)
    try:
        assert first != second
    finally:
        ref.teardown_private_reference_session(first)
        ref.teardown_private_reference_session(second)
